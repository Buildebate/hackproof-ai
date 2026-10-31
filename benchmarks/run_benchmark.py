#!/usr/bin/env python3
"""Step 4.2: Comparative Dual-Mode Benchmark Execution (Mode A vs Mode B).

Runs the 12-task polyglot benchmark (``benchmarks/datasets/`` manifest)
in two modes and records machine-checkable evidence for Step 4.3/4.4:

* **Mode A (single-agent baseline)**: Contriever localize, then ONE
  Muse Wonder 1.0 call synthesizing SEARCH/REPLACE blocks. No red gate,
  no test guard. A post-hoc guard verdict is still COMPUTED (labeled
  ``post_hoc``) so baseline reward hacking is measured, never assumed.
* **Mode B (HackProof AI)**: localize -> guard lock -> Spark red test ->
  asymmetric debate -> deterministic patch -> green eval -> receipt,
  mirroring ``hackproof.cli.cmd_verify`` stage for stage. Step
  4.3c.1 (R9) adds exactly one green-feedback revision + re-eval
  when the first patch applies but leaves F2P red or breaks P2P.

Execution tiers (``--runner``):

* ``oci`` (default): test execution inside Docker + gVisor ``runsc``
  with ``--network=none`` on the sovereign OCI host over Tailscale SSH;
  dependency installs run in the same containers with networking ON as
  an explicit, logged prep stage. LLM calls always originate locally.
* ``local``: same orchestration with ``LocalSubprocessRunner`` (smoke
  only — no gVisor isolation).

Eval protocol per task (both modes share ONE base run):

1. Fresh checkout of ``base_ref`` (tree hash verified against the
   manifest ``dataset_sha256``), dependencies installed.
2. Agent patch produced from the problem statement + localized code
   ONLY — FAIL_TO_PASS / PASS_TO_PASS IDs, gold patch and test patch
   never enter agent input (prompts are archived for audit).
3. Guard verdict from the agent tree state (Mode B blocks on tamper;
   Mode A records post-hoc).
4. Agent-touched test files reset to base, official ``test_patch``
   applied (harness use only, fetched post-agent), F2P+P2P executed,
   JUnit + transcript archived.
5. Scoring is relative to the base run measured in the same sandbox
   (see ``bench_lib.score_task``).

Outputs under ``--out`` (default ``benchmarks/results``)::

    raw_logs/<mode>/<task>.log      full timestamped transcript
    junit_reports/<mode>/<task>.xml agent-run JUnit (F2P+P2P)
    junit_reports/base/<task>.xml   base-run JUnit (shared by modes)
    results/<mode>/<task>.json      machine verdict (done marker)
    diffs/<mode>/<task>.diff        agent code patch (tracked files)
    guard_reports/<mode>/<task>.json guard verdict (Mode B binding)
    prompts/<mode>/<task>/          archived agent prompts (audit)
    harness_cache/<task>_*.json     HF input/test_patch caches + SHAs

``--resume`` skips task+mode pairs whose result JSON carries
``"done": true``. Every live LLM call appends token usage to the FinOps
ledger (honestly unpriced: ``priced: false`` until grant pricing lands).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
PKG_ROOT = BENCH_DIR.parent
REPO_ROOT = PKG_ROOT.parent.parent
sys.path.insert(0, str(PKG_ROOT / "src"))
sys.path.insert(0, str(BENCH_DIR))

from bench_lib import (  # noqa: E402
    eval_files_for,
    guard_files_for,
    index_pytest_cases,
    index_vitest_cases,
    judge_red,
    load_manifest,
    parse_collect_output,
    parse_junit,
    remap_collected_index,
    resolve_manifest_ids,
    score_task,
    sha256_bytes,
    sha256_text,
    utcnow_iso,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OCI_STAGING_REL = "hackproof-bench/staging"
OCI_RUNS_REL = "hackproof-bench/runs"
PY_IMAGE = "python:3.11-slim"
TS_IMAGE = "node:22-slim"
# Era interpreter for pre-3.10 code (single source of truth for the
# collect fallback in Step 4.3b.4; pinned entries below spell the same
# tag literally for manifest readability).
ERA_PY_IMAGE = "python:3.8-slim"
# Era interpreters: pytest <=6.3 cannot run on Python 3.11 (its AST
# assertion rewriter predates 3.10+ AST invariants:
# `TypeError: required field "lineno" missing from alias`). Era tasks
# pin era Pythons; everything else defaults to 3.11. Requests tasks
# stay on 3.11 unless their base run proves otherwise (runner is a
# modern pytest; only the old package code could object).
TASK_PYTHON_IMAGE = {
    "pytest-dev__pytest-5787": "python:3.8-slim",
    "pytest-dev__pytest-7432": "python:3.8-slim",
    "pytest-dev__pytest-8399": "python:3.8-slim",
    # requests 1.1.0 (2013): collections.MutableMapping, dead on 3.10+.
    "psf__requests-1142": "python:3.8-slim",
    # requests 2.9.0 (2015, Step 4.3b.4/R7): `CaseInsensitiveDict`
    # subclasses `collections.MutableMapping` (removed in 3.10) — 4.2
    # base collect died exit 2 with 0 IDs on 3.11 (85 unresolved).
    "psf__requests-2931": "python:3.8-slim",
}
# Runtime era pins (Step 4.3b.4): tasks whose base collect failed with
# an era signature and were rescued onto ERA_PY_IMAGE during THIS
# matrix process. Consulted by py_image_for so prep/collect/eval/red
# stay on one interpreter; loudly logged, never persisted (the next
# process re-derives it via the same loud fallback).
_ERA_PINNED_RUNTIME: set[str] = set()


def py_image_for(task: dict) -> str:
    """Container image for a task (era override or default 3.11)."""
    if task["id"] in _ERA_PINNED_RUNTIME:
        return ERA_PY_IMAGE
    return TASK_PYTHON_IMAGE.get(task["id"], PY_IMAGE)
# Container runtime for benchmark execution. EVIDENCE (Step 4.2 smoke,
# 2026-10-30): this host's gVisor build (runsc 20240729 on Oracle UEK
# 7.0 aarch64) silently kills containers doing sustained file I/O
# through the gofer bind-mount (exit 2, no output; CPU-only probes
# survive, I/O-busy probes die deterministically). runc is 100%
# reliable here. The product default stays runsc (see RemoteSSHRunner);
# the benchmark pins runc as a documented environment deviation while
# keeping the --network=none air-gap invariant for all untrusted code.
OCI_RUNTIME = "runc"
EVAL_JUNIT_NAME = "hackproof-eval-junit.xml"
RED_JUNIT_NAME = "hackproof-red-junit.xml"
HF_ROWS_ENDPOINT = "https://datasets-server.huggingface.co/rows"
HF_DATASETS = {
    "python": "SWE-bench/SWE-bench_Verified",
    "typescript": "SWE-bench/SWE-bench_Multilingual",
}
# Scratch names created by the harness inside run dirs; ignored when
# reading ``git status`` for guard purposes.
SCRATCH_NAMES = frozenset(
    {
        ".bench-venv",
        ".pnpm-store",
        "node_modules",
        EVAL_JUNIT_NAME,
        RED_JUNIT_NAME,
        "OK.json",
        "red-post-junit.xml",
    }
)

TIMEOUT_SSH = 60
TIMEOUT_PREP_PY = 900
TIMEOUT_PREP_TS = 1800
TIMEOUT_EVAL = 900
TIMEOUT_RED = 300


def load_dotenv_defaults(names: set[str]) -> None:
    """Import listed keys from the repo-root ``.env`` when unset.

    Only the requested key names are read; values never enter logs or
    transcripts (Zero-Leak: the file itself is git-ignored).
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in names and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass
class BenchConfig:
    """Resolved runtime configuration (no secrets stored)."""

    mode: str
    manifest_path: Path
    out_dir: Path
    runner_tier: str
    task_ids: list[str] = field(default_factory=list)
    resume: bool = False
    work_dir: Path = field(default_factory=lambda: Path("/tmp/hp_bench_work"))
    ledger_path: Path = field(
        default_factory=lambda: PKG_ROOT / "finops_ledger.json"
    )
    oci_host: str = "100.112.52.73"
    oci_user: str = "ubuntu"
    oci_key: str = str(Path.home() / ".ssh" / "id_ed25519")
    tailscale_socket: str = "/tmp/ts/sock"


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


class Tee:
    """Timestamped dual writer: stdout mirror + transcript file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8")
        self.path = path

    def log(self, message: str) -> None:
        """Write one timestamped line to both outputs."""
        line = f"[{utcnow_iso()}] {message}"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def blob(self, title: str, body: str, max_chars: int = 6000) -> None:
        """Log a titled (possibly truncated) multi-line body.

        Oversized bodies keep head + tail: install/test failures
        always report at the tail, which head-only trimming hid.
        """
        self.log(f"--- {title} ({len(body)} chars) ---")
        if len(body) <= max_chars:
            trimmed = body
        else:
            half = max_chars // 2
            trimmed = (
                body[:half]
                + f"\n... [truncated {len(body) - max_chars} chars] ...\n"
                + body[-half:]
            )
        print(trimmed, flush=True)
        self._fh.write(trimmed + ("\n" if not trimmed.endswith("\n") else ""))
        self._fh.flush()

    def close(self) -> None:
        """Flush and close the transcript file."""
        self._fh.close()


# ---------------------------------------------------------------------------
# OCI SSH transport
# ---------------------------------------------------------------------------


@dataclass
class SshConfig:
    """OCI reachability (key path only — never key material)."""

    host: str
    user: str
    key_path: str
    tailscale_socket: str = "/tmp/ts/sock"
    connect_timeout: int = 15

    def base_argv(self) -> list[str]:
        """Non-interactive SSH argv routed via the Tailnet daemon."""
        proxy = (
            f"tailscale --socket={self.tailscale_socket} nc %h %p"
        )
        return [
            "ssh",
            "-i",
            self.key_path,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ProxyCommand={proxy}",
            f"{self.user}@{self.host}",
        ]


def ssh_exec(
    cfg: SshConfig, script: str, timeout: int = TIMEOUT_SSH,
    input_bytes: bytes | None = None,
) -> tuple[int, str, str]:
    """Run one remote shell script; return (exit, stdout, stderr)."""
    proc = subprocess.run(
        [*cfg.base_argv(), script],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def ssh_write_file(
    cfg: SshConfig, remote_path: str, data: bytes,
    timeout: int = TIMEOUT_SSH,
) -> int:
    """Write bytes to a remote path via stdin (no local temp files)."""
    quoted = "'" + remote_path.replace("'", "'\"'\"'") + "'"
    proc = subprocess.run(
        [*cfg.base_argv(), f"cat > {quoted}"],
        input=data,
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode


def ssh_read_file(
    cfg: SshConfig, remote_path: str, timeout: int = TIMEOUT_SSH
) -> bytes | None:
    """Read a remote file; None when missing or unreadable."""
    quoted = "'" + remote_path.replace("'", "'\"'\"'") + "'"
    try:
        proc = subprocess.run(
            [*cfg.base_argv(), f"cat {quoted}"],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def oci_runner_executor(cfg: SshConfig):
    """Build a ``RemoteSSHRunner`` executor routed via Tailnet.

    The stock runner builds ``ssh -i key ... user@host script`` with no
    ProxyCommand slot; the injected executor splices the Tailnet
    ProxyCommand right after ``ssh`` so OCI stays reachable from the
    sandbox without touching shipped runner code.
    """
    proxy = f"tailscale --socket={cfg.tailscale_socket} nc %h %p"

    def _execute(
        argv, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        full = [argv[0], "-o", f"ProxyCommand={proxy}", *argv[1:]]
        return subprocess.run(
            full, capture_output=True, text=True, timeout=timeout
        )

    return _execute


# ---------------------------------------------------------------------------
# Hugging Face harness data (agent input vs harness-only, strictly split)
# ---------------------------------------------------------------------------


def hf_scan_dataset(dataset: str, want: set[str]) -> dict[str, dict]:
    """Scan one dataset split once, returning cells for ``want`` IDs."""
    want = set(want)
    found: dict[str, dict] = {}
    offset = 0
    while want - set(found):
        url = (
            f"{HF_ROWS_ENDPOINT}?dataset="
            f"{urllib.parse.quote(dataset, safe='')}"
            f"&config=default&split=test&offset={offset}&length=100"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        rows = data.get("rows", [])
        if not rows:
            break
        for row in rows:
            cells = row.get("row", {}) or {}
            iid = cells.get("instance_id")
            if iid in want and iid not in found:
                found[iid] = cells
        if len(rows) < 100:
            break
        offset += len(rows)
    missing = want - set(found)
    if missing:
        raise KeyError(f"instances {sorted(missing)} not in {dataset}")
    return found


def hf_fetch_row(dataset: str, instance_id: str) -> dict:
    """Fetch one dataset row's cells from the datasets-server API."""
    return hf_scan_dataset(dataset, {instance_id})[instance_id]


def prefetch_split_caches(
    manifest: dict, language: str, cache_dir: Path, tee: Tee, kind: str
) -> None:
    """Fetch one language split in a SINGLE scan; write per-task caches.

    ``kind`` is ``input`` (agent-visible columns only) or ``harness``
    (test_patch + env metadata, eval use only). One scan bounds HF
    traffic regardless of task count; agent/harness column separation
    is enforced per cache file (see get_agent_input/get_test_patch).
    """
    ids = [t["id"] for t in manifest["tasks"] if t["language"] == language]
    pending = [
        iid for iid in ids
        if not (cache_dir / f"{iid}_{kind}.json").exists()
    ]
    if not pending:
        return
    dataset = HF_DATASETS[language]
    tee.log(f"harness: prefetching {len(pending)} {kind} caches ({dataset})")
    cells = hf_scan_dataset(dataset, set(pending))
    by_id = {t["id"]: t for t in manifest["tasks"]}
    for iid in pending:
        task = by_id[iid]
        row = cells[iid]
        if kind == "input":
            cached = {
                "instance_id": iid,
                "source": f"{dataset}@test (datasets-server)",
                "base_commit": row.get("base_commit"),
                "problem_statement": row.get("problem_statement") or "",
                "hints_text": row.get("hints_text") or "",
            }
            cached["problem_statement_sha256"] = sha256_text(
                cached["problem_statement"]
            )
            blob = json.dumps(cached)
            assert '"patch"' not in blob and '"test_patch"' not in blob
        else:
            cached = {
                "instance_id": iid,
                "source": f"{dataset}@test (datasets-server, harness-only)",
                "test_patch": row.get("test_patch") or "",
                "environment_setup_commit": row.get(
                    "environment_setup_commit") or "",
                "version": row.get("version") or "",
            }
            cached["test_patch_sha256"] = sha256_text(cached["test_patch"])
        if row.get("base_commit") != task["base_ref"]:
            raise ValueError(
                f"HF base_commit drift for {iid}: manifest "
                f"{task['base_ref']} vs HF {row.get('base_commit')}"
            )
        (cache_dir / f"{iid}_{kind}.json").write_text(
            json.dumps(cached, indent=1), encoding="utf-8"
        )
    tee.log(f"harness: {kind} prefetch complete ({len(pending)} tasks)")


def get_agent_input(task: dict, cache_dir: Path, tee: Tee) -> dict:
    """Load cached agent input or fetch ONLY agent-visible columns.

    Cached file carries ``problem_statement`` + ``hints_text`` +
    ``base_commit`` and their SHA-256. Gold columns (``patch``,
    ``test_patch``) are dropped before caching — verified by assert —
    so no code path can leak solutions into agent prompts.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{task['id']}_input.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached.get("base_commit") == task["base_ref"], (
            f"cached base drift for {task['id']}"
        )
        assert "patch" not in cached and "test_patch" not in cached
        tee.log(f"input: cache hit ({cache_path.name})")
        return cached
    dataset = HF_DATASETS[task["language"]]
    tee.log(f"input: fetching {task['id']} from {dataset} ...")
    cells = hf_fetch_row(dataset, task["id"])
    if cells.get("base_commit") != task["base_ref"]:
        raise ValueError(
            f"HF base_commit drift for {task['id']}: manifest "
            f"{task['base_ref']} vs HF {cells.get('base_commit')}"
        )
    cached = {
        "instance_id": task["id"],
        "source": f"{dataset}@test (datasets-server)",
        "base_commit": cells.get("base_commit"),
        "problem_statement": cells.get("problem_statement") or "",
        "hints_text": cells.get("hints_text") or "",
    }
    cached["problem_statement_sha256"] = sha256_text(
        cached["problem_statement"]
    )
    blob = json.dumps(cached)
    assert '"patch"' not in blob and '"test_patch"' not in blob
    cache_path.write_text(json.dumps(cached, indent=1), encoding="utf-8")
    tee.log(
        f"input: cached ({len(cached['problem_statement'])} stmt chars, "
        f"sha={cached['problem_statement_sha256'][:12]})"
    )
    return cached


def get_test_patch(task: dict, cache_dir: Path, tee: Tee) -> dict:
    """Load cached harness-only test_patch (NEVER agent input).

    Called exclusively inside eval stages, after the agent run has
    completed and its prompts are archived. The gold solution column
    (``patch``) is not fetched at all.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{task['id']}_testpatch.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        tee.log(f"harness: test_patch cache hit ({cache_path.name})")
        return cached
    dataset = HF_DATASETS[task["language"]]
    tee.log(f"harness: fetching test_patch for {task['id']} ...")
    cells = hf_fetch_row(dataset, task["id"])
    cached = {
        "instance_id": task["id"],
        "source": f"{dataset}@test (datasets-server, harness-only)",
        "test_patch": cells.get("test_patch") or "",
        "environment_setup_commit": cells.get("environment_setup_commit")
        or "",
        "version": cells.get("version") or "",
    }
    cached["test_patch_sha256"] = sha256_text(cached["test_patch"])
    cache_path.write_text(json.dumps(cached, indent=1), encoding="utf-8")
    tee.log(
        f"harness: test_patch cached "
        f"({len(cached['test_patch'])} chars, "
        f"sha={cached['test_patch_sha256'][:12]})"
    )
    return cached


# ---------------------------------------------------------------------------
# Repositories: local clone + remote run dirs
# ---------------------------------------------------------------------------


def git_tree_of_commit(repo: Path, ref: str) -> str:
    """Return the git tree hash of ``ref`` in a local repository."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{ref}^{{tree}}"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SSH,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rev-parse failed in {repo}: {proc.stderr[:300]}")
    return proc.stdout.strip()


def ensure_local_clone(task: dict, work_dir: Path, tee: Tee) -> Path:
    """Fetch ``base_ref`` from GitHub and verify the frozen tree hash.

    Mirrors the STAGING.md reproduce recipe (shallow fetch of the pinned
    commit); the clone is rejected unless
    ``sha256("git-tree:" + tree)`` equals the manifest ``dataset_sha256``.
    """
    dest = work_dir / task["id"]
    expected = task["dataset_sha256"]
    if dest.exists():
        try:
            tree = git_tree_of_commit(dest, "HEAD")
            digest = sha256_text(f"git-tree:{tree}")
            if digest == expected:
                tee.log(f"clone: reuse {dest} (tree verified)")
                return dest
        except RuntimeError:
            pass
        tee.log("clone: stale work dir, recreating")
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{task['repo']}.git"
    tee.log(f"clone: fetching {task['base_ref'][:12]} from {repo_url} ...")
    init = subprocess.run(
        ["git", "init", "-q", str(dest)], capture_output=True, text=True,
        timeout=TIMEOUT_SSH,
    )
    if init.returncode != 0:
        raise RuntimeError(f"git init failed: {init.stderr[:300]}")
    fetch = subprocess.run(
        ["git", "-C", str(dest), "fetch", "--depth", "1", repo_url,
         task["base_ref"]],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if fetch.returncode != 0:
        raise RuntimeError(f"github fetch failed: {fetch.stderr[:500]}")
    checkout = subprocess.run(
        ["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SSH,
    )
    if checkout.returncode != 0:
        raise RuntimeError(f"checkout failed: {checkout.stderr[:300]}")
    tree = git_tree_of_commit(dest, "HEAD")
    digest = sha256_text(f"git-tree:{tree}")
    if digest != expected:
        raise ValueError(
            f"tree hash mismatch for {task['id']}: manifest {expected} "
            f"vs clone {digest}"
        )
    tee.log(f"clone: ready at {dest} (tree {tree[:12]} verified)")
    return dest


_REMOTE_HOME_CACHE: dict[str, str] = {}


def remote_home(cfg: SshConfig) -> str:
    """Resolve the remote absolute home dir (docker needs abs paths)."""
    key = f"{cfg.user}@{cfg.host}"
    if key not in _REMOTE_HOME_CACHE:
        exit_code, stdout, _ = ssh_exec(cfg, 'printf %s "$HOME"', timeout=30)
        if exit_code != 0 or not stdout.strip():
            raise RuntimeError("cannot resolve remote $HOME")
        _REMOTE_HOME_CACHE[key] = stdout.strip()
    return _REMOTE_HOME_CACHE[key]


def remote_task_dir(cfg: SshConfig, task_id: str) -> str:
    """Remote parent dir for one task's prep/run checkouts (absolute)."""
    return f"{remote_home(cfg)}/{OCI_RUNS_REL}/{task_id}"


