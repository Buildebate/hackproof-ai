#!/usr/bin/env python3
"""OCI collect probe (Step 4.3b.4/R7, reused in 4.3b.5/R8).

Prep-if-stale + ``pytest --collect-only`` + manifest resolution for one
task, WITHOUT any LLM call ($0) and WITHOUT touching the immutable
4.2 archives (all probe artifacts go to a scratch ``--out`` dir;
the harness cache is read-only).

Acceptance shape (4.3b.4): 2931 collect exit 0, 85/85 IDs resolved.

Usage:
    python3 benchmarks/probe_collect.py --task-id psf__requests-2931
    python3 benchmarks/probe_collect.py --task-id psf__requests-2931 --full
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
PKG_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(PKG_ROOT / "src"))
sys.path.insert(0, str(BENCH_DIR))

import run_benchmark as rb  # noqa: E402
from bench_lib import (  # noqa: E402
    load_manifest,
    resolve_manifest_ids,
    task_by_id,
    utcnow_iso,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the probe CLI."""
    parser = argparse.ArgumentParser(
        description="OCI collect probe: prep + collect-only + resolve ($0)"
    )
    parser.add_argument(
        "--task-id", required=True,
        help="manifest task ID (e.g. psf__requests-2931)",
    )
    parser.add_argument(
        "--tasks",
        default=str(BENCH_DIR / "datasets" / "tasks_manifest.json"),
        help="tasks manifest path",
    )
    parser.add_argument(
        "--out", default="/tmp/hp_probe/out",
        help="scratch output dir (NEVER the results archive)",
    )
    parser.add_argument(
        "--work-dir", default="/tmp/hp_bench_work",
        help="local scratch dir for clones (outside the repo)",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="run the full base eval after collect (still $0, slower)",
    )
    return parser


def ensure_prep(cfg: rb.SshConfig, task: dict, tee: rb.Tee) -> None:
    """Rebuild the OCI prep checkout when its fingerprint is stale."""
    fingerprint = rb.prep_fingerprint(task)
    if rb.prep_is_fresh(cfg, task, fingerprint, tee):
        return
    prep = rb.ssh_prep_checkout(cfg, task, tee)
    deps = rb.prep_dependencies(cfg, task, prep, tee)
    marker = {
        "base_ref": task["base_ref"],
        "dataset_sha256": task["dataset_sha256"],
        "fingerprint": fingerprint,
        "deps": deps,
        "ts": utcnow_iso(),
    }
    rc = rb.ssh_write_file(
        cfg, f"{prep}/OK.json", json.dumps(marker, indent=1).encode("utf-8")
    )
    if rc != 0:
        raise RuntimeError("prep marker write failed")
    tee.log("prep: marker written")


def probe_collect(
    cfg: rb.SshConfig, task: dict, tee: rb.Tee, out_dir: Path
) -> dict:
    """Collect-only probe; return the JSON verdict dict.

    The FULL collected ID list is archived to
    ``<task>.probe.ids.txt`` (Step 4.3b.5/R8: the 4.2 6028 failure was
    undiagnosable precisely because collect stdout was discarded).
    """
    cache_dir = PKG_ROOT / "benchmarks" / "results" / "harness_cache"
    harness = rb.get_test_patch(task, cache_dir, tee)
    run_dir = rb.fresh_run_dir(cfg, task, "probe", tee)
    rb.apply_text_patch(cfg, run_dir, harness["test_patch"], tee, "probe")
    manifest_ids = rb.manifest_ids_of(task)
    collected, report = rb.collect_ids_remote_full(
        cfg, task, run_dir, tee, "probe",
        archive=out_dir / f"{task['id']}.probe.collect",
    )
    ids_path = out_dir / f"{task['id']}.probe.ids.txt"
    ids_blob = "".join(line + "\n" for line in collected)
    ids_path.write_text(ids_blob, encoding="utf-8")
    resolution = resolve_manifest_ids(manifest_ids, collected)
    unresolved = [m for m, v in resolution.items() if not v]
    resolved = len(manifest_ids) - len(unresolved)
    verdict = {
        "task": task["id"],
        "ts": utcnow_iso(),
        "collect_exit": report["exit_code"],
        "collect_image": report["image"],
        "n_collected": len(collected),
        "n_manifest": len(manifest_ids),
        "n_resolved": resolved,
        "unresolved_ids": unresolved,
        "ids_file": ids_path.name,
        "ids_sha256": rb.sha256_text(ids_blob),
        "ok": report["exit_code"] == 0 and not unresolved,
    }
    tee.log(
        f"probe: exit={verdict['collect_exit']} "
        f"resolved={resolved}/{len(manifest_ids)} ok={verdict['ok']} "
        f"(ids archived: {ids_path.name})"
    )
    return verdict


def main(argv: list[str] | None = None) -> int:
    """Probe entry point; returns 0 when the probe verdict is ok."""
    args = build_parser().parse_args(argv)
    rb.load_dotenv_defaults(
        {"OCI_SSH_HOST", "OCI_SSH_USER", "OCI_SSH_KEY", "TAILSCALE_SOCKET"}
    )
    out_dir = Path(args.out)
    if out_dir.resolve().is_relative_to(
        (BENCH_DIR / "results").resolve()
    ):
        print("--out must NOT be inside benchmarks/results", file=sys.stderr)
        return 2
    manifest = load_manifest(Path(args.tasks))
    task = task_by_id(manifest, args.task_id)
    if task["language"] != "python":
        print("probe supports python tasks only", file=sys.stderr)
        return 2
    cfg = rb.SshConfig(
        host=os.environ.get("OCI_SSH_HOST", "100.112.52.73"),
        user=os.environ.get("OCI_SSH_USER", "ubuntu"),
        key_path=os.environ.get(
            "OCI_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519")
        ),
        tailscale_socket=os.environ.get("TAILSCALE_SOCKET", "/tmp/ts/sock"),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tee = rb.Tee(out_dir / f"{task['id']}.probe.log")
    try:
        exit_code, stdout, _ = rb.ssh_exec(cfg, "echo OCI_OK", timeout=30)
        if exit_code != 0 or "OCI_OK" not in stdout:
            tee.log("preflight: OCI SSH check failed")
            return 1
        tee.log("preflight: OCI reachable")
        ensure_prep(cfg, task, tee)
        verdict = probe_collect(cfg, task, tee, out_dir)
        if args.full:
            base_index, junit, record = rb.ensure_base_run(
                cfg, task, out_dir, False, Path(args.work_dir)
            )
            verdict["full_base"] = {
                "n_indexed": len(base_index),
                "junit_bytes": len(junit),
                "unresolved_ids": record.get("unresolved_ids", []),
                "collect": record.get("collect", {}),
            }
            verdict["ok"] = bool(
                verdict["ok"]
                and not record.get("unresolved_ids")
                and junit
            )
            tee.log(f"probe --full: ok={verdict['ok']}")
        verdict_path = out_dir / f"{task['id']}.probe.json"
        verdict_path.write_text(
            json.dumps(verdict, indent=1), encoding="utf-8"
        )
        print(json.dumps(verdict, indent=1))
        return 0 if verdict["ok"] else 1
    finally:
        tee.close()


if __name__ == "__main__":
    raise SystemExit(main())