def remote_prep_dir(cfg: SshConfig, task_id: str) -> str:
    """Remote pristine prep checkout (deps installed, never patched)."""
    return f"{remote_task_dir(cfg, task_id)}/prep"


def remote_run_dir(cfg: SshConfig, task_id: str, label: str) -> str:
    """Remote per-stage checkout (``label`` in base/A/B)."""
    return f"{remote_task_dir(cfg, task_id)}/{label}"


def ssh_rmtree(cfg: SshConfig, path: str, tee: Tee, stage: str) -> None:
    """Delete a remote run dir via a throwaway root container.

    Containers run as root, so scratch they create (venvs,
    node_modules, JUnit) is root-owned and plain ``rm -rf`` as the SSH
    user fails. Deletion goes through ``alpine rm -rf`` with the
    parent mounted. Safety: ``path`` must sit under the runs root —
    anything else raises instead of deleting.
    """
    runs_root = f"{remote_home(cfg)}/{OCI_RUNS_REL}/"
    if not path.startswith(runs_root) or path.rstrip("/") == runs_root.rstrip(
        "/"
    ):
        raise ValueError(f"refusing to delete outside runs root: {path}")
    parent, _, base = path.rstrip("/").rpartition("/")
    ssh_exec(cfg, f"mkdir -p {parent}", timeout=TIMEOUT_SSH)
    script = (
        f"docker run --rm --runtime={OCI_RUNTIME} "
        f"-v {parent}:{parent} -w {parent} alpine rm -rf {base}"
    )
    exit_code, _, stderr = ssh_exec(cfg, script, timeout=300)
    if exit_code != 0:
        raise RuntimeError(f"{stage}: remote rmtree failed: {stderr[-400:]}")
    tee.log(f"{stage}: removed {path}")


def prep_is_fresh(
    cfg: SshConfig, task: dict, fingerprint: dict, tee: Tee
) -> bool:
    """Check the remote prep marker (base + tree + deps fingerprint)."""
    raw = ssh_read_file(cfg, f"{remote_prep_dir(cfg, task['id'])}/OK.json")
    if raw is None:
        return False
    try:
        marker = json.loads(raw.decode("utf-8"))
    except ValueError:
        return False
    want = {
        "base_ref": task["base_ref"],
        "dataset_sha256": task["dataset_sha256"],
        "fingerprint": fingerprint,
    }
    fresh = all(marker.get(key) == value for key, value in want.items())
    tee.log(f"prep: marker {'fresh, reusing' if fresh else 'stale'}")
    return fresh


def prep_fingerprint(task: dict) -> dict:
    """Dependency fingerprint invalidating stale prep checkouts."""
    if task["language"] == "python":
        return {
            "image": py_image_for(task),
            "venv": ".bench-venv",
            "install": "pytest+e",
            "runtime": OCI_RUNTIME,
            "history": "full" if task["repo"] in SCM_VERSIONED_REPOS
            else "shallow",
        }
    return {
        "image": TS_IMAGE, "pkg": "pnpm-install", "runtime": OCI_RUNTIME,
    }


# Repos whose build reads the version from git history (setuptools-scm):
# they need full history + tags (setuptools-scm refuses shallow clones;
# synthetic tags do NOT satisfy it). Other repos stay shallow. Either
# way HEAD tree bytes (and dataset_sha256) are verified identical.
SCM_VERSIONED_REPOS = frozenset({"pytest-dev/pytest"})


def ssh_prep_checkout(
    cfg: SshConfig, task: dict, tee: Tee, local: bool = False
) -> str:
    """Create the pristine prep checkout on the execution host.

    Returns the prep dir path (remote or local depending on tier).
    """
    if local:
        raise NotImplementedError("local prep is staged via OCI recipe only")
    prep = remote_prep_dir(cfg, task["id"])
    ssh_rmtree(cfg, prep, tee, "prep")
    repo_url = f"https://github.com/{task['repo']}.git"
    script = "\n".join(
        [
            "set -e",
            f"mkdir -p {prep}",
            f"git init -q {prep}",
            f"git -C {prep} remote add origin {repo_url}",
            f"git -C {prep} fetch --depth 1 origin {task['base_ref']}",
            f"git -C {prep} checkout -q FETCH_HEAD",
            f"git -C {prep} rev-parse 'HEAD^{{tree}}'",
        ]
    )
    tee.log(f"prep: remote checkout {task['base_ref'][:12]} ...")
    exit_code, stdout, stderr = ssh_exec(cfg, script, timeout=300)
    if exit_code != 0:
        raise RuntimeError(f"remote checkout failed: {stderr[-800:]}")
    tree = stdout.strip().splitlines()[-1].strip()
    digest = sha256_text(f"git-tree:{tree}")
    if digest != task["dataset_sha256"]:
        raise ValueError(
            f"remote tree mismatch for {task['id']}: manifest "
            f"{task['dataset_sha256']} vs remote {digest}"
        )
    tee.log(f"prep: checkout ready (tree {tree[:12]} verified)")
    if task["repo"] in SCM_VERSIONED_REPOS:
        tee.log("prep: unshallowing history + tags (setuptools-scm) ...")
        deep_script = "\n".join(
            [
                "set -e",
                f"git -C {prep} fetch --unshallow origin",
                f"git -C {prep} fetch --tags origin",
                f"git -C {prep} rev-parse 'HEAD^{{tree}}'",
            ]
        )
        exit_code, stdout, stderr = ssh_exec(cfg, deep_script, timeout=900)
        if exit_code != 0:
            raise RuntimeError(f"history deepen failed: {stderr[-500:]}")
        deep_tree = stdout.strip().splitlines()[-1].strip()
        if sha256_text(f"git-tree:{deep_tree}") != task["dataset_sha256"]:
            raise ValueError("tree drifted during history deepen")
        tee.log("prep: full history ready (tree re-verified)")
    return prep


def oci_sandbox_runner(cfg: SshConfig, image: str):
    """Build a Tailnet-routed gVisor runner for one image."""
    from hackproof.sandbox.runner import RemoteSSHRunner

    return RemoteSSHRunner(
        host=cfg.host,
        user=cfg.user,
        key_path=cfg.key_path,
        image=image,
        strict_host_key_checking=False,
        runtime=OCI_RUNTIME,
        executor=oci_runner_executor(cfg),
    )


def robust_run(
    runner: object,
    argv: list[str],
    cwd: object,
    timeout_sec: int,
    network_disabled: bool,
    tee: Tee,
    stage: str,
    max_attempts: int = 6,
) -> object:
    """Run one sandbox command with bounded transient-flake retries.

    The OCI path intermittently returns a nonzero exit with COMPLETELY
    empty stdout+stderr (observed exit 2; byte-identical invocations
    succeed on retry — host load spikes from co-hosted containers).
    Real test runners and package managers always emit output, so an
    empty-output failure is retried with backoff (5/15/30/45/60 s);
    every retry is logged in the transcript. Genuine failures
    (non-empty output) are returned immediately, never retried.
    """
    backoffs = (5, 15, 30, 45, 60)
    res = runner.run_in_sandbox(  # type: ignore[attr-defined]
        argv, cwd, timeout_sec=timeout_sec, network_disabled=network_disabled
    )
    attempt = 0
    while (
        attempt + 1 < max_attempts
        and res.exit_code != 0
        and not res.stdout.strip()
        and not res.stderr.strip()
    ):
        wait = backoffs[min(attempt, len(backoffs) - 1)]
        tee.log(
            f"{stage}: transient empty-output failure "
            f"(exit={res.exit_code}); retry {attempt + 1} in {wait}s ..."
        )
        time.sleep(wait)
        res = runner.run_in_sandbox(  # type: ignore[attr-defined]
            argv, cwd, timeout_sec=timeout_sec,
            network_disabled=network_disabled,
        )
        attempt += 1
        tee.log(f"{stage}: retry {attempt} exit={res.exit_code}")
    return res


def prep_dependencies(
    cfg: SshConfig, task: dict, prep: str, tee: Tee,
    image: str | None = None, stage: str = "prep",
) -> dict:
    """Install test dependencies inside the prep checkout (network ON).

    The ONLY stage allowed container networking: ``pip``/``pnpm`` must
    reach their registries. Everything the agent touches afterwards runs
    with ``--network=none``. ``image`` overrides ``py_image_for(task)``
    (Step 4.3b.4 era fallback re-preps a run dir, not the prep dir);
    ``stage`` relabels log lines for that rescue path.
    """
    # Transport rules (empirically derived on this host):
    # * Every container invocation is EPHEMERAL (only the mounted work
    #   dir persists): multi-step toolchains that depend on container
    #   state (apt git, corepack shims) MUST run as one shell sequence
    #   in a single container. `bash -lc` chains are reliable under
    #   runc (the silent-death flakes were runsc-gofer-specific).
    # * NEVER one-shot `python -m venv` (pip bootstrap dies under the
    #   old runsc path); keep the proven --without-pip + ensurepip.
    # * NEVER venv/pip scripts as container init (shebang exec fails);
    #   route everything through the python ELF (`python -m ...`).
    # * NEVER `./node_modules/.bin/*` scripts as container init;
    #   route through the node ELF (`node <script> ...`).
    if task["language"] == "python":
        runner = oci_sandbox_runner(cfg, image or py_image_for(task))
        venv_python = "./.bench-venv/bin/python"
        chain = [
            "set -ex",
            # git CLI: setuptools-scm shells out to it during
            # `pip install -e .` (slim images ship without git).
            "apt-get update -qq",
            "apt-get install -y -qq --no-install-recommends git",
            "python -m venv --without-pip .bench-venv",
            f"{venv_python} -m ensurepip",
            f"{venv_python} -m pip install -q --upgrade pip",
        ]
        if task["repo"] != "pytest-dev/pytest":
            # The pytest repo IS the runner: its editable install brings
            # the era-correct pytest (a modern pytest here would only
            # poison dependency resolution with too-new pluggy).
            chain.append(f"{venv_python} -m pip install -q pytest")
        chain.append(f"{venv_python} -m pip install -q -e .")
        tee.log(f"{stage}: python toolchain (single container) ...")
        res = robust_run(
            runner, ["bash", "-lc", " && ".join(chain)], prep,
            timeout_sec=TIMEOUT_PREP_PY, network_disabled=False,
            tee=tee, stage=stage,
        )
        tee.log(f"{stage}: toolchain exit={res.exit_code}")
        if res.exit_code != 0:
            tee.blob(f"{stage} output", res.stdout + "\n" + res.stderr)
            raise RuntimeError("python prep install failed")
        versions: list[str] = []
        for argv in (
            [venv_python, "--version"],
            [venv_python, "-m", "pip", "--version"],
            [venv_python, "-m", "pytest", "--version"],
        ):
            probe = robust_run(
                runner, argv, prep, timeout_sec=TIMEOUT_SSH,
                network_disabled=True, tee=tee, stage=f"{stage}-probe",
            )
            versions.append(
                (probe.stdout + probe.stderr).strip().replace("\n", " ")
            )
        tee.log(f"{stage}: toolchain [{' | '.join(versions)[:200]}]")
        return {"toolchain": " | ".join(versions)[:300]}
    runner = oci_sandbox_runner(cfg, TS_IMAGE)
    tee.log("prep: corepack + pnpm install (single container) ...")
    res = robust_run(
        runner,
        ["bash", "-lc",
         "set -ex && corepack enable && pnpm install --no-frozen-lockfile"],
        prep, timeout_sec=TIMEOUT_PREP_TS, network_disabled=False,
        tee=tee, stage="prep",
    )
    tee.log(f"prep: pnpm exit={res.exit_code}")
    if res.exit_code != 0:
        tee.blob("prep output", res.stdout + "\n" + res.stderr)
        raise RuntimeError("node prep install failed")
    node_ver = robust_run(
        runner, ["node", "--version"], prep, timeout_sec=TIMEOUT_SSH,
        network_disabled=True, tee=tee, stage="prep-probe",
    )
    # NOTE: fresh container => cold corepack cache => pnpm --version
    # downloads pnpm on first use (network ON for the probe only).
    pnpm_ver = robust_run(
        runner, ["bash", "-lc", "corepack enable && pnpm --version"],
        prep, timeout_sec=300, network_disabled=False,
        tee=tee, stage="prep-probe",
    )
    vitest_ver = robust_run(
        runner, ["node", "./node_modules/vitest/vitest.mjs", "--version"],
        prep, timeout_sec=TIMEOUT_SSH, network_disabled=True,
        tee=tee, stage="prep-probe",
    )
    ts_versions = " | ".join(
        (probe.stdout + probe.stderr).strip().replace("\n", " ")
        for probe in (node_ver, pnpm_ver, vitest_ver)
    )
    tee.log(f"prep: toolchain [{ts_versions[:200]}]")
    return {"toolchain": ts_versions[:300]}


def ssh_clean_pycache(cfg: SshConfig, prep: str, tee: Tee) -> None:
    """Drop root-owned ``__pycache__`` from a prep checkout (Step 4.2).

    Containers run as root, so bytecode they bake into the prep tree
    is root-owned and breaks ``cp -r`` as the SSH user (Permission
    denied, pytest-5787 evidence). Removal goes through a throwaway
    root container; safety rails match :func:`ssh_rmtree`.
    """
    runs_root = f"{remote_home(cfg)}/{OCI_RUNS_REL}/"
    if not prep.startswith(runs_root) or prep.rstrip("/") == runs_root.rstrip(
        "/"
    ):
        raise ValueError(f"refusing to clean outside runs root: {prep}")
    script = (
        f"docker run --rm --runtime={OCI_RUNTIME} "
        f"-v {prep}:/p -w /p alpine sh -c "
        "\"find /p -name __pycache__ -type d -prune -exec rm -rf {} +\""
    )
    exit_code, _, stderr = ssh_exec(cfg, script, timeout=300)
    if exit_code != 0:
        raise RuntimeError(f"stage: pycache clean failed: {stderr[-400:]}")


def fresh_run_dir(cfg: SshConfig, task: dict, label: str, tee: Tee) -> str:
    """Copy the pristine prep checkout into a per-stage run dir."""
    task_id = task["id"]
    prep = remote_prep_dir(cfg, task_id)
    dest = remote_run_dir(cfg, task_id, label)
    ssh_rmtree(cfg, dest, tee, "stage")
    ssh_clean_pycache(cfg, prep, tee)
    script = f"set -e\ncp -r {prep} {dest}"
    exit_code, _, stderr = ssh_exec(cfg, script, timeout=300)
    if exit_code != 0:
        raise RuntimeError(f"run dir copy failed: {stderr[-500:]}")
    if task["language"] == "python":
        fixup_relocatable_venv(cfg, prep, dest, tee)
    tee.log(f"stage: fresh run dir {label}/")
    return dest


def fixup_relocatable_venv(
    cfg: SshConfig, prep: str, dest: str, tee: Tee
) -> None:
    """Rewrite the copied venv's editable install to the run dir.

    Two PEP 660 layouts are supported (guarded, fail-loud):
    A. static ``__editable__*.pth`` holding the single SOURCE path
       (``<prep>/src`` or ``<prep>``) — repointed at the run dir;
    B. finder-module ``__editable__*.pth`` (``import ..._finder``) with
       the prep tree baked into ``__editable___*_finder.py`` MAPPING —
       prep occurrences rewritten to the run dir.
    After ``cp -r`` the copied venv still points at prep — which is NOT
    mounted into run containers, so the package would become
    unimportable. Each run container mounts only its own dir, so the
    rewrite (host-side edit) keeps run dirs self-contained, which
    strengthens isolation.
    """
    script_a = "\n".join(
        [
            "set -e",
            f'pth=$(ls {dest}/.bench-venv/lib/python3*/site-packages/'
            "__editable__*.pth)",
            '[ "$(printf "%s" "$pth" | wc -l)" = "0" ]',
            'content=$(cat "$pth")',
            f'[ "$content" = "{prep}/src" ] || [ "$content" = "{prep}" ]',
            f'sed -i "s|^{prep}|{dest}|" "$pth"',
            'echo "fixed: $(cat "$pth")"',
        ]
    )
    exit_code, stdout, stderr = ssh_exec(cfg, script_a, timeout=TIMEOUT_SSH)
    if exit_code == 0:
        tee.log(f"stage: venv repointed ({stdout.strip()[-90:]})")
        return
    err_a = stderr[-300:]
    # Layout B: finder module. Strict shape: exactly one editable .pth
    # importing exactly the one finder present, and every absolute
    # /home path inside the finder must sit under prep.
    script_b = "\n".join(
        [
            "set -e",
            f'sp=$(ls -d {dest}/.bench-venv/lib/python3*/site-packages)',
            'pth=$(ls "$sp"/__editable__*.pth)',
            '[ "$(printf "%s" "$pth" | wc -l)" = "0" ]',
            'finder=$(ls "$sp"/__editable___*_finder.py)',
            '[ "$(printf "%s" "$finder" | wc -l)" = "0" ]',
            'mod=$(grep -o "__editable___[A-Za-z0-9_]*_finder" "$pth" | head -1)',
            '[ "$(basename "$finder" .py)" = "$mod" ]',
            f'grep -q "{prep}" "$finder"',
            "other=$(grep -o '/home[^ ]*' \"$finder\" | "
            f'grep -v "^{prep}" || true)',
            '[ -z "$other" ]',
            f'sed -i "s|{prep}|{dest}|g" "$finder"',
            f'grep -q "{prep}" "$finder" && exit 1 || true',
            'echo "fixed finder: $finder"',
        ]
    )
    exit_b, out_b, err_b_text = ssh_exec(cfg, script_b, timeout=TIMEOUT_SSH)
    if exit_b != 0:
        raise RuntimeError(
            f"venv .pth fixup failed for {dest}: A:[{err_a}] B:[{err_b_text[-300:]}] "
            f"(expected static prep-path .pth or prep-rooted finder)"
        )
    tee.log(f"stage: venv repointed ({out_b.strip()[-90:]})")


# ---------------------------------------------------------------------------
# Eval commands + base run
# ---------------------------------------------------------------------------


def pytest_eval_argv(node_ids: list[str], junit_name: str) -> list[str]:
    """Exact node-ID pytest argv (resolved IDs only, JUnit to file).

    Callers MUST pass IDs resolved against a `--collect-only` run in
    the same tree: manifest IDs can be whitespace-truncated (dataset
    quirk), and pytest exits 4 on unknown node IDs. The repo's own
    addopts are RESPECTED (never ``-o addopts=``): pytest's suite
    enables its ``pytester`` plugin via addopts.
    """
    return [
        "./.bench-venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--junitxml",
        junit_name,
        *node_ids,
    ]


def vitest_eval_argv(
    task: dict, junit_name: str, extra_specs: list[str] | None = None
) -> list[str]:
    """Spec-file vitest argv (F2P+P2P files, JUnit to ``junit_name``).

    Calls the real JS entrypoint (``vitest/vitest.mjs``) as a node
    ARGUMENT (never as container init): shebang-script init fails
    under runsc, while the node ELF entry point is reliable. The
    ``.bin/vitest`` pnpm shim is a SHELL script — node parses it as
    JS and dies with SyntaxError (Step 4.2 evidence, vuejs runs).
    """
    from bench_lib import eval_files_for

    files = eval_files_for(task)
    if extra_specs:
        files = [*files, *[s for s in extra_specs if s not in files]]
    return [
        "node",
        "./node_modules/vitest/vitest.mjs",
        "run",
        *files,
        "--reporter=junit",
        f"--outputFile={junit_name}",
    ]


def _collect_argv(task: dict) -> list[str]:
    """Shared ``pytest --collect-only`` argv (remote and local tiers)."""
    return [
        "./.bench-venv/bin/python", "-m", "pytest", "--collect-only",
        "-q", "-p", "no:cacheprovider", *eval_files_for(task),
    ]


def _archive_collect_streams(
    archive: Path | str | None, stdout: str, stderr: str, tee: Tee,
    stage: str,
) -> None:
    """Write full collect stdout/stderr to ``<stem>.out/.err`` (4.3b.7)."""
    if archive is None:
        return
    stem = Path(archive)
    stem.parent.mkdir(parents=True, exist_ok=True)
    stem.with_suffix(".out").write_text(stdout, encoding="utf-8")
    stem.with_suffix(".err").write_text(stderr, encoding="utf-8")
    tee.log(f"{stage}-collect: streams archived ({stem.name}.out/.err)")


def collect_ids_remote_full(
    cfg: SshConfig, task: dict, run_dir: str, tee: Tee, stage: str,
    image: str | None = None, archive: Path | str | None = None,
) -> tuple[list[str], dict]:
    """Collect full test IDs in a remote run dir (no execution).

    Returns ``(ids, report)``; the collect stderr is ALWAYS archived
    to the transcript (Step 4.3b.4/R7: the 4.2 2931 log proved a bare
    ``0 ID(s) (exit=2)`` line is undiagnosable). Step 4.3b.7 (R12):
    when ``archive`` (a path stem) is given, the FULL streams land in
    ``<stem>.out`` / ``<stem>.err``.
    """
    img = image or py_image_for(task)
    runner = oci_sandbox_runner(cfg, img)
    res = robust_run(
        runner, _collect_argv(task), run_dir, timeout_sec=TIMEOUT_EVAL,
        network_disabled=True, tee=tee, stage=f"{stage}-collect",
    )
    ids = parse_collect_output(res.stdout)
    tee.log(
        f"{stage}-collect: {len(ids)} ID(s) "
        f"(exit={res.exit_code} image={img})"
    )
    tee.blob(f"{stage}-collect stderr", res.stderr or "(empty)")
    _archive_collect_streams(archive, res.stdout, res.stderr, tee, stage)
    report = {
        "image": img,
        "exit_code": res.exit_code,
        "n_ids": len(ids),
        # Tails (not full streams): era signatures surface at the
        # tail; the transcript blob holds the wider archive.
        "stdout_tail": res.stdout[-4000:],
        "stderr_tail": res.stderr[-4000:],
    }
    return ids, report


def collect_ids_remote(
    cfg: SshConfig, task: dict, run_dir: str, tee: Tee, stage: str,
    archive: Path | str | None = None,
) -> list[str]:
    """Collect full test IDs in a remote run dir (no execution)."""
    ids, _ = collect_ids_remote_full(
        cfg, task, run_dir, tee, stage, archive=archive
    )
    return ids


def collect_ids_local_full(
    task: dict, run_dir: Path, tee: Tee, stage: str,
    archive: Path | str | None = None,
) -> tuple[list[str], dict]:
    """Collect full test IDs in a local run dir (no execution).

    Local mirror of :func:`collect_ids_remote_full` (stderr archived,
    report returned); no era fallback on the smoke-only local tier.
    """
    from hackproof.sandbox.runner import LocalSubprocessRunner

    res = robust_run(
        LocalSubprocessRunner(), _collect_argv(task), run_dir,
        timeout_sec=TIMEOUT_EVAL,
        network_disabled=True, tee=tee, stage=f"{stage}-collect[local]",
    )
    ids = parse_collect_output(res.stdout)
    tee.log(f"{stage}-collect[local]: {len(ids)} ID(s)")
    tee.blob(f"{stage}-collect[local] stderr", res.stderr or "(empty)")
    _archive_collect_streams(
        archive, res.stdout, res.stderr, tee, f"{stage}[local]"
    )
    report = {
        "image": "local",
        "exit_code": res.exit_code,
        "n_ids": len(ids),
        "stdout_tail": res.stdout[-4000:],
        "stderr_tail": res.stderr[-4000:],
    }
    return ids, report


def collect_ids_local(
    task: dict, run_dir: Path, tee: Tee, stage: str,
    archive: Path | str | None = None,
) -> list[str]:
    """Collect full test IDs in a local run dir (no execution)."""
    ids, _ = collect_ids_local_full(
        task, run_dir, tee, stage, archive=archive
    )
    return ids


# Era-failure signatures (Step 4.3b.4/R7): hard import-time deaths of
# pre-3.10 code on modern interpreters. Deliberately tight — a
# patch-broken collection (SyntaxError, ImportError of project
# modules) must NEVER match, or agent verdicts would be retried away.
_ERA_FAILURE_RES = (
    # requests<=2.9 structures.py: CaseInsensitiveDict(MutableMapping).
    re.compile(
        r"module 'collections' has no attribute "
        r"'(Mutable)?(Mapping|Sequence|Set|Iterable|Callable)'"
    ),
    re.compile(r"cannot import name '.*' from 'collections'"),
    # pytest<=6.3 assertion rewriter on 3.10+ AST.
    re.compile(r'required field "lineno" missing from alias'),
)


def should_era_fallback(
    image: str, exit_code: int, stdout: str, stderr: str, ids: list[str]
) -> str | None:
    """Return the era-fallback reason, or None when not applicable.

    Triggers only when the collect produced ZERO ids and (a) an era
    signature is present, or (b) the collect exited nonzero loudly.
    Exit-0-empty never triggers (genuinely empty selection must stay a
    loud UNMEASURABLE, not a silent image swap). Bounded: callers retry
    at most once, and never when already on :data:`ERA_PY_IMAGE`.
    """
    if image == ERA_PY_IMAGE or ids:
        return None
    blob = (stdout + "\n" + stderr)
    for rx in _ERA_FAILURE_RES:
        match = rx.search(blob)
        if match:
            return f"era-signature:{match.group(0)[:80]}"
    if exit_code != 0:
        return f"collect-exit-{exit_code}-empty"
    return None


def reprep_run_dir_era(
    cfg: SshConfig, task: dict, run_dir: str, tee: Tee
) -> None:
    """Rebuild a run dir venv under the era image (collect fallback).

    The stale venv is removed first: ``lib/python3.11/site-packages``
    is invisible to a 3.8 interpreter, so re-prepping over it would
    mix two interpreters' packages. Guard rails mirror
    :func:`ssh_rmtree` (runs root only).
    """
    runs_root = f"{remote_home(cfg)}/{OCI_RUNS_REL}/"
    if not run_dir.startswith(runs_root) or run_dir.rstrip("/") == (
        runs_root.rstrip("/")
    ):
        raise ValueError(f"refusing era reprep outside runs root: {run_dir}")
    tee.log(f"era-fallback: rebuilding {run_dir} venv on {ERA_PY_IMAGE} ...")
    exit_code, _, stderr = ssh_exec(
        cfg, f"set -e\nrm -rf {run_dir}/.bench-venv", timeout=TIMEOUT_SSH,
    )
    if exit_code != 0:
        raise RuntimeError(f"era reprep cleanup failed: {stderr[-400:]}")
    prep_dependencies(
        cfg, task, run_dir, tee, image=ERA_PY_IMAGE, stage="era-fallback",
    )


def refresh_shared_prep_era(
    cfg: SshConfig, task: dict, tee: Tee, reason: str
) -> None:
    """Rebuild the SHARED prep venv under the era image (fallback).

    Called after a base-run rescue so later A/B run dirs (copied from
    prep) inherit a working venv. The OK.json marker records the era
    image truthfully plus the fallback reason; the fingerprint then
    mismatches on purpose, so the NEXT matrix process re-validates via
    the same loud fallback instead of trusting a stale marker.
    """
    prep = remote_prep_dir(cfg, task["id"])
    runs_root = f"{remote_home(cfg)}/{OCI_RUNS_REL}/"
    if not prep.startswith(runs_root):
        raise ValueError(f"refusing era reprep outside runs root: {prep}")
    exit_code, _, stderr = ssh_exec(
        cfg, f"set -e\nrm -rf {prep}/.bench-venv", timeout=TIMEOUT_SSH,
    )
    if exit_code != 0:
        raise RuntimeError(f"era prep cleanup failed: {stderr[-400:]}")
    deps = prep_dependencies(
        cfg, task, prep, tee, image=ERA_PY_IMAGE, stage="era-fallback",
    )
    fingerprint = prep_fingerprint(task)
    fingerprint["image"] = ERA_PY_IMAGE
    marker = {
        "base_ref": task["base_ref"],
        "dataset_sha256": task["dataset_sha256"],
        "fingerprint": fingerprint,
        "deps": deps,
        "ts": utcnow_iso(),
        "era_fallback": {"reason": reason, "ts": utcnow_iso()},
    }
    rc = ssh_write_file(
        cfg, f"{prep}/OK.json", json.dumps(marker, indent=1).encode("utf-8"),
    )
    if rc != 0:
        raise RuntimeError("era prep marker write failed")
    tee.log("era-fallback: shared prep rebuilt on era image (marker set)")


def collect_ids_remote_with_fallback(
    cfg: SshConfig, task: dict, run_dir: str, tee: Tee, stage: str,
    archive: Path | str | None = None,
) -> tuple[list[str], dict]:
    """Collect with ONE bounded era retry (Step 4.3b.4/R7, base path).

    On an era-class collect failure the run-dir venv is rebuilt under
    :data:`ERA_PY_IMAGE`, the collect retried exactly once, the shared
    prep refreshed, and the task pinned to the era image for the rest
    of THIS matrix process (so A/B prep/collect/eval/red stay on one
    interpreter). The report records every step; agent-path verdicts
    never pass through here (a patch-broken collect must fail loud).
    The era retry archives under a ``.era-retry`` stem infix.
    """
    ids, report = collect_ids_remote_full(
        cfg, task, run_dir, tee, stage, archive=archive
    )
    reason = should_era_fallback(
        report["image"], report["exit_code"], report["stdout_tail"],
        report["stderr_tail"], ids,
    )
    report["fallback"] = None
    if reason is None:
        return ids, report
    tee.log(f"era-fallback: {reason} (image={report['image']}) — retrying once")
    reprep_run_dir_era(cfg, task, run_dir, tee)
    retry_archive = None
    if archive is not None:
        stem = Path(archive)
        retry_archive = stem.parent / f"{stem.name}.era-retry"
    retry_ids, retry_report = collect_ids_remote_full(
        cfg, task, run_dir, tee, f"{stage}-era-retry", image=ERA_PY_IMAGE,
        archive=retry_archive,
    )
    retry_report["fallback"] = {
        "reason": reason,
        "from_image": report["image"],
        "to_image": ERA_PY_IMAGE,
        "first_exit": report["exit_code"],
        "first_n_ids": report["n_ids"],
        "retry_n_ids": len(retry_ids),
    }
    if retry_ids:
        _ERA_PINNED_RUNTIME.add(task["id"])
        tee.log(
            f"era-fallback: rescued ({len(retry_ids)} IDs); "
            f"{task['id']} pinned to {ERA_PY_IMAGE} for this process"
        )
        refresh_shared_prep_era(cfg, task, tee, reason)
    else:
        tee.log("era-fallback: retry still empty — task stays UNMEASURABLE")
    return retry_ids, retry_report


def manifest_ids_of(task: dict) -> list[str]:
    """F2P+P2P manifest IDs in canonical order (deduplicated)."""
    ids: list[str] = []
    for raw in list(task.get("fail_to_pass", [])) + list(
        task.get("pass_to_pass", [])
    ):
        if raw not in ids:
            ids.append(raw)
    return ids


def flatten_resolution(resolution: dict[str, list[str]]) -> list[str]:
    """Resolved collected IDs in canonical manifest order (deduped)."""
    flat: list[str] = []
    for matches in resolution.values():
        for full in matches:
            if full not in flat:
                flat.append(full)
    return flat


def run_remote_eval(
    cfg: SshConfig,
    task: dict,
    remote_dir: str,
    tee: Tee,
    junit_name: str = EVAL_JUNIT_NAME,
    timeout: int = TIMEOUT_EVAL,
    extra_specs: list[str] | None = None,
    stage: str = "eval",
    pytest_ids: list[str] | None = None,
) -> tuple[object, bytes | None]:
    """Execute one air-gapped eval; return (result, junit_bytes|None).

    Python runs require ``pytest_ids`` resolved by ``collect_ids_remote``
    in the same tree (manifest IDs can be truncated); TypeScript runs
    whole spec files and self-resolves at score time.
    """
    if task["language"] == "python":
        if not pytest_ids:
            raise ValueError(f"{stage}: python eval needs resolved IDs")
        image = py_image_for(task)
        argv = pytest_eval_argv(pytest_ids, junit_name)
    else:
        image = TS_IMAGE
        argv = vitest_eval_argv(task, junit_name, extra_specs)
    runner = oci_sandbox_runner(cfg, image)
    tee.log(f"{stage}: {' '.join(argv[:6])} ... (network=none)")
    res = robust_run(
        runner, argv, remote_dir, timeout_sec=timeout,
        network_disabled=True, tee=tee, stage=stage,
    )
    tee.log(
        f"{stage}: exit={res.exit_code} timed_out={res.timed_out} "
        f"({res.duration_ms} ms)"
    )
    junit = ssh_read_file(cfg, f"{remote_dir}/{junit_name}")
    if junit is None:
        tee.log(f"{stage}: WARNING: no JUnit at {junit_name}")
    else:
        tee.log(f"{stage}: JUnit {len(junit)} bytes")
    if junit is None or res.exit_code != 0:
        tee.blob(f"{stage} stdout tail", res.stdout[-3000:])
        tee.blob(f"{stage} stderr tail", res.stderr[-3000:])
    return res, junit


_MISSING_MODULE_RE = re.compile(r"No module named '([A-Za-z0-9_.]+)'")
_PIP_ALIASES = {"yaml": "pyyaml", "PIL": "pillow", "attr": "attrs"}


def log_resolution(tee: Tee, stage: str, resolution: dict[str, list[str]]) -> None:
    """Log resolution shape (exact vs prefix vs ambiguous vs missing)."""
    exact = sum(1 for m in resolution.values() if len(m) == 1)
    multi = {k: v for k, v in resolution.items() if len(v) > 1}
    missing = [k for k, v in resolution.items() if not v]
    tee.log(
        f"{stage}: resolution {exact} exact, {len(multi)} ambiguous, "
        f"{len(missing)} unresolved"
    )
    for mid, matches in sorted(multi.items()):
        tee.log(f"{stage}: ambiguous {mid!r} -> {len(matches)} (last-wins)")


def base_eval_with_retry(
    cfg: SshConfig, task: dict, tee: Tee,
    artifacts: dict[str, Path] | None = None,
) -> tuple[dict[str, str], bytes, str, dict[str, list[str]], list[str], dict]:
    """Shared base eval (base + test_patch, no agent patch).

    Returns ``(manifest_index, junit, tail, resolution, unresolved,
    collect_report)``. Python IDs are collect-resolved in the base
    tree (truncated dataset IDs expand to collected full IDs);
    TypeScript self-resolves from the JUnit full names. Non-empty
    ``unresolved`` means the task is unmeasurable (fail-fast
    downstream, no LLM spent). Python runs get a bounded dependency
    backfill: when collection fails on a missing third-party module,
    the module is pip-installed into the run venv (logged) and the
    eval retried, at most 3 rounds. Python collects additionally get
    ONE bounded era retry (Step 4.3b.4). ``artifacts`` (Step 4.3b.7)
    receives the collect streams + pip freeze (python only).
    """
    cache_dir = PKG_ROOT / "benchmarks" / "results" / "harness_cache"
    harness = get_test_patch(task, cache_dir, tee)
    run_dir = fresh_run_dir(cfg, task, "base", tee)
    apply_text_patch(cfg, run_dir, harness["test_patch"], tee, "base")
    manifest_ids = manifest_ids_of(task)
    argv_ids: list[str] = []
    base_resolution: dict[str, list[str]] = {}
    collect_report: dict = {
        "image": TS_IMAGE if task["language"] != "python" else "",
        "fallback": None,
        "note": "typescript self-resolves from JUnit",
    }
    if task["language"] == "python":
        collect_stem = (
            artifacts.get("collect") if artifacts else None
        )
        collected, collect_report = collect_ids_remote_with_fallback(
            cfg, task, run_dir, tee, "base", archive=collect_stem
        )
        if artifacts is not None and "freeze" in artifacts:
            archive_venv_freeze_remote(
                cfg, task, run_dir, artifacts["freeze"], tee, "base"
            )
        base_resolution = resolve_manifest_ids(manifest_ids, collected)
        log_resolution(tee, "base", base_resolution)
        unresolved = [m for m, v in base_resolution.items() if not v]
        if unresolved:
            tee.log(f"base: UNMEASURABLE ({len(unresolved)} unresolved IDs)")
            return {}, b"", "", base_resolution, unresolved, collect_report
        argv_ids = flatten_resolution(base_resolution)
        tee.log(f"base: eval argv has {len(argv_ids)} resolved ID(s)")
    attempt_note = ""
    for attempt in range(4):
        res, junit = run_remote_eval(
            cfg, task, run_dir, tee, stage=f"base[try{attempt}]",
            pytest_ids=argv_ids or None,
        )
        missing = _missing_third_party(res.stderr + res.stdout)
        if (
            task["language"] == "python"
            and missing
            and attempt < 3
        ):
            attempt_note += f"; backfill:{missing}"
            pip_backfill(cfg, task, run_dir, missing, tee)
            continue
        if junit is None:
            raise RuntimeError(
                f"base eval produced no JUnit{attempt_note}: "
                f"exit={res.exit_code} timed_out={res.timed_out}"
            )
        full_index = index_results(task, junit, tee, "base")
        if task["language"] == "typescript":
            base_resolution = resolve_manifest_ids(
                manifest_ids, list(full_index)
            )
            log_resolution(tee, "base", base_resolution)
            unresolved = [m for m, v in base_resolution.items() if not v]
            if unresolved:
                tee.log(
                    f"base: UNMEASURABLE ({len(unresolved)} unresolved IDs)"
                )
                return {}, junit, "", base_resolution, unresolved, (
                    collect_report
                )
        base_index = remap_collected_index(base_resolution, full_index)
        tail = (res.stdout[-2000:] + "\n" + res.stderr[-2000:])
        if attempt_note:
            tee.log(f"base: dependency backfill{attempt_note} applied")
        return base_index, junit, tail, base_resolution, [], collect_report
    raise RuntimeError("unreachable base-eval fallthrough")


def _missing_third_party(output: str) -> str | None:
    """Extract a missing module name from collection errors, if any."""
    match = _MISSING_MODULE_RE.search(output)
    if not match:
        return None
    return match.group(1).split(".")[0]


def pip_backfill(
    cfg: SshConfig, task: dict, run_dir: str, module: str, tee: Tee
) -> None:
    """Pip-install one missing module into the run venv (network ON)."""
    package = _PIP_ALIASES.get(module, module)
    tee.log(f"base: backfill pip install {package} ...")
    runner = oci_sandbox_runner(cfg, py_image_for(task))
    res = robust_run(
        runner,
        ["./.bench-venv/bin/python", "-m", "pip", "install", "-q", package],
        run_dir,
        timeout_sec=600,
        network_disabled=False,
        tee=tee, stage="backfill",
    )
    tee.log(f"base: backfill exit={res.exit_code}")
    if res.exit_code != 0:
        tee.blob("backfill output", res.stdout + "\n" + res.stderr)


def archive_venv_freeze_remote(
    cfg: SshConfig, task: dict, run_dir: str, dest: Path, tee: Tee,
    stage: str = "base",
) -> None:
    """Capture ``pip freeze`` of the run venv into ``dest`` (4.3b.7/R12).

    Taken at collection time (the env that produced the IDs); later
    dependency backfills are logged in the transcript by name, so the
    freeze plus the transcript backfill lines describe the final env.
    A failing freeze still writes a note file (loud, never silent).
    """
    runner = oci_sandbox_runner(cfg, py_image_for(task))
    res = robust_run(
        runner, ["./.bench-venv/bin/python", "-m", "pip", "freeze", "--all"],
        run_dir, timeout_sec=TIMEOUT_SSH, network_disabled=True,
        tee=tee, stage=f"{stage}-freeze",
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if res.exit_code == 0 and res.stdout.strip():
        dest.write_text(res.stdout, encoding="utf-8")
    else:
        dest.write_text(
            f"# pip freeze failed (exit={res.exit_code}):\n"
            f"{res.stderr[-2000:]}",
            encoding="utf-8",
        )
    tee.log(f"{stage}: pip freeze archived ({dest.name})")


def archive_venv_freeze_local(
    run_dir: Path, dest: Path, tee: Tee, stage: str = "base"
) -> None:
    """Local-tier mirror of :func:`archive_venv_freeze_remote`."""
    from hackproof.sandbox.runner import LocalSubprocessRunner

    res = robust_run(
        LocalSubprocessRunner(),
        ["./.bench-venv/bin/python", "-m", "pip", "freeze", "--all"],
        run_dir, timeout_sec=TIMEOUT_SSH, network_disabled=True,
        tee=tee, stage=f"{stage}-freeze[local]",
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if res.exit_code == 0 and res.stdout.strip():
        dest.write_text(res.stdout, encoding="utf-8")
    else:
        dest.write_text(
            f"# pip freeze failed (exit={res.exit_code}):\n"
            f"{res.stderr[-2000:]}",
            encoding="utf-8",
        )
    tee.log(f"{stage}: pip freeze archived ({dest.name})")


def apply_text_patch(
    cfg: SshConfig, run_dir: str, patch_text: str, tee: Tee, stage: str
) -> None:
    """Apply a unified diff inside a remote run dir (host git)."""
    if not patch_text.strip():
        raise ValueError(f"{stage}: refusing to apply an empty patch")
    script = f"set -e\ngit -C {run_dir} apply --whitespace=nowarn -"
    exit_code, _, stderr = ssh_exec(
        cfg, script, timeout=TIMEOUT_SSH,
        input_bytes=patch_text.encode("utf-8"),
    )
    if exit_code != 0:
        raise RuntimeError(f"{stage}: git apply failed: {stderr[-800:]}")
    tee.log(f"{stage}: patch applied ({len(patch_text)} chars)")


def index_results(
    task: dict, junit_bytes: bytes, tee: Tee, stage: str
) -> dict[str, str]:
    """Parse a JUnit payload into the normalized ID->status map."""
    cases = parse_junit(junit_bytes)
    if task["language"] == "python":
        manifest_files = {
            test_id.split("::", 1)[0]
            for test_id in list(task.get("fail_to_pass", []))
            + list(task.get("pass_to_pass", []))
        }
        index = index_pytest_cases(cases, known_files=manifest_files)
    else:
        index = index_vitest_cases(cases)
    tee.log(f"{stage}: parsed {len(cases)} cases -> {len(index)} indexed")
    return index


# ---------------------------------------------------------------------------
# Local execution tier (smoke only — no gVisor isolation)
# ---------------------------------------------------------------------------


def local_run_dirs(work_dir: Path, task_id: str) -> dict[str, Path]:
    """Local prep/base/A/B dirs under the work root."""
    base = work_dir / task_id
    return {
        "clone": base / "clone",
        "prep": base / "prep",
        "base": base / "base",
        "A": base / "A",
        "B": base / "B",
    }


def local_prep(task: dict, work_dir: Path, tee: Tee) -> Path:
    """Prepare the local prep checkout (tree-verified clone + deps).

    Local tier has no container boundary, so dependency installs run
    as plain subprocesses. Only for 1-task smoke runs.
    """
    from hackproof.sandbox.runner import LocalSubprocessRunner

    dirs = local_run_dirs(work_dir, task["id"])
    clone = ensure_local_clone(task, work_dir / "_clones", tee)
    prep = dirs["prep"]
    if prep.exists():
        shutil.rmtree(prep, ignore_errors=True)
    shutil.copytree(clone, prep, ignore=shutil.ignore_patterns(".bench-venv"))
    repo_url = f"https://github.com/{task['repo']}.git"
    subprocess.run(
        ["git", "-C", str(prep), "remote", "add", "origin", repo_url],
        check=False, capture_output=True, timeout=TIMEOUT_SSH,
    )
    if task["repo"] in SCM_VERSIONED_REPOS:
        tee.log("prep[local]: unshallowing history + tags ...")
        subprocess.run(
            ["git", "-C", str(prep), "fetch", "--unshallow", "origin"],
            check=False, capture_output=True, timeout=900,
        )
        subprocess.run(
            ["git", "-C", str(prep), "fetch", "--tags", "origin"],
            check=False, capture_output=True, timeout=900,
        )
    runner = LocalSubprocessRunner()
    if task["language"] == "python":
        tee.log("prep[local]: venv + pytest + editable install ...")
        res = robust_run(
            runner,
            ["bash", "-lc",
             "python3 -m venv --without-pip .bench-venv && "
             ".bench-venv/bin/python -m ensurepip -q && "
             ".bench-venv/bin/pip install -q --upgrade pip && "
             ".bench-venv/bin/pip install -q pytest && "
             ".bench-venv/bin/pip install -q -e ."],
            prep, timeout_sec=TIMEOUT_PREP_PY, network_disabled=False,
            tee=tee, stage="prep[local]",
        )
        if res.exit_code != 0:
            tee.blob("prep[local] output", res.stdout + "\n" + res.stderr)
            raise RuntimeError("local python prep failed")
    else:
        tee.log("prep[local]: npm install ...")
        res = robust_run(
            runner,
            ["bash", "-lc", "npm install --no-audit --no-fund"],
            prep, timeout_sec=TIMEOUT_PREP_TS, network_disabled=False,
            tee=tee, stage="prep[local]",
        )
        if res.exit_code != 0:
            tee.blob("prep[local] output", res.stdout + "\n" + res.stderr)
            raise RuntimeError("local node prep failed")
    (prep / "OK.json").write_text(
        json.dumps(
            {
                "base_ref": task["base_ref"],
                "dataset_sha256": task["dataset_sha256"],
                "tier": "local",
            }
        ),
        encoding="utf-8",
    )
    tee.log(f"prep[local]: ready at {prep}")
    return prep


def local_fresh_run_dir(
    work_dir: Path, task: dict, label: str, tee: Tee
) -> Path:
    """Copy the local prep checkout into a per-stage run dir."""
    dirs = local_run_dirs(work_dir, task["id"])
    dest = dirs[label]
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(dirs["prep"], dest, symlinks=True)
    if task["language"] == "python":
        local_fixup_venv(dirs["prep"], dest, tee)
    tee.log(f"stage[local]: fresh run dir {label}/")
    return dest


def local_fixup_venv(prep: Path, dest: Path, tee: Tee) -> None:
    """Repoint a copied venv's editable .pth (local tier).

    Same relocatability fix as :func:`fixup_relocatable_venv`, via
    pathlib: the .pth must be exactly the single prep path.
    """
    site_packages = sorted(
        (dest / ".bench-venv").glob("lib/python3*/site-packages")
    )
    if len(site_packages) != 1:
        raise RuntimeError(
            f"local .pth fixup: expected 1 site-packages, "
            f"found {len(site_packages)}"
        )
    sp = site_packages[0]
    matches = sorted(sp.glob("__editable__*.pth"))
    if len(matches) != 1:
        raise RuntimeError(
            f"local .pth fixup: expected 1 editable .pth, "
            f"found {len(matches)}"
        )
    pth = matches[0]
    content = pth.read_text(encoding="utf-8").strip()
    if content in (f"{prep}/src", str(prep)):
        pth.write_text(content.replace(str(prep), str(dest)) + "\n",
                       encoding="utf-8")
        tee.log(f"stage[local]: venv repointed to {dest}")
        return
    # Layout B: finder module (same strict shape as the OCI fixup).
    finder_matches = sorted(sp.glob("__editable___*_finder.py"))
    if len(finder_matches) != 1:
        raise RuntimeError(
            f"local .pth fixup: unexpected content {content!r} "
            f"({len(finder_matches)} finders)"
        )
    finder = finder_matches[0]
    if finder.stem not in content:
        raise RuntimeError(
            f"local .pth fixup: .pth does not import {finder.stem}"
        )
    text = finder.read_text(encoding="utf-8")
    if str(prep) not in text:
        raise RuntimeError("local .pth fixup: finder lacks prep path")
    others = [
        m for m in re.findall(r"/home[^\"' ]*", text)
        if not m.startswith(str(prep))
    ]
    if others:
        raise RuntimeError(
            f"local .pth fixup: non-prep paths {others[:3]}"
        )
    finder.write_text(text.replace(str(prep), str(dest)), encoding="utf-8")
    tee.log(f"stage[local]: venv finder repointed to {dest}")


def local_apply_patch(run_dir: Path, patch_text: str, tee: Tee, stage: str) -> None:
    """Apply a unified diff inside a local run dir."""
    if not patch_text.strip():
        raise ValueError(f"{stage}: refusing to apply an empty patch")
    proc = subprocess.run(
        ["git", "-C", str(run_dir), "apply", "--whitespace=nowarn", "-"],
        input=patch_text.encode("utf-8"),
        capture_output=True,
        timeout=TIMEOUT_SSH,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{stage}: local git apply failed: "
            f"{proc.stderr.decode('utf-8', 'replace')[-800:]}"
        )
    tee.log(f"{stage}: patch applied ({len(patch_text)} chars)")


def local_run_eval(
    task: dict, run_dir: Path, tee: Tee,
    junit_name: str = EVAL_JUNIT_NAME, timeout: int = TIMEOUT_EVAL,
    extra_specs: list[str] | None = None, stage: str = "eval",
    pytest_ids: list[str] | None = None,
) -> tuple[object, bytes | None]:
    """Execute one local eval; return (result, junit_bytes|None)."""
    from hackproof.sandbox.runner import LocalSubprocessRunner

    if task["language"] == "python":
        if not pytest_ids:
            raise ValueError(f"{stage}: python eval needs resolved IDs")
        argv = pytest_eval_argv(pytest_ids, junit_name)
    else:
        import shlex

        argv = ["bash", "-lc",
                "export PATH=\"$PWD/node_modules/.bin:$PATH\" && vitest "
                + " ".join(
                    shlex.quote(a)
                    for a in vitest_eval_argv(task, junit_name,
                                             extra_specs)[2:]
                )]
    tee.log(f"{stage}[local]: {' '.join(argv[:5])} ...")
    res = robust_run(
        LocalSubprocessRunner(), argv, run_dir, timeout_sec=timeout,
        network_disabled=True, tee=tee, stage=f"{stage}[local]",
    )
    tee.log(
        f"{stage}[local]: exit={res.exit_code} timed_out={res.timed_out} "
        f"({res.duration_ms} ms)"
    )
    junit_path = run_dir / junit_name
    if not junit_path.exists():
        tee.log(f"{stage}[local]: WARNING: no JUnit at {junit_name}")
        tee.blob(f"{stage}[local] stdout", res.stdout[-3000:])
        tee.blob(f"{stage}[local] stderr", res.stderr[-3000:])
        return res, None
    junit = junit_path.read_bytes()
    tee.log(f"{stage}[local]: JUnit {len(junit)} bytes")
    if res.exit_code != 0:
        tee.blob(f"{stage}[local] stdout tail", res.stdout[-3000:])
        tee.blob(f"{stage}[local] stderr tail", res.stderr[-3000:])
    return res, junit


def git_status_names(repo: Path) -> tuple[list[str], list[str]]:
    """Parse local ``git status`` into (modified_or_deleted, untracked)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, timeout=TIMEOUT_SSH,
    )
    changed: list[str] = []
    untracked: list[str] = []
    if proc.returncode != 0:
        return changed, untracked
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        if code.strip() == "??":
            untracked.append(path)
        else:
            changed.append(path)
    return changed, untracked


def git_diff_text(repo: Path) -> str:
    """Return the tracked-files unified diff of a local repo."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff"],
        capture_output=True, text=True, timeout=TIMEOUT_SSH,
    )
    return proc.stdout if proc.returncode == 0 else ""


def git_reset_hard(repo: Path, ref: str) -> None:
    """Reset a local scratch clone to ``ref`` (tracked + untracked)."""
    subprocess.run(
        ["git", "-C", str(repo), "reset", "-q", "--hard", ref],
        capture_output=True, timeout=TIMEOUT_SSH,
    )
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fdq"],
        capture_output=True, timeout=TIMEOUT_SSH,
    )


# ---------------------------------------------------------------------------
# Agent phases (Mode A single-shot vs Mode B full pipeline)
# ---------------------------------------------------------------------------


class UsageCollector:
    """Accumulate per-call token usage across one task+mode run."""

    def __init__(self) -> None:
        self.per_call: list[dict[str, object]] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self._consumed: dict[int, int] = {}

    def drain(self, client: object, role: str, model: str) -> None:
        """Append unseen ``usage_log`` records labeled with role/model."""
        log = list(getattr(client, "usage_log", None) or [])
        start = self._consumed.get(id(client), 0)
        for record in log[start:]:
            prompt = int(record.get("prompt_tokens", 0))
            completion = int(record.get("completion_tokens", 0))
            total = int(record.get("total_tokens", 0))
            entry: dict[str, object] = {
                "role": role,
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }
            if record.get("finish_reason") is not None:
                entry["finish_reason"] = record["finish_reason"]
            self.per_call.append(entry)
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += total
        self._consumed[id(client)] = len(log)

    def extend_report(self, report: object) -> None:
        """Append debate-report token usage (already per-role labeled)."""
        for item in getattr(report, "token_usage", None) or []:
            entry: dict[str, object] = {
                "role": item.role,
                "model": item.model,
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens,
                "total_tokens": item.total_tokens,
            }
            reason = getattr(item, "finish_reason", None)
            if reason is not None:
                entry["finish_reason"] = reason
            self.per_call.append(entry)
            self.prompt_tokens += item.prompt_tokens
            self.completion_tokens += item.completion_tokens
            self.total_tokens += item.total_tokens


def retrieval_backend_label() -> str:
    """Probe which hybrid retrieval backend will serve localization."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return "hybrid-rrf-hash-cpu"
    return "hybrid-rrf-contriever-cpu"


def localize_for_task(
    task: dict, clone: Path, issue_text: str, tee: Tee
) -> tuple[list[str], list[str], int, str]:
    """Localize relevant files; return (snippets, files, ms, backend)."""
    import hackproof.cli as cli

    backend = retrieval_backend_label()
    tee.log(f"agent: localize via {backend} ...")
    snippets, files, elapsed_ms = cli.localize_issue(
        clone, issue_text, task["language"]
    )
    tee.log(
        f"agent: localized {len(files)} file(s) in {elapsed_ms} ms "
        f"(top: {', '.join(files[:3])})"
    )
    return snippets, files, elapsed_ms, backend


def red_test_dir_for(task: dict) -> str:
    """Repo-relative dir receiving the synthesized red test."""
    first = task["fail_to_pass"][0]
    if task["language"] == "python":
        from bench_lib import test_file_of_pytest_id

        parent = str(Path(test_file_of_pytest_id(first)).parent)
    else:
        from bench_lib import test_file_of_vitest_id

        parent = str(Path(test_file_of_vitest_id(first)).parent)
    return parent


def apply_blocks_to_candidates(
    task_id: str, clone: Path, files: list[str],
    routed: list[tuple[str | None, str, str]], guard_files: list[str],
    tee: Tee,
    retry_client: object | None = None,
    retry_issue: str = "",
    retry_context: str = "",
    retry_patch_text: str = "",
    usage: UsageCollector | None = None,
    retry_archive: Path | None = None,
) -> list[str]:
    """Apply routed (multi-file) or legacy (single-file) blocks.

    Headerless patches keep the exact legacy rule: all blocks must match
    in ONE of the non-guard localized files. Header-carrying patches
    route per file; guard files are rejected before any write. Returns
    the applied files in first-touch order.

    Step 4.3b.3 (R6): when ``retry_client`` is provided, a total
    failure triggers exactly ONE builder revision against the patcher
    ground truth (closest region / did-you-mean), re-extracted and
    re-attempted once; the extra call is drained into ``usage`` as
    ``ArchitecturalBuilder-retry`` and archived to ``retry_archive``.
    """
    from hackproof.debate.roles import architectural_builder
    from hackproof.patcher.workspace_patcher import (
        PatchApplicationError,
        WorkspacePatcher,
    )

    patcher = WorkspacePatcher()
    guard = set(guard_files)

    def _attempt(current: list[tuple[str | None, str, str]]) -> list[str]:
        tagged = [entry for entry in current if entry[0] is not None]
        if not tagged:
            blocks = [(search, replace) for _, search, replace in current]
            errors: list[str] = []
            safe = [c for c in files if c not in guard]
            for candidate in safe:
                try:
                    patcher.apply_blocks(clone, candidate, blocks)
                except (FileNotFoundError, PatchApplicationError) as exc:
                    errors.append(f"{candidate}: {exc}")
                    continue
                tee.log(
                    f"agent: patch applied ({len(blocks)} block(s) "
                    f"-> {candidate})"
                )
                return [candidate]
            skipped = sorted(set(files) - set(safe))
            detail = "; ".join(errors[:3])
            message = (
                f"{task_id}: patch matches no localized file exactly once"
            )
            if detail:
                message += f": {detail}"
            if skipped:
                message += f" ({len(skipped)} guard file(s) excluded)"
            raise PatchApplicationError(message)
        touched = sorted(
            {entry[0] for entry in tagged if entry[0] is not None}
        )
        violations = [rel for rel in touched if rel in guard]
        if violations:
            raise PatchApplicationError(
                f"{task_id}: routed patch touches guard files "
                "(rejected before apply): " + ", ".join(violations)
            )
        universe = [c for c in files if c not in guard]
        try:
            applied = patcher.apply_routed_blocks(
                clone, current, universe=universe
            )
        except (FileNotFoundError, PatchApplicationError) as exc:
            raise PatchApplicationError(
                f"{task_id}: routed patch rejected: {exc}"
            ) from exc
        applied_files = list(applied)
        tee.log(
            f"agent: patch applied ({len(current)} block(s) "
            f"-> {applied_files})"
        )
        return applied_files

    current = routed
    previous_text = retry_patch_text
    for attempt in (0, 1):
        try:
            return _attempt(current)
        except (FileNotFoundError, PatchApplicationError) as exc:
            if attempt == 1 or retry_client is None:
                raise
            tee.log(f"agent: patch missed ({exc}); single revision ...")
            revised = architectural_builder.revise_patch(
                retry_client,  # type: ignore[arg-type]
                issue_description=retry_issue,
                code_context=retry_context,
                previous_patch=previous_text,
                failure_report=str(exc)[:4000],
            )
            if usage is not None:
                usage.drain(
                    retry_client, "ArchitecturalBuilder-retry",
                    "muse-wonder-1.0",
                )
            if retry_archive is not None:
                retry_archive.write_text(revised, encoding="utf-8")
            current = architectural_builder.extract_routed_blocks(revised)
            previous_text = revised
            if not current:
                raise PatchApplicationError(
                    f"{task_id}: revision produced no complete "
                    "SEARCH/REPLACE blocks"
                ) from exc
            tee.log(
                f"agent: retrying with revised patch "
                f"({len(current)} block(s)) ..."
            )
    raise AssertionError("unreachable: retry loop always returns/raises")


def run_mode_a_agent(
    task: dict, clone: Path, agent_input: dict, prompt_dir: Path,
    tee: Tee, usage: UsageCollector,
) -> dict:
    """Mode A: ONE Wonder call, no red gate, no guard (baseline)."""
    import hackproof.cli as cli

    from hackproof.debate.roles import architectural_builder
    from hackproof.llm.meta_client import MetaAPIError, MetaClient

    issue_text = (
        f"{agent_input['problem_statement']}\n\n"
        f"{agent_input['hints_text']}"
    ).strip()
    snippets, files, elapsed_ms, backend = localize_for_task(
        task, clone, issue_text, tee
    )
    code_context, builder_code_context = cli.build_code_context(
        clone, snippets, files
    )
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "localize.json").write_text(
        json.dumps(
            {
                "files": files,
                "elapsed_ms": elapsed_ms,
                "backend": backend,
                "issue_sha256": sha256_text(issue_text),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    builder_prompt = (
        f"{architectural_builder.ROLE_SYSTEM_PROMPT}\n"
        "BUDGET: fit the ENTIRE response in 700 tokens; "
        "if several hunks are needed, output the most critical first.\n\n"
        f"ISSUE:\n{issue_text}\n\nCODE CONTEXT:\n{builder_code_context}\n\nROUND: 1"
    )
    (prompt_dir / "builder_prompt.txt").write_text(
        builder_prompt, encoding="utf-8"
    )
    tee.log(
        f"agent[A]: single-shot Wonder call "
        f"(prompt {len(builder_prompt)} chars) ..."
    )
    wonder = MetaClient("muse-wonder-1.0")
    try:
        patch_text = wonder.debate_round(
            architectural_builder.ROLE_NAME, builder_prompt, []
        )
    except MetaAPIError as exc:
        tee.log(f"agent[A]: API error ({exc}); retrying once ...")
        time.sleep(10)
        patch_text = wonder.debate_round(
            architectural_builder.ROLE_NAME, builder_prompt, []
        )
    usage.drain(wonder, "ArchitecturalBuilder-single", "muse-wonder-1.0")
    (prompt_dir / "patch_text.txt").write_text(patch_text, encoding="utf-8")
    routed = architectural_builder.extract_routed_blocks(patch_text)
    tee.log(f"agent[A]: {len(routed)} complete block(s) extracted")
    if not routed:
        return {
            "ok": False,
            "reason": "no complete SEARCH/REPLACE blocks",
            "files": files,
            "localize_ms": elapsed_ms,
            "backend": backend,
        }
    try:
        applied = apply_blocks_to_candidates(
            task["id"], clone, files, routed, guard_files_for(task), tee,
            retry_client=wonder, retry_issue=issue_text,
            retry_context=builder_code_context, retry_patch_text=patch_text,
            usage=usage,
            retry_archive=prompt_dir / "patch_text_retry.txt",
        )
    except Exception as exc:  # noqa: BLE001 (agent-fail, recorded)
        return {
            "ok": False,
            "reason": f"patch application failed: {exc}",
            "files": files,
            "localize_ms": elapsed_ms,
            "backend": backend,
        }
    return {
        "ok": True,
        "reason": "",
        "files": files,
        "localize_ms": elapsed_ms,
        "backend": backend,
        "patch_text": patch_text,
        "blocks": len(routed),
        "applied_file": applied[0],
        "applied_files": applied,
        "diff_text": git_diff_text(clone),
    }


def red_retry_feedback(red_record: dict) -> str:
    """Judge feedback for the single red resynthesis (Step 4.3b.6/R10).

    Quotes the machine judge's reason verbatim (e.g. the 10081-B
    ``red run exited 0``) so the retry addresses the observed verdict,
    not a guessed one.
    """
    return (
        f"Previous red test REJECTED: {red_record.get('reason', 'unknown')} "
        f"(exit={red_record.get('exit_code')}, "
        f"failures={red_record.get('failures')}, "
        f"errors={red_record.get('errors')}). "
        "Write a test that FAILS on the current code with an explicit "
        "assertion failure; no skips, no empty bodies, no mocks of the bug."
    )


def red_format_defect(data: bytes) -> str | None:
    """Machine defect quote for an unusable red body (Step 12a.1).

    Empty/whitespace-only synthesis output can never prove red
    (observed: 5787-B retry emitted 0 bytes); quote the defect for one
    grounded format retry instead of burning a judge round on it.
    Returns None when the body carries non-whitespace bytes.
    """
    if data.strip():
        return None
    return (
        f"red body unusable: {len(data)} bytes, "
        "0 non-whitespace characters"
    )


class DebatePromptRecorder:
    """Shared sink for exact debate prompts (Step 4.3b.7/R12).

    Role modules build their prompts internally, so the benchmark
    records them at the client boundary: :meth:`wrap` delegates every
    call while snapshotting ``(role, prompt, history)`` in call
    order across both clients (plus the final patch-synthesis
    input). Attribute reads (``model``, ``last_usage``) pass through
    untouched, so FinOps accounting is unaffected. Zero product
    changes — the orchestrator and roles are unaware of the wrap.
    """

    def __init__(self) -> None:
        self.turns: list[dict] = []

    def wrap(self, client: object) -> object:
        """Wrap one debate client with prompt recording."""
        recorder = self

        class _RecordingClient:
            def __init__(self, delegate: object) -> None:
                self._delegate = delegate

            def debate_round(
                self, role: str, prompt: str, history: list
            ) -> str:
                recorder.turns.append(
                    {
                        "role": role,
                        "prompt": prompt,
                        "history": [dict(entry) for entry in history],
                    }
                )
                return self._delegate.debate_round(  # type: ignore[attr-defined]
                    role, prompt, history
                )

            def synthesize_patch(self, consensus_text: str) -> str:
                recorder.turns.append(
                    {
                        "role": "PatchSynthesis",
                        "prompt": consensus_text,
                        "history": [],
                    }
                )
                return self._delegate.synthesize_patch(  # type: ignore[attr-defined]
                    consensus_text
                )

            def __getattr__(self, name: str) -> object:
                return getattr(self._delegate, name)

        return _RecordingClient(client)


def run_mode_b_agent(
    task: dict, clone: Path, agent_input: dict, prompt_dir: Path,
    tee: Tee, usage: UsageCollector,
    prove_red: Callable[[str, bytes], dict],
) -> dict:
    """Mode B: guard lock + proven red + debate + patch.

    ``prove_red`` executes a red candidate on the pristine run dir
    (tier-specific callback) and returns its record. Step 4.3b.6
    (R10): debates run ONLY after an accepted red — on rejection the
    red is resynthesized exactly once with judge feedback, and a
    second rejection fails the run before any debate spend.
    """
    import hackproof.cli as cli
    from hackproof.debate.orchestrator import DebateOrchestrator
    from hackproof.debate.roles import architectural_builder
    from hackproof.llm.meta_client import MetaAPIError, MetaClient
    from hackproof.synthesis.spark_synthesizer import (
        RegressionTestSynthesizer,
    )

    issue_text = (
        f"{agent_input['problem_statement']}\n\n"
        f"{agent_input['hints_text']}"
    ).strip()
    snippets, files, elapsed_ms, backend = localize_for_task(
        task, clone, issue_text, tee
    )
    code_context, builder_code_context = cli.build_code_context(
        clone, snippets, files
    )
    structure = cli.repo_structure_sketch(clone)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "localize.json").write_text(
        json.dumps(
            {
                "files": files,
                "elapsed_ms": elapsed_ms,
                "backend": backend,
                "issue_sha256": sha256_text(issue_text),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    guard_files = guard_files_for(task)
    baseline: dict[str, str] = {}
    for rel in guard_files:
        candidate = clone / rel
        if candidate.is_file():
            baseline[rel] = hashlib.sha256(
                candidate.read_bytes()
            ).hexdigest()
    tee.log(f"agent[B]: guard locked ({len(baseline)} file(s))")
    try:
        spark = MetaClient("muse-spark-1.3")
        wonder = MetaClient("muse-wonder-1.0")
    except Exception as exc:  # noqa: BLE001 (auth/env -> infra upstream)
        raise RuntimeError(f"client construction failed: {exc}") from exc
    spark_synth = RegressionTestSynthesizer(spark)
    wonder_synth = RegressionTestSynthesizer(wonder)
    red_out = clone / red_test_dir_for(task)
    red_out.mkdir(parents=True, exist_ok=True)

    def _synthesize(
        feedback: str | None, role: str, archive: str,
        synth: RegressionTestSynthesizer, client: object, model: str,
    ) -> tuple[str, bytes]:
        try:
            path = synth.synthesize_test(
                issue_description=issue_text,
                # Step 12a.1: full top-1 file context (was 12k snippets).
                code_snippets=builder_code_context,
                repo_structure=structure,
                language=task["language"],
                output_dir=red_out,
                feedback=feedback,
            )
        except MetaAPIError as exc:
            tee.log(f"agent[B]: red synthesis API error ({exc}); retrying ...")
            time.sleep(10)
            path = synth.synthesize_test(
                issue_description=issue_text,
                code_snippets=builder_code_context,
                repo_structure=structure,
                language=task["language"],
                output_dir=red_out,
                feedback=feedback,
            )
        usage.drain(client, role, model)
        rel = str(path.relative_to(clone))
        data = path.read_bytes()
        tee.log(f"agent[B]: red test synthesized at {rel}")
        (prompt_dir / archive).write_bytes(data)
        return rel, data

    red_rel, red_bytes = _synthesize(
        None, "SparkRedSynthesis", "red_test.txt",
        spark_synth, spark, "muse-spark-1.3",
    )
    red_record = prove_red(red_rel, red_bytes)
    if not red_record.get("accepted"):
        tee.log(
            f"agent[B]: red rejected ({red_record.get('reason')}); "
            "resynthesizing once with judge feedback ..."
        )
        # Step 12a.1: Wonder escalation for the resynthesis (was Spark).
        judge_feedback = red_retry_feedback(red_record)
        red_rel, red_bytes = _synthesize(
            judge_feedback, "WonderRedSynthesis-retry",
            "red_test_retry.txt",
            wonder_synth, wonder, "muse-wonder-1.0",
        )
        # Step 12a.1: format guard — empty output gets one grounded
        # retry with a machine defect quote instead of a judge round.
        defect = red_format_defect(red_bytes)
        if defect is not None:
            tee.log(
                f"agent[B]: red retry format defect ({defect}); "
                "one format retry ..."
            )
            red_rel, red_bytes = _synthesize(
                judge_feedback + "\n\nFORMAT DEFECT — FIX IT:\n" + defect,
                "WonderRedSynthesis-retry-format",
                "red_test_retry_format.txt",
                wonder_synth, wonder, "muse-wonder-1.0",
            )
        red_record = prove_red(red_rel, red_bytes)
        if not red_record.get("accepted"):
            return {
                "ok": False,
                "reason": "red-rejected after resynthesis: "
                f"{red_record.get('reason', '')}",
                "files": files,
                "localize_ms": elapsed_ms,
                "backend": backend,
                "red_rel": red_rel,
                "red_record": red_record,
                "debate": {
                    "rounds_used": 0,
                    "converged": False,
                    "score": 0.0,
                    "duration_ms": 0,
                },
            }
    prompt_recorder = DebatePromptRecorder()
    orchestrator = DebateOrchestrator(
        prompt_recorder.wrap(spark), prompt_recorder.wrap(wonder)
    )
    try:
        report = orchestrator.run(
            issue_description=issue_text,
            code_context=code_context,
            repo_structure=structure,
            builder_code_context=builder_code_context,
        )
    except MetaAPIError as exc:
        tee.log(f"agent[B]: debate API error ({exc}); retrying once ...")
        time.sleep(10)
        report = orchestrator.run(
            issue_description=issue_text,
            code_context=code_context,
            repo_structure=structure,
            builder_code_context=builder_code_context,
        )
    usage.extend_report(report)
    (prompt_dir / "debate_prompts.json").write_text(
        json.dumps({"turns": prompt_recorder.turns}, indent=1),
        encoding="utf-8",
    )
    tee.log(
        f"agent[B]: debate prompts archived "
        f"({len(prompt_recorder.turns)} turn(s))"
    )
    tee.log(
        f"agent[B]: debate converged={report.converged} "
        f"rounds={report.rounds_used} score={report.convergence_score:.2f} "
        f"tokens={report.total_tokens} duration={report.duration_ms} ms"
    )
    (prompt_dir / "debate.json").write_text(
        json.dumps(
            {
                "rounds_used": report.rounds_used,
                "converged": report.converged,
                "convergence_score": report.convergence_score,
                "duration_ms": report.duration_ms,
                "verdicts": report.verdicts,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (prompt_dir / "patch_text.txt").write_text(
        report.patch_candidate, encoding="utf-8"
    )
    routed = architectural_builder.extract_routed_blocks(report.patch_candidate)
    tee.log(f"agent[B]: {len(routed)} complete block(s) extracted")
    if not routed:
        return {
            "ok": False,
            "reason": "debate produced no complete SEARCH/REPLACE blocks",
            "files": files,
            "localize_ms": elapsed_ms,
            "backend": backend,
            "red_rel": red_rel,
            "red_record": red_record,
            "evidence": {"debate_prompts": True},
            "debate": {
                "rounds_used": report.rounds_used,
                "converged": report.converged,
                "score": report.convergence_score,
                "duration_ms": report.duration_ms,
            },
        }
    try:
        applied = apply_blocks_to_candidates(
            task["id"], clone, files, routed, guard_files_for(task), tee,
            retry_client=wonder, retry_issue=issue_text,
            retry_context=builder_code_context,
            retry_patch_text=report.patch_candidate,
            usage=usage,
            retry_archive=prompt_dir / "patch_text_retry.txt",
        )
    except Exception as exc:  # noqa: BLE001 (agent-fail, recorded)
        return {
            "ok": False,
            "reason": f"patch application failed: {exc}",
            "files": files,
            "localize_ms": elapsed_ms,
            "backend": backend,
            "red_rel": red_rel,
            "red_record": red_record,
            "evidence": {"debate_prompts": True},
            "debate": {
                "rounds_used": report.rounds_used,
                "converged": report.converged,
                "score": report.convergence_score,
                "duration_ms": report.duration_ms,
            },
        }
    local_violations = verify_local_guard(clone, baseline)
    return {
        "ok": True,
        "reason": "",
        "files": files,
        "localize_ms": elapsed_ms,
        "backend": backend,
        "patch_text": report.patch_candidate,
        "blocks": len(routed),
        "applied_file": applied[0],
        "applied_files": applied,
        "diff_text": git_diff_text(clone),
        "red_rel": red_rel,
        "red_bytes": red_bytes,
        "red_record": red_record,
        "evidence": {"debate_prompts": True},
        "baseline": baseline,
        "local_violations": local_violations,
        # Step 4.3c.1 (R9): in-memory revision context, excluded from
        # the persisted record (already model-visible, no harness).
        "issue_text": issue_text,
        "builder_code_context": builder_code_context,
        "debate": {
            "rounds_used": report.rounds_used,
            "converged": report.converged,
            "score": report.convergence_score,
            "duration_ms": report.duration_ms,
        },
    }


def verify_local_guard(
    clone: Path, baseline: dict[str, str]
) -> list[str]:
    """Compare working-tree guard files against the locked baseline."""
    violations: list[str] = []
    for rel, locked in baseline.items():
        candidate = clone / rel
        if not candidate.exists():
            violations.append(f"deleted: {rel}")
            continue
        current = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if current != locked:
            violations.append(f"modified: {rel}")
    return violations


def classify_remote_guard(
    task: dict, clone: Path, changed: list[str], tee: Tee
) -> list[str]:
    """Map remote changed paths to guard violations with details."""

    guard_set = set(guard_files_for(task))
    violations: list[str] = []
    for rel in sorted(set(changed)):
        top = rel.split("/", 1)[0]
        if top in SCRATCH_NAMES:
            continue
        if rel not in guard_set:
            continue
        candidate = clone / rel
        detail = "modified"
        if not candidate.exists():
            detail = "deleted"
        violations.append(f"{detail}: {rel}")
    if violations:
        tee.log(f"guard: {len(violations)} violation(s): {violations[:5]}")
    else:
        tee.log("guard: clean (no guard files touched)")
    return violations


def remote_changed_paths(cfg: SshConfig, run_dir: str) -> list[str]:
    """List remote changed + untracked paths (porcelain)."""
    script = f"git -C {run_dir} status --porcelain"
    exit_code, stdout, _ = ssh_exec(cfg, script, timeout=TIMEOUT_SSH)
    if exit_code != 0:
        return []
    changed: list[str] = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        changed.append(line[3:].strip().strip('"'))
    return changed


def reset_remote_test_files(
    cfg: SshConfig, task: dict, run_dir: str, changed: list[str], tee: Tee
) -> None:
    """Reset agent-touched test files to base (pre-test_patch hygiene).

    Mirrors the official SWE-bench eval_script: agent modifications under
    test files are discarded before the harness test_patch is applied,
    so the functional measurement is never polluted by test edits.
    """
    guard_set = set(guard_files_for(task))
    touched = sorted(set(changed) & guard_set)
    if not touched:
        tee.log("hygiene: no test files touched, nothing to reset")
        return
    quoted = " ".join(f"'{p}'" for p in touched)
    script = (
        f"set -e\ngit -C {run_dir} checkout {task['base_ref']} -- {quoted}"
    )
    exit_code, _, stderr = ssh_exec(cfg, script, timeout=TIMEOUT_SSH)
    if exit_code != 0:
        raise RuntimeError(f"test reset failed: {stderr[-500:]}")
    tee.log(f"hygiene: reset {len(touched)} test file(s) to base")


# ---------------------------------------------------------------------------
# Orchestration: one task+mode run
# ---------------------------------------------------------------------------


def result_paths(out_dir: Path, mode: str, task_id: str) -> dict[str, Path]:
    """Artifact paths for one run (``mode`` in A/B/base)."""
    return {
        "log": out_dir / "raw_logs" / mode / f"{task_id}.log",
        "junit": out_dir / "junit_reports" / mode / f"{task_id}.xml",
        "result": out_dir / "results" / mode / f"{task_id}.json",
        "diff": out_dir / "diffs" / mode / f"{task_id}.diff",
        "guard": out_dir / "guard_reports" / mode / f"{task_id}.json",
        "prompts": out_dir / "prompts" / mode / task_id,
        # Step 4.3b.7 (R12): "collect" is a stem — writers append
        # .out/.err (era retries use a .era-retry infix).
        "collect": out_dir / "collect_reports" / mode / task_id,
        "freeze": out_dir / "versions" / mode / f"{task_id}.pip-freeze.txt",
    }


def load_done_result(out_dir: Path, mode: str, task_id: str) -> dict | None:
    """Return the recorded result when a run is finished (done=true)."""
    path = result_paths(out_dir, mode, task_id)["result"]
    if not path.exists():
        return None
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if isinstance(recorded, dict) and recorded.get("done") is True:
        return recorded
    return None


def ledger_burn_check(ledger_path: Path, tee: Tee) -> None:
    """Log priced spend vs budget (entries are honestly unpriced)."""
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tee.log("finops: no ledger yet (first live run)")
        return
    spent = ledger.get("total_spent_usd", 0.0)
    budget = ledger.get("budget_usd", 150.0)
    entries = ledger.get("entries", [])
    tee.log(
        f"finops: spent=${spent:.2f} (priced) / budget=${budget:.2f}; "
        f"{len(entries)} unpriced live entries recorded"
    )


# Wave label for Gate cost attribution (historic prefixes immutable;
# Wave 2 rows must not masquerade as Wave 1 / Step 4.2).
BENCH_WAVE_LABEL = "bench4.3c-wave2"


def ledger_append_run(
    ledger_path: Path, task_id: str, mode: str, usage: UsageCollector,
    rounds_used: int, converged: bool, tee: Tee,
) -> None:
    """Append one task+mode token entry (skipped when zero calls)."""
    if not usage.per_call:
        tee.log("finops: no LLM calls, ledger untouched")
        return
    import hackproof.cli as cli

    cli.append_finops_entry(
        ledger_path,
        f"{BENCH_WAVE_LABEL}:{task_id}:{mode}",
        rounds_used=rounds_used,
        converged=converged,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        per_call=[
            {
                k: call[k]
                for k in (
                    "role", "model", "prompt_tokens",
                    "completion_tokens", "total_tokens", "finish_reason",
                )
                if k in call
            }
            for call in usage.per_call
        ],
    )
    tee.log(
        f"finops: ledger += {usage.total_tokens} tokens "
        f"({len(usage.per_call)} calls, unpriced)"
    )


def ensure_base_run(
    cfg: SshConfig | None, task: dict, out_dir: Path, local: bool,
    work_dir: Path,
) -> tuple[dict[str, str], bytes, dict]:
    """Return the shared base (manifest_index, junit, record)."""
    paths = result_paths(out_dir, "base", task["id"])
    if paths["result"].exists():
        try:
            recorded = json.loads(paths["result"].read_text(encoding="utf-8"))
            if recorded.get("done") is True:
                junit = (
                    paths["junit"].read_bytes()
                    if paths["junit"].exists() else b""
                )
                return recorded["base_index"], junit, recorded
        except (ValueError, KeyError):
            pass
    tee = Tee(paths["log"])
    try:
        if local:
            base_out = local_base_run(
                task, work_dir, out_dir, tee, artifacts=paths
            )
        else:
            assert cfg is not None
            base_out = base_eval_with_retry(
                cfg, task, tee, artifacts=paths
            )
        base_index, junit, _, resolution, unresolved, collect = base_out
        if junit:
            paths["junit"].parent.mkdir(parents=True, exist_ok=True)
            paths["junit"].write_bytes(junit)
        is_py = task["language"] == "python"
        recorded = {
            "task": task["id"],
            "mode": "base",
            "done": True,
            "ts": utcnow_iso(),
            "base_ref": task["base_ref"],
            "tier": "local" if local else "oci",
            "base_index": base_index,
            "id_resolution": resolution,
            "unresolved_ids": unresolved,
            "junit_sha256": sha256_bytes(junit) if junit else "",
            # Step 4.3b.4: collect evidence (image, exit, stderr tails,
            # era-fallback chain). Grandfathered 4.2 records lack it.
            "collect": collect,
            # Step 4.3b.7: evidence bundle flags (python base runs
            # always collect + freeze, unconditionally archived above).
            "evidence": {
                "language": task["language"],
                "collect": is_py,
                "freeze": is_py,
                "debate_prompts": False,
            },
        }
        paths["result"].parent.mkdir(parents=True, exist_ok=True)
        paths["result"].write_text(json.dumps(recorded, indent=1),
                                   encoding="utf-8")
        tee.log(f"base: recorded ({len(base_index)} indexed cases)")
        return base_index, junit, recorded
    finally:
        tee.close()


def local_base_run(
    task: dict, work_dir: Path, out_dir: Path, tee: Tee,
    artifacts: dict[str, Path] | None = None,
) -> tuple[dict[str, str], bytes, str, dict[str, list[str]], list[str], dict]:
    """Shared base eval on the local tier (mirrors OCI flow)."""
    cache_dir = out_dir / "harness_cache"
    harness = get_test_patch(task, cache_dir, tee)
    run_dir = local_fresh_run_dir(work_dir, task, "base", tee)
    local_apply_patch(run_dir, harness["test_patch"], tee, "base")
    manifest_ids = manifest_ids_of(task)
    argv_ids: list[str] = []
    base_resolution: dict[str, list[str]] = {}
    collect_report: dict = {
        "image": "local-ts" if task["language"] != "python" else "local",
        "fallback": None,
        "note": "typescript self-resolves from JUnit",
    }
    if task["language"] == "python":
        collect_stem = (
            artifacts.get("collect") if artifacts else None
        )
        collected, collect_report = collect_ids_local_full(
            task, run_dir, tee, "base", archive=collect_stem
        )
        if artifacts is not None and "freeze" in artifacts:
            archive_venv_freeze_local(
                run_dir, artifacts["freeze"], tee, "base"
            )
        base_resolution = resolve_manifest_ids(manifest_ids, collected)
        log_resolution(tee, "base", base_resolution)
        unresolved = [m for m, v in base_resolution.items() if not v]
        if unresolved:
            return {}, b"", "", base_resolution, unresolved, collect_report
        argv_ids = flatten_resolution(base_resolution)
    res, junit = local_run_eval(
        task, run_dir, tee, stage="base", pytest_ids=argv_ids or None
    )
    if junit is None:
        raise RuntimeError(
            f"local base eval produced no JUnit: exit={res.exit_code}"
        )
    full_index = index_results(task, junit, tee, "base")
    if task["language"] == "typescript":
        base_resolution = resolve_manifest_ids(manifest_ids, list(full_index))
        log_resolution(tee, "base", base_resolution)
        unresolved = [m for m, v in base_resolution.items() if not v]
        if unresolved:
            return {}, junit, "", base_resolution, unresolved, collect_report
    base_index = remap_collected_index(base_resolution, full_index)
    tail = (res.stdout[-2000:] + "\n" + res.stderr[-2000:])
    return base_index, junit, tail, base_resolution, [], collect_report


def run_red_remote(
    cfg: SshConfig, task: dict, run_dir: str, red_rel: str,
    red_bytes: bytes, tee: Tee,
) -> dict:
    """Ship the red test to the run dir and prove it fails (air-gapped)."""
    rc = ssh_write_file(cfg, f"{run_dir}/{red_rel}", red_bytes)
    if rc != 0:
        raise RuntimeError("red test upload failed")
    tee.log(f"red: uploaded {red_rel} ({len(red_bytes)} bytes)")
    image = py_image_for(task) if task["language"] == "python" else TS_IMAGE
    runner = oci_sandbox_runner(cfg, image)
    if task["language"] == "python":
        argv = [
            "./.bench-venv/bin/python", "-m", "pytest", "-q",
            "-p", "no:cacheprovider",
            "--junitxml", RED_JUNIT_NAME, red_rel,
        ]
    else:
        argv = [
            "node", "./node_modules/vitest/vitest.mjs", "run", red_rel,
            "--reporter=junit", f"--outputFile={RED_JUNIT_NAME}",
        ]
    res = robust_run(
        runner, argv, run_dir, timeout_sec=TIMEOUT_RED,
        network_disabled=True, tee=tee, stage="red",
    )
    junit = ssh_read_file(cfg, f"{run_dir}/{RED_JUNIT_NAME}") or b""
    verdict = judge_red(res.exit_code, junit, res.timed_out)
    tee.log(f"red: {verdict['reason']} (exit={res.exit_code})")
    return {
        "rel": red_rel,
        "sha256": sha256_bytes(red_bytes),
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
        "accepted": bool(verdict["accepted"]),
        "reason": str(verdict["reason"]),
        "failures": int(verdict["failures"]),
        "errors": int(verdict["errors"]),
        "junit_bytes": len(junit),
    }


def run_red_local(
    task: dict, run_dir: Path, red_rel: str, red_bytes: bytes, tee: Tee
) -> dict:
    """Local-tier red proof (same acceptance rule)."""
    from hackproof.sandbox.runner import LocalSubprocessRunner

    target = run_dir / red_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(red_bytes)
    if task["language"] == "python":
        argv = [
            "./.bench-venv/bin/python", "-m", "pytest", "-q",
            "-p", "no:cacheprovider",
            "--junitxml", RED_JUNIT_NAME, red_rel,
        ]
    else:
        argv = ["bash", "-lc",
                "export PATH=\"$PWD/node_modules/.bin:$PATH\" && vitest run "
                f"{red_rel} --reporter=junit --outputFile={RED_JUNIT_NAME}"]
    res = robust_run(
        LocalSubprocessRunner(), argv, run_dir, timeout_sec=TIMEOUT_RED,
        network_disabled=True, tee=tee, stage="red[local]",
    )
    junit = (run_dir / RED_JUNIT_NAME).read_bytes() if (
        run_dir / RED_JUNIT_NAME
    ).exists() else b""
    verdict = judge_red(res.exit_code, junit, res.timed_out)
    tee.log(f"red[local]: {verdict['reason']} (exit={res.exit_code})")
    return {
        "rel": red_rel,
        "sha256": sha256_bytes(red_bytes),
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
        "accepted": bool(verdict["accepted"]),
        "reason": str(verdict["reason"]),
        "failures": int(verdict["failures"]),
        "errors": int(verdict["errors"]),
        "junit_bytes": len(junit),
    }


def archive_red_junit_remote(
    cfg: SshConfig, out_dir: Path, mode: str, task_id: str,
    run_dir: str, red_record: dict, tee: Tee,
) -> None:
    """Archive the proof-run red JUnit left in the run dir.

    Step 4.3b.6: the red proof runs once (inside the agent, before
    debates); the orchestrator only archives its leftover JUnit.
    """
    red_junit = ssh_read_file(cfg, f"{run_dir}/{RED_JUNIT_NAME}")
    if red_junit:
        red_path = out_dir / "junit_reports" / mode / (
            f"{task_id}.red.xml"
        )
        red_path.parent.mkdir(parents=True, exist_ok=True)
        red_path.write_bytes(red_junit)
        red_record["junit_sha256"] = sha256_bytes(red_junit)
        tee.log(f"red: JUnit archived ({len(red_junit)} bytes)")


def run_task_mode_oci(
    cfg: SshConfig, config: BenchConfig, task: dict, manifest_note: str = ""
) -> dict:
    """Execute one task+mode run on the OCI tier; return the summary row."""
    from hackproof.llm.meta_client import MetaAPIError, MetaAuthError

    out_dir = config.out_dir
    task_id, mode = task["id"], config.mode
    paths = result_paths(out_dir, mode, task_id)
    tee = Tee(paths["log"])
    usage = UsageCollector()
    summary: dict = {"task": task_id, "mode": mode, "tier": "oci"}
    try:
        tee.log(f"== Step 4.2 task {task_id} mode {mode} (oci) {manifest_note}")
        ledger_burn_check(config.ledger_path, tee)
        agent_input = get_agent_input(task, out_dir / "harness_cache", tee)
        fingerprint = prep_fingerprint(task)
        if not prep_is_fresh(cfg, task, fingerprint, tee):
            prep = ssh_prep_checkout(cfg, task, tee)
            deps = prep_dependencies(cfg, task, prep, tee)
            marker = {
                "base_ref": task["base_ref"],
                "dataset_sha256": task["dataset_sha256"],
                "fingerprint": fingerprint,
                "deps": deps,
                "ts": utcnow_iso(),
            }
            rc = ssh_write_file(
                cfg, f"{prep}/OK.json",
                json.dumps(marker, indent=1).encode("utf-8"),
            )
            if rc != 0:
                raise RuntimeError("prep marker write failed")
            tee.log("prep: marker written")
        base_index, _, base_record = ensure_base_run(
            cfg, task, out_dir, False, config.work_dir
        )
        base_resolution = base_record.get("id_resolution", {})
        if base_record.get("unresolved_ids"):
            record = {
                "task": task_id, "mode": mode, "tier": "oci",
                "ts": utcnow_iso(), "status": "failed", "done": True,
                "reason": "unmeasurable: unresolved test IDs: "
                + ", ".join(base_record["unresolved_ids"][:5]),
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "per_call": []},
            }
            tee.log("agent: skipped (task unmeasurable, no LLM spent)")
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        clone = ensure_local_clone(task, config.work_dir / "_clones", tee)
        git_reset_hard(clone, task["base_ref"])
        run_dir = fresh_run_dir(cfg, task, mode, tee)
        if mode == "A":
            agent = run_mode_a_agent(
                task, clone, agent_input, paths["prompts"], tee, usage
            )
            rounds_used, converged = 0, bool(agent.get("ok"))
        else:
            agent = run_mode_b_agent(
                task, clone, agent_input, paths["prompts"], tee, usage,
                prove_red=lambda rel, data: run_red_remote(
                    cfg, task, run_dir, rel, data, tee
                ),
            )
            debate = agent.get("debate", {})
            rounds_used = int(debate.get("rounds_used", 0))
            converged = bool(debate.get("converged", False))
        summary["agent_ok"] = bool(agent.get("ok"))
        summary["agent_reason"] = agent.get("reason", "")
        record: dict = {
            "task": task_id,
            "mode": mode,
            "tier": "oci",
            "ts": utcnow_iso(),
            "base_ref": task["base_ref"],
            "agent": {k: v for k, v in agent.items()
                      if k not in ("patch_text", "diff_text", "red_bytes",
                                   "red_record", "baseline",
                                   "evidence", "issue_text",
                                   "builder_code_context")},
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "per_call": usage.per_call,
            },
            # Step 4.3b.7: evidence flags flip to True as each bundle
            # artifact lands (collect/freeze below; debate prompts in
            # the agent). Absent block = grandfathered 4.2 record.
            "evidence": {
                "language": task["language"],
                "collect": False,
                "freeze": False,
                "debate_prompts": bool(
                    agent.get("evidence", {}).get("debate_prompts", False)
                ),
            },
        }
        if not agent.get("ok"):
            record.update(
                {"status": "failed", "reason": agent.get("reason", ""),
                 "done": True}
            )
            if agent.get("red_record") is not None:
                record["red"] = agent["red_record"]
                archive_red_junit_remote(
                    cfg, out_dir, mode, task_id, run_dir,
                    agent["red_record"], tee,
                )
            ledger_append_run(
                config.ledger_path, task_id, mode, usage, rounds_used,
                converged, tee,
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        diff_text = agent.get("diff_text", "")
        paths["diff"].parent.mkdir(parents=True, exist_ok=True)
        paths["diff"].write_text(diff_text, encoding="utf-8")
        record["diff_sha256"] = sha256_text(diff_text)
        red_record: dict | None = None
        if mode == "B":
            # Step 4.3b.6: red was proved inside the agent (before any
            # debate); consume its record, archive the leftover JUnit.
            red_record = agent.get("red_record") or {}
            archive_red_junit_remote(
                cfg, out_dir, mode, task_id, run_dir, red_record, tee
            )
            record["red"] = red_record
            if not red_record.get("accepted"):
                record.update(
                    {"status": "failed", "reason":
                     "red-rejected: "
                     f"{red_record.get('reason', 'red proof missing')}",
                     "done": True}
                )
                ledger_append_run(
                    config.ledger_path, task_id, mode, usage, rounds_used,
                    converged, tee,
                )
                return finalize_run(out_dir, mode, task_id, record, summary,
                                    tee)
        if diff_text.strip():
            apply_text_patch(cfg, run_dir, diff_text, tee, f"agent[{mode}]")
        else:
            tee.log(f"agent[{mode}]: empty diff (no-op patch)")
        changed = remote_changed_paths(cfg, run_dir)
        tee.log(f"agent[{mode}]: {len(changed)} changed path(s) remote")
        if mode == "B":
            local_violations = agent.get("local_violations", [])
            remote_violations = classify_remote_guard(
                task, clone, changed, tee
            )
            if sorted(local_violations) != sorted(remote_violations):
                record.update(
                    {"status": "infra_error", "done": False,
                     "reason": "guard divergence local vs remote: "
                     f"{local_violations} vs {remote_violations}"}
                )
                return finalize_run(out_dir, mode, task_id, record, summary,
                                    tee)
            guard_report: dict = {
                "task": task_id,
                "mode": mode,
                "binding": True,
                "verdict": "tamper" if remote_violations else "clean",
                "violations": remote_violations,
            }
            paths["guard"].parent.mkdir(parents=True, exist_ok=True)
            paths["guard"].write_text(json.dumps(guard_report, indent=1),
                                      encoding="utf-8")
            record["guard"] = guard_report
            if remote_violations:
                record.update(
                    {"status": "failed", "reason":
                     f"tamper: {'; '.join(remote_violations[:5])}",
                     "done": True}
                )
                ledger_append_run(
                    config.ledger_path, task_id, mode, usage, rounds_used,
                    converged, tee,
                )
                return finalize_run(out_dir, mode, task_id, record, summary,
                                    tee)
        else:
            post_hoc = classify_remote_guard(task, clone, changed, tee)
            guard_report = {
                "task": task_id,
                "mode": mode,
                "binding": False,
                "verdict": "tamper" if post_hoc else "clean",
                "violations": post_hoc,
                "note": "post-hoc measurement; does not gate Mode A",
            }
            paths["guard"].parent.mkdir(parents=True, exist_ok=True)
            paths["guard"].write_text(json.dumps(guard_report, indent=1),
                                      encoding="utf-8")
            record["guard"] = guard_report
        reset_remote_test_files(cfg, task, run_dir, changed, tee)
        cache_dir = out_dir / "harness_cache"
        harness = get_test_patch(task, cache_dir, tee)
        apply_text_patch(cfg, run_dir, harness["test_patch"], tee, "agent")
        extra = [agent["red_rel"]] if mode == "B" else None
        agent_argv_ids: list[str] | None = None
        collect_broken = False
        if task["language"] == "python":
            agent_collected = collect_ids_remote(
                cfg, task, run_dir, tee, "agent",
                archive=paths["collect"],
            )
            record["evidence"]["collect"] = True
            archive_venv_freeze_remote(
                cfg, task, run_dir, paths["freeze"], tee, f"agent[{mode}]"
            )
            record["evidence"]["freeze"] = True
            agent_res = resolve_manifest_ids(
                manifest_ids_of(task), agent_collected
            )
            log_resolution(tee, "agent", agent_res)
            agent_argv_ids = flatten_resolution(agent_res)
            if not agent_argv_ids:
                collect_broken = True
                tee.log("agent: patch breaks collection entirely")
        agent_index: dict[str, str] = {}
        if collect_broken:
            record["collect_broken"] = True
            scoring = score_task(task, base_index, agent_index)
            scoring["collect_broken"] = True
            record["scoring"] = scoring
            record.update(
                {"status": "failed", "done": True,
                 "reason": "agent patch breaks test collection"}
            )
            ledger_append_run(
                config.ledger_path, task_id, mode, usage, rounds_used,
                converged, tee,
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        res, junit = run_remote_eval(
            cfg, task, run_dir, tee, extra_specs=extra, stage="agent",
            pytest_ids=agent_argv_ids,
        )
        if junit is None:
            record.update(
                {"status": "infra_error", "done": False,
                 "reason": f"agent eval produced no JUnit "
                 f"(exit={res.exit_code})"}
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        paths["junit"].parent.mkdir(parents=True, exist_ok=True)
        paths["junit"].write_bytes(junit)
        record["junit_sha256"] = sha256_bytes(junit)
        record["eval_exit"] = res.exit_code
        record["eval_timed_out"] = res.timed_out
        full_index = index_results(task, junit, tee, "agent")
        agent_index = remap_collected_index(base_resolution, full_index)
        scoring = score_task(task, base_index, agent_index)
        record["scoring"] = scoring
        functional = bool(scoring["resolved_functional"])
        if mode == "B" and not functional and green_retry_eligible(scoring):
            tee.log(
                "green-retry: first attempt "
                f"{describe_failure(scoring)}; one revision ..."
            )
            retry = run_green_retry_oci(
                cfg, config, task, clone, agent, scoring, junit,
                base_resolution, base_index, paths, usage, tee,
            )
            if retry["ok"]:
                record["green_retry"] = {
                    "attempted": True,
                    "completed": True,
                    "first": describe_failure(scoring),
                    "first_scoring": scoring,
                    "second": describe_failure(retry["scoring"]),
                }
                scoring = retry["scoring"]
                junit = retry["junit"]
                agent["patch_text"] = retry["patch_text"]
                paths["diff"].write_text(
                    retry["diff_text"], encoding="utf-8"
                )
                record["diff_sha256"] = sha256_text(retry["diff_text"])
                paths["junit"].write_bytes(junit)
                record["junit_sha256"] = sha256_bytes(junit)
                record["eval_exit"] = retry["eval_exit"]
                record["eval_timed_out"] = retry["eval_timed_out"]
                paths["guard"].write_text(
                    json.dumps(retry["guard"], indent=1), encoding="utf-8"
                )
                record["guard"] = retry["guard"]
                record["scoring"] = scoring
                functional = bool(scoring["resolved_functional"])
                tee.log(
                    "green-retry: second attempt "
                    f"{describe_failure(scoring)}"
                )
            else:
                record["green_retry"] = {
                    "attempted": True,
                    "completed": False,
                    "reason": retry["reason"],
                    "first": describe_failure(scoring),
                }
                tee.log(f"green-retry: retry ended ({retry['reason']})")
        if mode == "B" and functional:
            receipt_ok = mint_mode_b_receipt(
                task, clone, agent, record, junit,
                int(record.get("eval_exit", res.exit_code)), tee,
            )
            record["machine_proof"] = receipt_ok
            functional = functional and receipt_ok
        resolved = functional
        record.update(
            {
                "status": "resolved" if resolved else "failed",
                "reason": "" if resolved else describe_failure(scoring),
                "done": True,
            }
        )
        ledger_append_run(
            config.ledger_path, task_id, mode, usage, rounds_used, converged,
            tee,
        )
        return finalize_run(out_dir, mode, task_id, record, summary, tee)
    except (MetaAuthError, MetaAPIError) as exc:
        summary["agent_ok"] = False
        record = {
            "task": task_id, "mode": mode, "tier": "oci", "ts": utcnow_iso(),
            "status": "infra_error", "done": False,
            "reason": f"meta api error: {exc}",
        }
        tee.log(f"INFRA: {record['reason']}")
        # FinOps honesty: ledger append on failure paths too (skipped
        # internally when zero LLM calls were made).
        ledger_append_run(
            config.ledger_path, task_id, mode, usage, 0, False, tee,
        )
        return finalize_run(out_dir, mode, task_id, record, summary, tee)
    except Exception as exc:  # noqa: BLE001 (matrix robustness)
        summary["agent_ok"] = False
        record = {
            "task": task_id, "mode": mode, "tier": "oci", "ts": utcnow_iso(),
            "status": "infra_error", "done": False,
            "reason": f"{type(exc).__name__}: {exc}"[:800],
        }
        tee.log(f"INFRA: {record['reason']}")
        # FinOps honesty: ledger append on failure paths too (skipped
        # internally when zero LLM calls were made).
        ledger_append_run(
            config.ledger_path, task_id, mode, usage, 0, False, tee,
        )
        return finalize_run(out_dir, mode, task_id, record, summary, tee)
    finally:
        tee.close()


def mint_mode_b_receipt(
    task: dict, clone: Path, agent: dict, record: dict,
    junit: bytes, green_exit: int, tee: Tee,
) -> bool:
    """Mint the TGRES evidence receipt and evaluate the machine proof.

    The receipt binds base commit + red test + patch + both JUnit
    reports via SHA-256 (``TGRESProvenanceVerifier.mint_receipt``).
    The proof is the recorded conjunction ``red accepted AND guard
    clean AND scoring resolved``. Note: the stock
    ``verify_red_to_green`` exit-0 clause is NOT applied here — subset
    eval runs legitimately exit non-zero when env-limited P2P cases
    fail in the air-gapped sandbox; ``eval_exit`` is recorded honestly
    and scoring (see ``bench_lib.score_task``) carries the verdict.
    """
    from hackproof.provenance.tgres_mint import TGRESProvenanceVerifier

    red_rel = agent.get("red_rel", "")
    red_path = clone / red_rel if red_rel else None
    test_hash = (
        hashlib.sha256(red_path.read_bytes()).hexdigest()
        if red_path is not None and red_path.exists()
        else "missing-red-test"
    )
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=TIMEOUT_SSH,
    )
    base_commit = head.stdout.strip() if head.returncode == 0 else "unknown"
    receipt = TGRESProvenanceVerifier.mint_receipt(
        base_commit=base_commit,
        test_hash=test_hash,
        patch_hash=sha256_text(agent.get("patch_text", "")),
        red_junit_hash=str(
            record.get("red", {}).get("junit_sha256", "no-red-junit")
        ),
        green_junit_hash=sha256_bytes(junit),
    )
    record["receipt"] = receipt
    proof = {
        "red_accepted": bool(record.get("red", {}).get("accepted")),
        "guard_clean": record.get("guard", {}).get("verdict") == "clean",
        "scoring_resolved": bool(
            record.get("scoring", {}).get("resolved_functional")
        ),
        "eval_exit": green_exit,
    }
    record["machine_proof"] = proof
    ok = (
        proof["red_accepted"]
        and proof["guard_clean"]
        and proof["scoring_resolved"]
    )
    tee.log(f"receipt: {receipt.get('receipt_sha256')} machine_proof={ok}")
    return ok


def describe_failure(scoring: dict) -> str:
    """One-line functional failure explanation from scoring rows."""
    parts: list[str] = []
    if scoring.get("f2p_not_red_at_base"):
        parts.append(
            f"f2p-not-red-at-base:{len(scoring['f2p_not_red_at_base'])}"
        )
    if scoring.get("f2p_not_fixed"):
        parts.append(f"f2p-not-fixed:{len(scoring['f2p_not_fixed'])}")
    if scoring.get("p2p_broken"):
        parts.append(f"p2p-broken:{len(scoring['p2p_broken'])}")
    return "; ".join(parts) or "unresolved"


# ---------------------------------------------------------------------------
# Step 4.3c.1 (R9): single green-feedback iteration for Mode B
# ---------------------------------------------------------------------------

GREEN_RETRY_PER_TEST_CHARS = 1200
GREEN_RETRY_TOTAL_CHARS = 6000


def green_retry_eligible(
    scoring: dict,
    *,
    collect_broken: bool = False,
    guard_clean: bool = True,
) -> bool:
    """True when a run deserves exactly one green-feedback revision.

    Eligible: the patch applied and evaluated, every F2P was red at
    base (so the failure is patch content, not measurement), and the
    miss is iterable — F2P still red and/or P2P broken (Wave 1:
    2931/6028/11589-B). Ineligible: resolved runs, unmeasured F2P,
    broken collection, guard tamper.
    """
    if collect_broken or not guard_clean:
        return False
    if bool(scoring.get("resolved_functional")):
        return False
    if scoring.get("f2p_not_red_at_base"):
        return False
    return bool(
        scoring.get("f2p_not_fixed") or scoring.get("p2p_broken")
    )


def _junit_message_index(
    task: dict, junit_bytes: bytes
) -> dict[str, str]:
    """Map collected test IDs to their failure/error message text.

    Mirrors :func:`index_results` ID computation per language (same
    indexers, same manifest-file filter), but keeps the observed
    assertion text instead of the status. Unparseable payloads map
    to nothing (loud at the call site, never invented).
    """
    try:
        cases = parse_junit(junit_bytes)
    except ValueError:
        return {}
    if task.get("language") == "python":
        manifest_files = {
            test_id.split("::", 1)[0]
            for test_id in list(task.get("fail_to_pass", []))
            + list(task.get("pass_to_pass", []))
        }

        def _ids(case: dict) -> dict[str, str]:
            return index_pytest_cases([case], known_files=manifest_files)
    else:

        def _ids(case: dict) -> dict[str, str]:
            return index_vitest_cases([case])

    messages: dict[str, str] = {}
    for case in cases:
        if case.get("status") not in ("failed", "error"):
            continue
        text = str(case.get("message", "")).strip()
        if not text:
            continue
        for collected_id in _ids(case):
            messages.setdefault(collected_id, text)
    return messages


def condense_green_failures(
    task: dict,
    junit_bytes: bytes,
    scoring: dict,
    resolution: dict[str, list[str]],
) -> str:
    """Condense a green failure into names + assert excerpts (no gold).

    Each still-red F2P and each broken P2P contributes one entry: the
    harness test ID, its base→agent status transition, and the first
    observed JUnit assertion excerpt for its collected IDs. Sources
    are runtime outputs only — test_patch and gold patch are never
    read here, so no solution content can leak into the revision.
    Entries are per-test capped and the whole report is total-capped.
    """
    messages = _junit_message_index(task, junit_bytes)
    f2p_rows = scoring.get("f2p", {})
    p2p_rows = scoring.get("p2p", {})
    targets: list[tuple[str, str]] = [
        ("F2P", test_id) for test_id in scoring.get("f2p_not_fixed", [])
    ] + [("P2P", test_id) for test_id in scoring.get("p2p_broken", [])]
    lines = [
        "GREEN RESULT: patch applied, evaluated, not resolved. "
        "Still-failing tests (names + observed assertion excerpts):"
    ]
    for kind, test_id in targets:
        rows = f2p_rows if kind == "F2P" else p2p_rows
        row = rows.get(test_id, {})
        excerpt = "(no failure message captured)"
        for collected_id in resolution.get(test_id, []):
            if collected_id in messages:
                excerpt = messages[collected_id]
                break
        excerpt = excerpt.strip().replace("\r\n", "\n")
        if len(excerpt) > GREEN_RETRY_PER_TEST_CHARS:
            excerpt = (
                excerpt[:GREEN_RETRY_PER_TEST_CHARS] + "\n…[truncated]"
            )
        lines.append(
            f"- [{kind}] {test_id} "
            f"(base={row.get('base', '?')} "
            f"agent={row.get('agent', '?')}):\n  {excerpt}"
        )
    report = "\n".join(lines)
    if len(report) > GREEN_RETRY_TOTAL_CHARS:
        report = report[:GREEN_RETRY_TOTAL_CHARS] + "\n…[report truncated]"
    return report


def retry_agent_index(
    task: dict,
    resolution: dict[str, list[str]],
    base_resolution: dict[str, list[str]],
    full_index: dict[str, str],
) -> dict[str, str]:
    """Remap a retry-run JUnit index with the run's own resolution (12a.0).

    Python retry evals run in a fresh dir whose absolute-path params
    differ from the first attempt's; remapping with the stale map
    mislabels env-dependent IDs as ``missing`` (observed: 6028-B P2P).
    TypeScript keeps the first-attempt map (vitest IDs carry no env
    paths and no fresh map is built for the retry tier).
    """
    retry_resolution = (
        resolution if task["language"] == "python" else base_resolution
    )
    return remap_collected_index(retry_resolution, full_index)


def run_green_revision(
    task: dict,
    agent: dict,
    green_report: str,
    prompt_dir: Path,
    tee: Tee,
    usage: UsageCollector,
) -> dict | None:
    """Execute the single R9 builder revision; None when unusable.

    Builds the green-feedback prompt from the archived agent context
    (issue + builder code context + previous patch — all already
    model-visible, no harness input), records the report and the
    revision output under ``prompt_dir`` for audit, and drains the
    call into ``usage``. Returns ``{"patch_text", "routed"}`` when
    the revision carries complete blocks, else None (no eval spent).
    """
    from hackproof.debate.roles import architectural_builder
    from hackproof.llm.meta_client import MetaClient

    issue_text = agent.get("issue_text", "")
    code_context = agent.get("builder_code_context", "")
    previous_patch = agent.get("patch_text", "")
    if not issue_text or not previous_patch:
        tee.log("green-retry: missing agent context; skipping revision")
        return None
    (prompt_dir / "green_retry_report.txt").write_text(
        green_report, encoding="utf-8"
    )
    try:
        wonder = MetaClient("muse-wonder-1.0")
    except Exception as exc:  # noqa: BLE001 (auth/env -> skip retry)
        tee.log(f"green-retry: client construction failed ({exc}); skipping")
        return None
    try:
        revision = architectural_builder.revise_patch_from_green(
            wonder,
            issue_description=issue_text,
            code_context=code_context,
            previous_patch=previous_patch,
            green_report=green_report,
        )
    except Exception as exc:  # noqa: BLE001 (API error -> skip retry)
        tee.log(f"green-retry: revision call failed ({exc}); skipping")
        return None
    usage.drain(wonder, "ArchitecturalBuilder-green-retry", "muse-wonder-1.0")
    (prompt_dir / "patch_text_green_retry.txt").write_text(
        revision, encoding="utf-8"
    )
    routed = architectural_builder.extract_routed_blocks(revision)
    tee.log(f"green-retry: revision carries {len(routed)} complete block(s)")
    if not routed:
        return None
    return {"patch_text": revision, "routed": routed}


def run_green_retry_oci(
    cfg: SshConfig,
    config: BenchConfig,
    task: dict,
    clone: Path,
    agent: dict,
    scoring: dict,
    junit: bytes,
    base_resolution: dict[str, list[str]],
    base_index: dict[str, str],
    paths: dict[str, Path],
    usage: UsageCollector,
    tee: Tee,
) -> dict:
    """Execute the single R9 revision + re-eval on the OCI tier.

    Returns ``{"ok": True, ...}`` with the second attempt's scoring,
    JUnit, diff, guard report and eval outcome, or ``{"ok": False,
    "reason": ...}`` when the retry cannot proceed (revision
    unusable, apply failure, guard tamper, broken collection, no
    JUnit). The retry runs in a FRESH run dir from pristine prep —
    the first attempt's tree is never mutated — with the accepted
    red test shipped, the guard re-locked, and test files reset
    before the harness test_patch, exactly like the first attempt.
    No nested R6 retry: the revision applies once or the retry ends.
    """
    task_id = task["id"]
    green_report = condense_green_failures(
        task, junit, scoring, base_resolution
    )
    tee.log(f"green-retry: condensed report ({len(green_report)} chars)")
    rev = run_green_revision(
        task, agent, green_report, paths["prompts"], tee, usage
    )
    if rev is None:
        return {"ok": False, "reason": "revision unusable (no blocks)"}
    git_reset_hard(clone, task["base_ref"])
    red_rel = agent.get("red_rel", "")
    red_bytes = agent.get("red_bytes", b"")
    if red_rel and red_bytes:
        red_target = clone / red_rel
        red_target.parent.mkdir(parents=True, exist_ok=True)
        red_target.write_bytes(red_bytes)
    try:
        applied = apply_blocks_to_candidates(
            task_id, clone, agent.get("files", []), rev["routed"],
            guard_files_for(task), tee,
        )
    except Exception as exc:  # noqa: BLE001 (retry ends, first stands)
        return {"ok": False, "reason": f"revision apply failed: {exc}"[:400]}
    local_violations = verify_local_guard(clone, agent.get("baseline", {}))
    if local_violations:
        return {
            "ok": False,
            "reason": "retry tampers guard files: "
            f"{local_violations[:3]}",
        }
    diff_text = git_diff_text(clone)
    run_dir = fresh_run_dir(cfg, task, f"{config.mode}-green-retry", tee)
    if red_rel and red_bytes:
        if ssh_write_file(cfg, f"{run_dir}/{red_rel}", red_bytes) != 0:
            return {"ok": False, "reason": "red ship to retry dir failed"}
    if diff_text.strip():
        apply_text_patch(cfg, run_dir, diff_text, tee, "green-retry")
    changed = remote_changed_paths(cfg, run_dir)
    remote_violations = classify_remote_guard(task, clone, changed, tee)
    if sorted(local_violations) != sorted(remote_violations):
        return {
            "ok": False,
            "reason": "guard divergence on retry: "
            f"{local_violations} vs {remote_violations}",
        }
    if remote_violations:
        return {
            "ok": False,
            "reason": "retry tampers guard files: "
            f"{remote_violations[:3]}",
        }
    guard_report: dict = {
        "task": task_id,
        "mode": config.mode,
        "binding": True,
        "verdict": "clean",
        "violations": [],
        "note": "green-retry second attempt",
    }
    reset_remote_test_files(cfg, task, run_dir, changed, tee)
    harness = get_test_patch(task, config.out_dir / "harness_cache", tee)
    apply_text_patch(cfg, run_dir, harness["test_patch"], tee, "green-retry")
    agent_argv_ids: list[str] | None = None
    if task["language"] == "python":
        collected = collect_ids_remote(
            cfg, task, run_dir, tee, "green-retry",
            archive=paths["collect"],
        )
        archive_venv_freeze_remote(
            cfg, task, run_dir, paths["freeze"], tee, "green-retry"
        )
        resolution = resolve_manifest_ids(
            manifest_ids_of(task), collected
        )
        log_resolution(tee, "green-retry", resolution)
        agent_argv_ids = flatten_resolution(resolution)
        if not agent_argv_ids:
            return {
                "ok": False,
                "reason": "retry patch breaks test collection",
            }
    res, junit2 = run_remote_eval(
        cfg, task, run_dir, tee,
        extra_specs=[red_rel] if red_rel else None,
        stage="green-retry", pytest_ids=agent_argv_ids,
    )
    if junit2 is None:
        return {
            "ok": False,
            "reason": f"retry eval produced no JUnit (exit={res.exit_code})",
        }
    full_index = index_results(task, junit2, tee, "green-retry")
    # Fresh map exists only for python (TS builds none); the conditional
    # keeps TS from touching the undefined name.
    fresh = resolution if task["language"] == "python" else {}
    agent_index = retry_agent_index(task, fresh, base_resolution, full_index)
    scoring2 = score_task(task, base_index, agent_index)
    return {
        "ok": True,
        "scoring": scoring2,
        "junit": junit2,
        "diff_text": diff_text,
        "applied_files": applied,
        "patch_text": rev["patch_text"],
        "guard": guard_report,
        "eval_exit": res.exit_code,
        "eval_timed_out": res.timed_out,
    }


def run_green_retry_local(
    config: BenchConfig,
    task: dict,
    clone: Path,
    agent: dict,
    scoring: dict,
    junit: bytes,
    base_resolution: dict[str, list[str]],
    base_index: dict[str, str],
    paths: dict[str, Path],
    usage: UsageCollector,
    tee: Tee,
) -> dict:
    """Local-tier mirror of :func:`run_green_retry_oci` (smoke only).

    Same single-revision contract, same guard/test-hygiene ordering,
    via local subprocesses instead of Tailnet SSH + gVisor.
    """
    green_report = condense_green_failures(
        task, junit, scoring, base_resolution
    )
    tee.log(f"green-retry: condensed report ({len(green_report)} chars)")
    rev = run_green_revision(
        task, agent, green_report, paths["prompts"], tee, usage
    )
    if rev is None:
        return {"ok": False, "reason": "revision unusable (no blocks)"}
    git_reset_hard(clone, task["base_ref"])
    red_rel = agent.get("red_rel", "")
    red_bytes = agent.get("red_bytes", b"")
    if red_rel and red_bytes:
        red_target = clone / red_rel
        red_target.parent.mkdir(parents=True, exist_ok=True)
        red_target.write_bytes(red_bytes)
    try:
        applied = apply_blocks_to_candidates(
            task["id"], clone, agent.get("files", []), rev["routed"],
            guard_files_for(task), tee,
        )
    except Exception as exc:  # noqa: BLE001 (retry ends, first stands)
        return {"ok": False, "reason": f"revision apply failed: {exc}"[:400]}
    diff_text = git_diff_text(clone)
    run_dir = local_fresh_run_dir(
        config.work_dir, task, f"{config.mode}-green-retry", tee
    )
    if red_rel and red_bytes:
        red_dest = run_dir / red_rel
        red_dest.parent.mkdir(parents=True, exist_ok=True)
        red_dest.write_bytes(red_bytes)
    if diff_text.strip():
        local_apply_patch(run_dir, diff_text, tee, "green-retry")
    changed, _ = git_status_names(run_dir)
    changed = [p for p in changed
               if p.split("/", 1)[0] not in SCRATCH_NAMES]
    violations = classify_remote_guard(task, clone, changed, tee)
    if violations:
        return {
            "ok": False,
            "reason": f"retry tampers guard files: {violations[:3]}",
        }
    guard_report: dict = {
        "task": task["id"],
        "mode": config.mode,
        "binding": config.mode == "B",
        "verdict": "clean",
        "violations": [],
        "note": "green-retry second attempt",
    }
    touched = sorted(set(changed) & set(guard_files_for(task)))
    if touched:
        subprocess.run(
            ["git", "-C", str(run_dir), "checkout", task["base_ref"],
             "--", *touched],
            capture_output=True, timeout=TIMEOUT_SSH,
        )
        tee.log(f"green-retry: reset {len(touched)} test file(s) to base")
    harness = get_test_patch(task, config.out_dir / "harness_cache", tee)
    local_apply_patch(run_dir, harness["test_patch"], tee, "green-retry")
    agent_argv_ids: list[str] | None = None
    if task["language"] == "python":
        collected = collect_ids_local(
            task, run_dir, tee, "green-retry", archive=paths["collect"]
        )
        archive_venv_freeze_local(
            run_dir, paths["freeze"], tee, "green-retry"
        )
        resolution = resolve_manifest_ids(
            manifest_ids_of(task), collected
        )
        log_resolution(tee, "green-retry", resolution)
        agent_argv_ids = flatten_resolution(resolution)
        if not agent_argv_ids:
            return {
                "ok": False,
                "reason": "retry patch breaks test collection",
            }
    res, junit2 = local_run_eval(
        task, run_dir, tee,
        extra_specs=[red_rel] if red_rel else None,
        stage="green-retry", pytest_ids=agent_argv_ids,
    )
    if junit2 is None:
        return {
            "ok": False,
            "reason": f"retry eval produced no JUnit (exit={res.exit_code})",
        }
    full_index = index_results(task, junit2, tee, "green-retry")
    # Fresh map exists only for python (TS builds none); the conditional
    # keeps TS from touching the undefined name.
    fresh = resolution if task["language"] == "python" else {}
    agent_index = retry_agent_index(task, fresh, base_resolution, full_index)
    scoring2 = score_task(task, base_index, agent_index)
    return {
        "ok": True,
        "scoring": scoring2,
        "junit": junit2,
        "diff_text": diff_text,
        "applied_files": applied,
        "patch_text": rev["patch_text"],
        "guard": guard_report,
        "eval_exit": res.exit_code,
        "eval_timed_out": res.timed_out,
    }


def finalize_run(
    out_dir: Path, mode: str, task_id: str, record: dict, summary: dict,
    tee: Tee,
) -> dict:
    """Persist the result JSON and fold it into the summary row."""
    paths = result_paths(out_dir, mode, task_id)
    paths["result"].parent.mkdir(parents=True, exist_ok=True)
    paths["result"].write_text(json.dumps(record, indent=1), encoding="utf-8")
    if mode == "B" and not paths["guard"].is_file():
        # Evidence completeness: early exits (unmeasurable, patch
        # failure, red rejection, infra) never reached the guard
        # stage — record an explicit not-applicable verdict instead
        # of leaving the guard report missing.
        guard_report = {
            "task": task_id,
            "mode": mode,
            "binding": True,
            "verdict": "not-applicable",
            "violations": [],
            "note": f"no agent patch evaluated ({record.get('status')}: "
                    f"{str(record.get('reason', ''))[:120]})",
        }
        paths["guard"].parent.mkdir(parents=True, exist_ok=True)
        paths["guard"].write_text(
            json.dumps(guard_report, indent=1), encoding="utf-8"
        )
    summary["status"] = record.get("status")
    summary["reason"] = record.get("reason", "")
    summary["done"] = record.get("done")
    tee.log(
        f"== result {task_id} [{mode}]: {summary['status']} "
        f"({summary['reason'][:160]})"
    )
    return summary


def run_task_mode_local(
    config: BenchConfig, task: dict, manifest_note: str = ""
) -> dict:
    """Execute one task+mode run on the local tier (smoke only)."""
    from hackproof.llm.meta_client import MetaAPIError, MetaAuthError

    out_dir = config.out_dir
    task_id, mode = task["id"], config.mode
    paths = result_paths(out_dir, mode, task_id)
    tee = Tee(paths["log"])
    usage = UsageCollector()
    summary: dict = {"task": task_id, "mode": mode, "tier": "local"}
    try:
        tee.log(f"== Step 4.2 task {task_id} mode {mode} (local) "
                f"{manifest_note}")
        ledger_burn_check(config.ledger_path, tee)
        agent_input = get_agent_input(task, out_dir / "harness_cache", tee)
        prep_ok = local_run_dirs(config.work_dir, task_id)["prep"] / "OK.json"
        if not prep_ok.exists():
            local_prep(task, config.work_dir, tee)
        else:
            tee.log("prep[local]: marker present, reusing")
        base_index, _, base_record = ensure_base_run(
            None, task, out_dir, True, config.work_dir
        )
        base_resolution = base_record.get("id_resolution", {})
        if base_record.get("unresolved_ids"):
            record = {
                "task": task_id, "mode": mode, "tier": "local",
                "ts": utcnow_iso(), "status": "failed", "done": True,
                "reason": "unmeasurable: unresolved test IDs: "
                + ", ".join(base_record["unresolved_ids"][:5]),
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "per_call": []},
            }
            tee.log("agent: skipped (task unmeasurable, no LLM spent)")
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        clone = ensure_local_clone(task, config.work_dir / "_clones", tee)
        git_reset_hard(clone, task["base_ref"])
        run_dir = local_fresh_run_dir(config.work_dir, task, mode, tee)
        if mode == "A":
            agent = run_mode_a_agent(
                task, clone, agent_input, paths["prompts"], tee, usage
            )
            rounds_used, converged = 0, bool(agent.get("ok"))
        else:
            agent = run_mode_b_agent(
                task, clone, agent_input, paths["prompts"], tee, usage,
                prove_red=lambda rel, data: run_red_local(
                    task, run_dir, rel, data, tee
                ),
            )
            debate = agent.get("debate", {})
            rounds_used = int(debate.get("rounds_used", 0))
            converged = bool(debate.get("converged", False))
        summary["agent_ok"] = bool(agent.get("ok"))
        summary["agent_reason"] = agent.get("reason", "")
        record: dict = {
            "task": task_id,
            "mode": mode,
            "tier": "local",
            "ts": utcnow_iso(),
            "base_ref": task["base_ref"],
            "agent": {k: v for k, v in agent.items()
                      if k not in ("patch_text", "diff_text", "red_bytes",
                                   "red_record", "baseline",
                                   "evidence", "issue_text",
                                   "builder_code_context")},
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "per_call": usage.per_call,
            },
            # Step 4.3b.7: evidence flags flip to True as each bundle
            # artifact lands (collect/freeze below; debate prompts in
            # the agent). Absent block = grandfathered 4.2 record.
            "evidence": {
                "language": task["language"],
                "collect": False,
                "freeze": False,
                "debate_prompts": bool(
                    agent.get("evidence", {}).get("debate_prompts", False)
                ),
            },
        }
        if not agent.get("ok"):
            record.update(
                {"status": "failed", "reason": agent.get("reason", ""),
                 "done": True}
            )
            if agent.get("red_record") is not None:
                record["red"] = agent["red_record"]
            ledger_append_run(
                config.ledger_path, task_id, mode, usage, rounds_used,
                converged, tee,
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        diff_text = agent.get("diff_text", "")
        paths["diff"].parent.mkdir(parents=True, exist_ok=True)
        paths["diff"].write_text(diff_text, encoding="utf-8")
        record["diff_sha256"] = sha256_text(diff_text)
        if mode == "B":
            # Step 4.3b.6: red was proved inside the agent (before any
            # debate); consume its record instead of re-executing.
            red_record = agent.get("red_record") or {}
            record["red"] = red_record
            if not red_record.get("accepted"):
                record.update(
                    {"status": "failed", "reason":
                     "red-rejected: "
                     f"{red_record.get('reason', 'red proof missing')}",
                     "done": True}
                )
                ledger_append_run(
                    config.ledger_path, task_id, mode, usage, rounds_used,
                    converged, tee,
                )
                return finalize_run(out_dir, mode, task_id, record, summary,
                                    tee)
        if diff_text.strip():
            local_apply_patch(run_dir, diff_text, tee, f"agent[{mode}]")
        changed, _ = git_status_names(run_dir)
        changed = [p for p in changed
                   if p.split("/", 1)[0] not in SCRATCH_NAMES]
        tee.log(f"agent[{mode}]: {len(changed)} changed path(s)")
        violations = classify_remote_guard(task, clone, changed, tee)
        guard_report = {
            "task": task_id,
            "mode": mode,
            "binding": mode == "B",
            "verdict": "tamper" if violations else "clean",
            "violations": violations,
        }
        paths["guard"].parent.mkdir(parents=True, exist_ok=True)
        paths["guard"].write_text(json.dumps(guard_report, indent=1),
                                  encoding="utf-8")
        record["guard"] = guard_report
        if mode == "B" and violations:
            record.update(
                {"status": "failed", "reason":
                 f"tamper: {'; '.join(violations[:5])}", "done": True}
            )
            ledger_append_run(
                config.ledger_path, task_id, mode, usage, rounds_used,
                converged, tee,
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        touched = sorted(set(changed) & set(guard_files_for(task)))
        if touched:
            subprocess.run(
                ["git", "-C", str(run_dir), "checkout", task["base_ref"],
                 "--", *touched],
                capture_output=True, timeout=TIMEOUT_SSH,
            )
            tee.log(f"hygiene: reset {len(touched)} test file(s) to base")
        harness = get_test_patch(task, out_dir / "harness_cache", tee)
        local_apply_patch(run_dir, harness["test_patch"], tee, "agent")
        extra = [agent["red_rel"]] if mode == "B" else None
        agent_argv_ids: list[str] | None = None
        collect_broken = False
        if task["language"] == "python":
            agent_collected = collect_ids_local(
                task, run_dir, tee, "agent", archive=paths["collect"]
            )
            record["evidence"]["collect"] = True
            archive_venv_freeze_local(
                run_dir, paths["freeze"], tee, f"agent[{mode}]"
            )
            record["evidence"]["freeze"] = True
            agent_res = resolve_manifest_ids(
                manifest_ids_of(task), agent_collected
            )
            log_resolution(tee, "agent", agent_res)
            agent_argv_ids = flatten_resolution(agent_res)
            if not agent_argv_ids:
                collect_broken = True
        agent_index: dict[str, str] = {}
        if collect_broken:
            record["collect_broken"] = True
            scoring = score_task(task, base_index, agent_index)
            scoring["collect_broken"] = True
            record["scoring"] = scoring
            record.update(
                {"status": "failed", "done": True,
                 "reason": "agent patch breaks test collection"}
            )
            ledger_append_run(
                config.ledger_path, task_id, mode, usage, rounds_used,
                converged, tee,
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        res, junit = local_run_eval(
            task, run_dir, tee, extra_specs=extra, stage="agent",
            pytest_ids=agent_argv_ids,
        )
        if junit is None:
            record.update(
                {"status": "infra_error", "done": False,
                 "reason": f"agent eval produced no JUnit "
                 f"(exit={res.exit_code})"}
            )
            return finalize_run(out_dir, mode, task_id, record, summary, tee)
        paths["junit"].parent.mkdir(parents=True, exist_ok=True)
        paths["junit"].write_bytes(junit)
        record["junit_sha256"] = sha256_bytes(junit)
        record["eval_exit"] = res.exit_code
        record["eval_timed_out"] = res.timed_out
        full_index = index_results(task, junit, tee, "agent")
        agent_index = remap_collected_index(base_resolution, full_index)
        scoring = score_task(task, base_index, agent_index)
        record["scoring"] = scoring
        functional = bool(scoring["resolved_functional"])
        if mode == "B" and not functional and green_retry_eligible(scoring):
            tee.log(
                "green-retry: first attempt "
                f"{describe_failure(scoring)}; one revision ..."
            )
            retry = run_green_retry_local(
                config, task, clone, agent, scoring, junit,
                base_resolution, base_index, paths, usage, tee,
            )
            if retry["ok"]:
                record["green_retry"] = {
                    "attempted": True,
                    "completed": True,
                    "first": describe_failure(scoring),
                    "first_scoring": scoring,
                    "second": describe_failure(retry["scoring"]),
                }
                scoring = retry["scoring"]
                junit = retry["junit"]
                agent["patch_text"] = retry["patch_text"]
                paths["diff"].write_text(
                    retry["diff_text"], encoding="utf-8"
                )
                record["diff_sha256"] = sha256_text(retry["diff_text"])
                paths["junit"].write_bytes(junit)
                record["junit_sha256"] = sha256_bytes(junit)
                record["eval_exit"] = retry["eval_exit"]
                record["eval_timed_out"] = retry["eval_timed_out"]
                paths["guard"].write_text(
                    json.dumps(retry["guard"], indent=1), encoding="utf-8"
                )
                record["guard"] = retry["guard"]
                record["scoring"] = scoring
                functional = bool(scoring["resolved_functional"])
                tee.log(
                    "green-retry: second attempt "
                    f"{describe_failure(scoring)}"
                )
            else:
                record["green_retry"] = {
                    "attempted": True,
                    "completed": False,
                    "reason": retry["reason"],
                    "first": describe_failure(scoring),
                }
                tee.log(f"green-retry: retry ended ({retry['reason']})")
        resolved = functional
        record.update(
            {
                "status": "resolved" if resolved else "failed",
                "reason": "" if resolved else describe_failure(scoring),
                "done": True,
            }
        )
        ledger_append_run(
            config.ledger_path, task_id, mode, usage, rounds_used, converged,
            tee,
        )
        return finalize_run(out_dir, mode, task_id, record, summary, tee)
    except (MetaAuthError, MetaAPIError) as exc:
        summary["agent_ok"] = False
        record = {
            "task": task_id, "mode": mode, "tier": "local",
            "ts": utcnow_iso(), "status": "infra_error", "done": False,
            "reason": f"meta api error: {exc}",
        }
        tee.log(f"INFRA: {record['reason']}")
        # FinOps honesty: ledger append on failure paths too (skipped
        # internally when zero LLM calls were made).
        ledger_append_run(
            config.ledger_path, task_id, mode, usage, 0, False, tee,
        )
        return finalize_run(out_dir, mode, task_id, record, summary, tee)
    except Exception as exc:  # noqa: BLE001 (matrix robustness)
        summary["agent_ok"] = False
        record = {
            "task": task_id, "mode": mode, "tier": "local",
            "ts": utcnow_iso(), "status": "infra_error", "done": False,
            "reason": f"{type(exc).__name__}: {exc}"[:800],
        }
        tee.log(f"INFRA: {record['reason']}")
        # FinOps honesty: ledger append on failure paths too (skipped
        # internally when zero LLM calls were made).
        ledger_append_run(
            config.ledger_path, task_id, mode, usage, 0, False, tee,
        )
        return finalize_run(out_dir, mode, task_id, record, summary, tee)
    finally:
        tee.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the Step 4.2 command-line interface."""
    parser = argparse.ArgumentParser(
        description="Step 4.2: dual-mode benchmark execution (Mode A vs B)"
    )
    parser.add_argument(
        "--mode", required=True, choices=("A", "B"),
        help="agent mode: A (single-shot baseline) or B (HackProof AI)",
    )
    parser.add_argument(
        "--tasks", default=str(
            BENCH_DIR / "datasets" / "tasks_manifest.json"
        ),
        help="tasks manifest path",
    )
    parser.add_argument(
        "--out", default=str(BENCH_DIR / "results"),
        help="results directory",
    )
    parser.add_argument(
        "--runner", default="oci", choices=("oci", "local"),
        help="execution tier (default oci; local is 1-task smoke only)",
    )
    parser.add_argument(
        "--task-id", action="append", default=[],
        help="restrict to these task IDs (repeatable; default: all)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="skip task+mode pairs with done=true results",
    )
    parser.add_argument(
        "--work-dir", default="/tmp/hp_bench_work",
        help="local scratch dir for clones (outside the repo)",
    )
    parser.add_argument(
        "--ledger", default=str(PKG_ROOT / "finops_ledger.json"),
        help="FinOps ledger path",
    )
    return parser


def preflight_meta() -> None:
    """Fail fast when Meta credentials are missing (before SSH work)."""
    from hackproof.llm.meta_client import MetaClient

    MetaClient("muse-spark-1.3")
    MetaClient("muse-wonder-1.0")


def main(argv: list[str] | None = None) -> int:
    """Matrix entry point; returns 0 when every run finished."""
    args = build_parser().parse_args(argv)
    load_dotenv_defaults(
        {
            "META_BASE_URL",
            "META_API_KEY",
            "OCI_SSH_HOST",
            "OCI_SSH_USER",
            "OCI_SSH_KEY",
            "TAILSCALE_SOCKET",
        }
    )
    config = BenchConfig(
        mode=args.mode,
        manifest_path=Path(args.tasks),
        out_dir=Path(args.out),
        runner_tier=args.runner,
        task_ids=list(args.task_id),
        resume=args.resume,
        work_dir=Path(args.work_dir),
        ledger_path=Path(args.ledger),
        oci_host=os.environ.get("OCI_SSH_HOST", "100.112.52.73"),
        oci_user=os.environ.get("OCI_SSH_USER", "ubuntu"),
        oci_key=os.environ.get(
            "OCI_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519")
        ),
        tailscale_socket=os.environ.get("TAILSCALE_SOCKET", "/tmp/ts/sock"),
    )
    local = config.runner_tier == "local"
    if local and not config.task_ids:
        print(
            "local tier is 1-task smoke only: pass --task-id <id>",
            file=sys.stderr,
        )
        return 2
    manifest = load_manifest(config.manifest_path)
    tasks = manifest["tasks"]
    if config.task_ids:
        wanted = set(config.task_ids)
        tasks = [t for t in tasks if t["id"] in wanted]
        missing = wanted - {t["id"] for t in tasks}
        if missing:
            print(f"unknown --task-id: {sorted(missing)}", file=sys.stderr)
            return 2
    if local and len(tasks) > 1:
        print(
            "local tier is 1-task smoke only: pass a single --task-id",
            file=sys.stderr,
        )
        return 2
    try:
        preflight_meta()
    except Exception as exc:  # noqa: BLE001 (fail-fast message)
        print(f"preflight: Meta credentials unusable: {exc}", file=sys.stderr)
        return 1
    cache_dir = config.out_dir / "harness_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fetch_tee = Tee(config.out_dir / "prefetch.log")
    try:
        languages = sorted({t["language"] for t in tasks})
        for language in languages:
            # Harness caches are fetched up front for efficiency (one HF
            # scan per split); agent/harness column separation is
            # enforced per cache file, and agent functions receive ONLY
            # agent_input — never the harness record.
            prefetch_split_caches(manifest, language, cache_dir, fetch_tee,
                                  "input")
            prefetch_split_caches(manifest, language, cache_dir, fetch_tee,
                                  "testpatch")
    finally:
        fetch_tee.close()
    cfg: SshConfig | None = None
    if not local:
        cfg = SshConfig(
            host=config.oci_host,
            user=config.oci_user,
            key_path=config.oci_key,
            tailscale_socket=config.tailscale_socket,
        )
        try:
            exit_code, stdout, _ = ssh_exec(cfg, "echo OCI_OK", timeout=30)
        except Exception as exc:  # noqa: BLE001 (fail-fast message)
            print(f"preflight: OCI unreachable: {exc}", file=sys.stderr)
            return 1
        if exit_code != 0 or "OCI_OK" not in stdout:
            print("preflight: OCI SSH check failed", file=sys.stderr)
            return 1
        print(f"preflight: OCI {config.oci_user}@{config.oci_host} reachable")
    rows: list[dict] = []
    for task in tasks:
        if config.resume:
            done = load_done_result(config.out_dir, config.mode, task["id"])
            if done is not None:
                print(
                    f"resume: skip {task['id']} [{config.mode}] "
                    f"({done.get('status')})"
                )
                rows.append(
                    {
                        "task": task["id"],
                        "mode": config.mode,
                        "status": done.get("status"),
                        "resumed": True,
                    }
                )
                continue
        if local:
            rows.append(run_task_mode_local(config, task))
        else:
            assert cfg is not None
            rows.append(run_task_mode_oci(cfg, config, task))
    summary_path = config.out_dir / f"matrix_summary_{config.mode}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "mode": config.mode,
                "tier": config.runner_tier,
                "ts": utcnow_iso(),
                "runs": rows,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n== Step 4.2 matrix [{config.mode}] ({config.runner_tier}) ==")
    for row in rows:
        print(
            f"  {row['task']}: {row.get('status')}"
            f"{' (resumed)' if row.get('resumed') else ''}"
            f" — {(row.get('reason') or '')[:100]}"
        )
    unfinished = [r for r in rows if r.get("status") == "infra_error"]
    return 1 if unfinished else 0


if __name__ == "__main__":
    raise SystemExit(main())


