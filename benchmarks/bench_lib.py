"""Shared benchmark library for Step 4.2 (Mode A vs Mode B).

Pure stdlib helpers with zero network access, shared by
``benchmarks/run_benchmark.py`` (live oracle) and
``tests/unit/test_benchmark_results.py`` (offline validation):

* manifest loading + guard-file derivation from test IDs;
* tolerant JUnit parsing for pytest and vitest reports;
* red-baseline judging (mirrors ``RegressionTestSynthesizer`` rule);
* per-task F2P/P2P scoring against a measured base run;
* run-accounting aggregation for the results directory.

Scoring is always RELATIVE to a base run measured in the same sandbox:
a PASS_TO_PASS test that fails at base (e.g. needs live network under
``--network=none``) is reported as ``env_limited`` and excluded from the
denominator instead of silently failing the agent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "PARAM_RE",
    "STATUS_PASSED",
    "STATUS_FAILED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "STATUS_MISSING",
    "JUnitCase",
    "load_manifest",
    "task_by_id",
    "test_file_of_pytest_id",
    "test_file_of_vitest_id",
    "guard_files_for",
    "eval_files_for",
    "parse_junit",
    "parse_collect_output",
    "index_pytest_cases",
    "index_vitest_cases",
    "resolve_manifest_ids",
    "remap_collected_index",
    "judge_red",
    "score_task",
    "summarize_results",
    "audit_results_dir",
    "sha256_text",
    "sha256_bytes",
    "utcnow_iso",
]

PARAM_RE = re.compile(r"^(?P<name>.+?)(?P<params>\[.+\])$")

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
STATUS_MISSING = "missing"

_NON_PASS = frozenset({STATUS_FAILED, STATUS_ERROR})


class JUnitCase(dict):
    """One parsed ``<testcase>``: file/classname/name/status/message."""


def sha256_text(text: str) -> str:
    """Hex SHA-256 of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(blob: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(blob).hexdigest()


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def load_manifest(path: Path | str) -> dict:
    """Load and minimally validate the tasks manifest."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("tasks"), list
    ):
        raise ValueError("manifest must be a dict with a 'tasks' list")
    return manifest


def task_by_id(manifest: dict, task_id: str) -> dict:
    """Return the task dict for ``task_id`` (KeyError when absent)."""
    for task in manifest["tasks"]:
        if task.get("id") == task_id:
            return task
    raise KeyError(f"unknown benchmark task: {task_id}")


def test_file_of_pytest_id(node_id: str) -> str:
    """Extract the file part of a pytest node ID (``::``-separated)."""
    return node_id.split("::", 1)[0].strip()


def test_file_of_vitest_id(case_name: str) -> str:
    """Extract the spec-file part of a vitest ``file > suite > test`` ID."""
    return case_name.split(">", 1)[0].strip()


def guard_files_for(task: dict) -> list[str]:
    """Test files the agent must leave untouched (F2P + P2P file union).

    The guard scope is derived from test IDs — never from a hardcoded
    ``tests/`` directory — because old snapshots keep test files at the
    repo root (``test_requests.py``) and TypeScript specs live next to
    sources (``__tests__/*.spec.ts``).
    """
    language = task.get("language")
    ids = list(task.get("fail_to_pass", [])) + list(
        task.get("pass_to_pass", [])
    )
    files: list[str] = []
    for raw in ids:
        if language == "python":
            candidate = test_file_of_pytest_id(raw)
        else:
            candidate = test_file_of_vitest_id(raw)
        if candidate and candidate not in files:
            files.append(candidate)
    return files


def eval_files_for(task: dict) -> list[str]:
    """Files whose tests are executed during evaluation runs."""
    return guard_files_for(task)


def _case_status(case: ET.Element) -> tuple[str, str]:
    """Map a ``<testcase>`` element to (status, message)."""
    for child in case:
        tag = child.tag.split("}")[-1]
        if tag == "skipped":
            return STATUS_SKIPPED, (child.get("message") or "").strip()
        if tag in ("failure", "failed"):
            return STATUS_FAILED, (child.get("message") or "").strip()
        if tag == "error":
            return STATUS_ERROR, (child.get("message") or "").strip()
    return STATUS_PASSED, ""


def parse_junit(xml_bytes: bytes) -> list[JUnitCase]:
    """Parse JUnit XML into per-testcase records (tolerant).

    Accepts both ``<testsuite>`` and ``<testsuites>`` roots. Entries that
    are not ``<testcase>`` elements are ignored. Raises ``ValueError``
    when the payload is not parseable XML or carries no testcase.

    Returns:
        List of ``JUnitCase`` dicts with ``file``, ``classname``,
        ``name`` and ``status`` keys (plus ``message`` on non-pass).
    """
    try:
        text = xml_bytes.decode("utf-8", errors="replace")
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable JUnit XML: {exc}") from exc
    cases: list[JUnitCase] = []
    for case in root.iter():
        if case.tag.split("}")[-1] != "testcase":
            continue
        status, message = _case_status(case)
        entry = JUnitCase(
            file=(case.get("file") or "").strip(),
            classname=(case.get("classname") or "").strip(),
            name=(case.get("name") or "").strip(),
            status=status,
        )
        if message:
            entry["message"] = message[:500]
        cases.append(entry)
    if not cases:
        raise ValueError("JUnit XML carries zero testcase entries")
    return cases


def _last_dotted_segment(value: str) -> str:
    """Last ``.``-separated segment (class name out of a module path)."""
    return value.rsplit(".", 1)[-1].strip()


def _classname_file_candidates(classname: str) -> list[str]:
    """Ordered repo-relative ``.py`` candidates for a dotted classname.

    Class-style trailing segments (``TestX``) resolve to the enclosing
    module (drop-last first); module-style spellings (``a.b.c``) resolve
    to the full path first. Ordering is deterministic; callers filter
    by the collected/manifest file set.
    """
    segments = [seg for seg in classname.split(".") if seg]
    if not segments:
        return []
    full = "/".join(segments) + ".py"
    if len(segments) == 1:
        return [full]
    drop = "/".join(segments[:-1]) + ".py"
    if segments[-1][:1].isupper():
        return [drop, full]
    return [full, drop]


def _class_of_classname(classname: str, file: str) -> str:
    """Class segment of ``classname`` relative to the resolved ``file``.

    Returns ``""`` for module-level tests (classname is the module path
    itself), the trailing segment when it extends the module path
    (``a.b.Class``), and the last segment for bare or unrelated
    classnames (``Class``).
    """
    if not classname:
        return ""
    dotted = file[:-3].replace("/", ".") if file.endswith(".py") else ""
    if dotted and (classname == dotted or classname.startswith(dotted + ".")):
        rest = classname[len(dotted):].lstrip(".")
        return rest.split(".")[0] if rest else ""
    return _last_dotted_segment(classname)


def index_pytest_cases(
    cases: list[JUnitCase],
    known_files: Collection[str] | None = None,
) -> dict[str, str]:
    """Index pytest cases by canonical ``file::Class::test`` node ID.

    The ``file`` attribute is normalized to a repo-relative POSIX path
    (leading ``./`` stripped). Old pytest (<6) omits ``file``: the path
    is then derived from ``classname`` (drop-last for ``Class``-style
    trailing segments, full path for module-style spellings), filtered
    by ``known_files`` when given (manifest/collected file set).
    Unresolvable cases stay missing (loud), never invented. The class
    segment is empty for module-level tests.
    """
    index: dict[str, str] = {}
    known = set(known_files) if known_files is not None else None
    for case in cases:
        raw_file = (case.get("file") or "").lstrip("./")
        classname = (case.get("classname") or "").strip()
        name = case.get("name") or ""
        if not name:
            continue
        if not raw_file:
            candidates = _classname_file_candidates(classname)
            if known is not None:
                raw_file = next((c for c in candidates if c in known), "")
            else:
                raw_file = candidates[0] if candidates else ""
            if not raw_file:
                continue
        cls = _class_of_classname(classname, raw_file)
        node = f"{raw_file}::{cls}::{name}" if cls else f"{raw_file}::{name}"
        index[node] = case.get("status", STATUS_MISSING)
    return index


def _vitest_full_name(case: JUnitCase) -> str:
    """Best-effort ``file > suite > test`` reconstruction for vitest.

    Names already embedding the spec file as their first segment are
    normalized (whitespace around separators). All other names are kept
    byte-identical with the ``file``/``classname`` prefix prepended:
    test titles may legitimately contain ``>`` (generics, ``>>>``).
    """
    name = case.get("name") or ""
    if ">" in name:
        head = name.split(">", 1)[0].strip()
        if head.endswith(".spec.ts") or head.endswith(".test.ts"):
            return " > ".join(part.strip() for part in name.split(">"))
    prefix = (case.get("file") or case.get("classname") or "").strip()
    if prefix:
        return f"{prefix} > {name}".strip() if name else prefix
    return name


def index_vitest_cases(cases: list[JUnitCase]) -> dict[str, str]:
    """Index vitest cases by ``file > suite > test`` display ID.

    Both reporter spellings are covered: the spec file carried in the
    ``file``/``classname`` attribute, or already embedded as the first
    ``>``-separated segment of ``name``.
    """
    index: dict[str, str] = {}
    for case in cases:
        full = _vitest_full_name(case)
        if full:
            index[full] = case.get("status", STATUS_MISSING)
    return index


def parse_collect_output(text: str) -> list[str]:
    """Parse ``pytest --collect-only -q`` stdout into full test IDs.

    Collected IDs print one per line as ``file::test[params]`` with no
    leading whitespace; summary, warning and error lines are skipped.
    """
    ids: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "::" not in line:
            continue
        if raw != raw.strip() or line.startswith(("ERROR", "error")):
            continue
        if line not in ids:
            ids.append(line)
    return ids


# pytest>=8 renders str param values longer than 100 chars as
# ``argname+index`` (``_pytest/python.py::_apply_long_str_strategy``,
# default ``parametrize_long_str_id_strategy=short``); older pytest
# rendered the full value. Step 4.3b.5/R8 back-matches the dataset's
# full spelling to the collected short spelling (6028 auth-URL case:
# 154-char head observed byte-exact in the OCI probe archive).
_PYTEST_LONG_STR_THRESHOLD = 100
_SHORT_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\d+$")


def _node_of(test_id: str) -> str:
    """Node prefix of a test ID (everything before ``[``, if any)."""
    return test_id.partition("[")[0]


def _bracket_of(test_id: str) -> str | None:
    """Param bracket of ``node[param]``; None when not bracket-closed."""
    match = PARAM_RE.match(test_id)
    if match is None:
        return None
    return match.group("params")[1:-1]


def _suffix_candidates(
    node: str, param: str, collected: list[str]
) -> list[str]:
    """Collected IDs whose bracket strictly extends ``param`` (same node).

    Scoped to path-like params (``/`` required): a bare endswith would
    mis-pair short tokens (``[0]`` vs ``[10]``). All observed cases
    are ``/``+basename vs absolute path (``__file__``-style values).
    """
    if "/" not in param:
        return []
    cands: list[str] = []
    for full in collected:
        if _node_of(full) != node:
            continue
        cparam = _bracket_of(full)
        if cparam is None or cparam == param:
            continue
        if cparam.endswith(param):
            cands.append(full)
    return cands


def _longstr_candidates(
    node: str, param: str, collected: list[str]
) -> list[str]:
    """Back-match a full param spelling to a pytest-8 short rendering.

    Same node; collected head is ``argname+digits``, tails after the
    first ``-`` are identical, and the manifest head exceeds the
    pytest long-string threshold (it is exactly the value pytest
    refused to render).
    """
    cands: list[str] = []
    for full in collected:
        if _node_of(full) != node:
            continue
        cparam = _bracket_of(full)
        if cparam is None or "-" not in cparam:
            continue
        chead, _, ctail = cparam.partition("-")
        if not ctail or _SHORT_ID_RE.match(chead) is None:
            continue
        if not param.endswith("-" + ctail):
            continue
        longhead = param[: -(len(ctail) + 1)]
        if len(longhead) <= _PYTEST_LONG_STR_THRESHOLD:
            continue
        cands.append(full)
    return cands


def resolve_manifest_ids(
    manifest_ids: list[str], collected_ids: list[str]
) -> dict[str, list[str]]:
    """Map manifest test IDs to collected full IDs (collected order).

    The dataset truncates parametrized IDs at the first whitespace
    (an artifact of the official ``-rA`` summary parser, which reads
    the second whitespace token). A manifest ID therefore resolves on
    EXACT match, or when the collected ID starts with the manifest ID
    followed by a whitespace boundary. Step 4.3b.5/R8 adds three
    LAST-RESORT fallbacks (each strictly on uniqueness, bracket-closed
    parametrized IDs only — vitest ``>`` IDs never engage them):

    * suffix: the manifest path-like bracket (``/`` required) is a
      suffix of the collected bracket (env-dependent absolute paths,
      e.g. ``__file__`` values);
    * longstr: the manifest head exceeds pytest's 100-char threshold
      and the collected head is its ``argname+index`` short rendering
      (identical dash-tails);
    * exhaustion: exactly one manifest and one collected leftover
      under a node with equal case counts, where the collected side
      hides its true value behind a pure short ID and the manifest
      side is path-like (``/`` required — the observed lossy
      ``/``+basename abbreviation shape).

    Matches keep collected order; unresolvable IDs map to ``[]``
    (loudly visible, never silently dropped).
    """
    collected = list(collected_ids)
    resolution: dict[str, list[str]] = {}
    for mid in manifest_ids:
        if mid in collected:
            resolution[mid] = [mid]
            continue
        matches = [
            full for full in collected
            if len(full) > len(mid)
            and full.startswith(mid)
            and full[len(mid)].isspace()
        ]
        resolution[mid] = matches
    for mid in manifest_ids:
        if resolution[mid]:
            continue
        node, param = _node_of(mid), _bracket_of(mid)
        if param is None:
            continue
        cands = _suffix_candidates(node, param, collected)
        if len(cands) == 1:
            resolution[mid] = cands
    for mid in manifest_ids:
        if resolution[mid]:
            continue
        node, param = _node_of(mid), _bracket_of(mid)
        if param is None:
            continue
        cands = _longstr_candidates(node, param, collected)
        if len(cands) == 1:
            resolution[mid] = cands
    used: set[str] = set()
    for matches in resolution.values():
        used.update(matches)
    leftovers: dict[str, list[str]] = {}
    for mid in manifest_ids:
        if not resolution[mid] and _bracket_of(mid) is not None:
            leftovers.setdefault(_node_of(mid), []).append(mid)
    for node, mleft in leftovers.items():
        if len(mleft) != 1:
            continue
        cleft = [
            full for full in collected
            if _node_of(full) == node and full not in used
        ]
        if len(cleft) != 1:
            continue
        mcount = sum(1 for mid in manifest_ids if _node_of(mid) == node)
        ccount = sum(1 for full in collected if _node_of(full) == node)
        if mcount != ccount:
            continue
        cparam = _bracket_of(cleft[0])
        mparam = _bracket_of(mleft[0])
        if cparam is None or mparam is None:
            continue
        if _SHORT_ID_RE.match(cparam) is None:
            continue
        if "/" not in mparam:
            continue
        resolution[mleft[0]] = cleft
    return resolution


def remap_collected_index(
    resolution: dict[str, list[str]], collected_index: dict[str, str]
) -> dict[str, str]:
    """Remap a collected-ID status index onto manifest IDs.

    Ambiguous prefixes (several collected IDs per manifest ID) resolve
    LAST-WINS in collected order, mirroring the official parser (later
    ``-rA`` summary lines overwrite earlier ones for the same truncated
    key). Unresolved or absent IDs map to ``missing``.
    """
    manifest_index: dict[str, str] = {}
    for mid, matches in resolution.items():
        if not matches:
            manifest_index[mid] = STATUS_MISSING
            continue
        manifest_index[mid] = collected_index.get(
            matches[-1], STATUS_MISSING
        )
    return manifest_index


def judge_red(
    exit_code: int, junit_bytes: bytes, timed_out: bool = False
) -> dict[str, object]:
    """Judge a red-baseline run: must FAIL with assertion failures.

    Acceptance rule (mirrors ``RegressionTestSynthesizer``): non-zero
    exit code AND at least one ``<failure>`` AND zero ``<error>``
    entries. Collection/syntax breakage (``<error>``) rejects the run
    as a broken test rather than a proven reproduction.
    """
    failures = errors = 0
    try:
        cases = parse_junit(junit_bytes)
    except ValueError as exc:
        return {
            "accepted": False,
            "reason": f"red JUnit unusable: {exc}",
            "failures": 0,
            "errors": 0,
            "exit_code": exit_code,
            "timed_out": timed_out,
        }
    for case in cases:
        if case["status"] == STATUS_FAILED:
            failures += 1
        elif case["status"] == STATUS_ERROR:
            errors += 1
    if timed_out:
        reason = "red run timed out"
        accepted = False
    elif exit_code == 0:
        reason = "red run exited 0 (test passed on unpatched base)"
        accepted = False
    elif errors:
        reason = f"red run has {errors} error(s): broken test, not a repro"
        accepted = False
    elif not failures:
        reason = "red run failed without assertion failures"
        accepted = False
    else:
        reason = f"red accepted ({failures} failure(s), 0 errors)"
        accepted = True
    return {
        "accepted": accepted,
        "reason": reason,
        "failures": failures,
        "errors": errors,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }


def score_task(
    task: dict, base_index: dict[str, str], agent_index: dict[str, str]
) -> dict[str, object]:
    """Score one task run against its measured base run.

    Args:
        task: Manifest task (F2P/P2P/hidden_golden lists).
        base_index: Normalized test-ID -> status map at base+test_patch.
        agent_index: Same map after the agent patch (+test_patch).

    A FAIL_TO_PASS test counts as fixed only when it is red
    (failed/error) at base and passed after the patch. A PASS_TO_PASS
    test that is not green at base is ``env_limited`` (sandbox or
    dependency drift — reported, never silently counted against the
    agent). ``resolved_functional`` requires every F2P fixed and zero
    broken P2P tests. Guard gating is applied by the caller (Mode B).
    """
    f2p = list(task.get("fail_to_pass", []))
    p2p = list(task.get("pass_to_pass", []))
    golden = set(task.get("hidden_golden", []))
    f2p_rows: dict[str, dict[str, str]] = {}
    p2p_rows: dict[str, dict[str, str]] = {}
    f2p_fixed: list[str] = []
    f2p_not_fixed: list[str] = []
    f2p_not_red_at_base: list[str] = []
    for raw in f2p:
        base_status = base_index.get(raw, STATUS_MISSING)
        agent_status = agent_index.get(raw, STATUS_MISSING)
        f2p_rows[raw] = {"base": base_status, "agent": agent_status}
        if base_status not in _NON_PASS:
            f2p_not_red_at_base.append(raw)
            f2p_not_fixed.append(raw)
        elif agent_status == STATUS_PASSED:
            f2p_fixed.append(raw)
        else:
            f2p_not_fixed.append(raw)
    p2p_broken: list[str] = []
    p2p_env_limited: list[str] = []
    p2p_kept: list[str] = []
    for raw in p2p:
        base_status = base_index.get(raw, STATUS_MISSING)
        agent_status = agent_index.get(raw, STATUS_MISSING)
        p2p_rows[raw] = {
            "base": base_status,
            "agent": agent_status,
            "golden": "yes" if raw in golden else "no",
        }
        if base_status != STATUS_PASSED:
            p2p_env_limited.append(raw)
        elif agent_status == STATUS_PASSED:
            p2p_kept.append(raw)
        else:
            p2p_broken.append(raw)
    resolved = (
        len(f2p) > 0
        and not f2p_not_fixed
        and not f2p_not_red_at_base
        and not p2p_broken
    )
    unmatched = sorted(set(agent_index) - set(f2p) - set(p2p))
    missing_ids = sorted(
        [i for i in f2p + p2p if agent_index.get(i, STATUS_MISSING)
         == STATUS_MISSING]
    )
    return {
        "f2p": f2p_rows,
        "p2p": p2p_rows,
        "f2p_fixed": f2p_fixed,
        "f2p_not_fixed": f2p_not_fixed,
        "f2p_not_red_at_base": f2p_not_red_at_base,
        "p2p_broken": p2p_broken,
        "p2p_env_limited": p2p_env_limited,
        "p2p_kept": p2p_kept,
        "resolved_functional": resolved,
        "unmatched_agent_cases": unmatched,
        "missing_agent_ids": missing_ids,
    }


def summarize_results(results: list[dict]) -> dict[str, object]:
    """Aggregate per-run result dicts into accounting totals.

    Every result must carry ``status`` (one of ``resolved``,
    ``failed``, ``infra_error``) — unknown values raise instead of
    being silently bucketed.
    """
    allowed = {"resolved", "failed", "infra_error"}
    by_mode: dict[str, dict[str, int]] = {}
    total = 0
    for result in results:
        status = result.get("status")
        if status not in allowed:
            raise ValueError(f"unknown result status: {status!r}")
        mode = str(result.get("mode", "?"))
        bucket = by_mode.setdefault(
            mode, {"resolved": 0, "failed": 0, "infra_error": 0}
        )
        bucket[status] += 1  # type: ignore[literal-required]
        total += 1
    return {"total_runs": total, "by_mode": by_mode}


def _evidence_paths(
    root: Path, mode: str, task_id: str
) -> dict[str, Path]:
    """Artifact paths for one run's Step 4.3b.7 evidence bundle."""
    return {
        "collect_out": (
            root / "collect_reports" / mode / f"{task_id}.out"
        ),
        "collect_err": (
            root / "collect_reports" / mode / f"{task_id}.err"
        ),
        "freeze": (
            root / "versions" / mode / f"{task_id}.pip-freeze.txt"
        ),
        "debate_prompts": (
            root / "prompts" / mode / task_id / "debate_prompts.json"
        ),
    }


def audit_results_dir(out_dir: Path | str) -> dict[str, object]:
    """Audit a results directory for artifact completeness.

    For every ``results/<mode>/<task>.json`` present, checks that the
    recorded ``junit_sha256`` matches the bytes of
    ``junit_reports/<mode>/<task>.xml`` (when the record carries one),
    and that every Mode B run has a guard report. Step 4.3b.7 (R12):
    records carrying an ``evidence`` block (runs produced after the
    archival rollout) additionally require their flagged artifacts —
    collect stdout/stderr, pip freeze, debate prompts — plus two
    flag-independent expectations (python eval ⟹ collect+freeze,
    debated B-run ⟹ prompts). Grandfathered records without the
    block are skipped. Returns the loaded runs plus the offending
    task lists (empty when complete).
    """
    root = Path(out_dir)
    runs: list[dict] = []
    missing_guard: list[str] = []
    junit_mismatch: list[str] = []
    missing_evidence: list[str] = []
    results_root = root / "results"
    if results_root.is_dir():
        for mode_dir in sorted(results_root.iterdir()):
            if not mode_dir.is_dir():
                continue
            for record_path in sorted(mode_dir.glob("*.json")):
                try:
                    record = json.loads(
                        record_path.read_text(encoding="utf-8")
                    )
                except ValueError:
                    junit_mismatch.append(
                        f"{mode_dir.name}/{record_path.stem}:unparseable"
                    )
                    continue
                if not isinstance(record, dict):
                    junit_mismatch.append(
                        f"{mode_dir.name}/{record_path.stem}:not-a-dict"
                    )
                    continue
                runs.append(record)
                task_id = record.get("task", record_path.stem)
                mode = record.get("mode", mode_dir.name)
                expected = record.get("junit_sha256")
                if expected:
                    junit_path = (
                        root / "junit_reports" / str(mode)
                        / f"{task_id}.xml"
                    )
                    if not junit_path.is_file():
                        junit_mismatch.append(f"{mode}/{task_id}:missing-xml")
                    elif sha256_bytes(junit_path.read_bytes()) != expected:
                        junit_mismatch.append(f"{mode}/{task_id}:sha-drift")
                if mode == "B":
                    guard_path = (
                        root / "guard_reports" / "B" / f"{task_id}.json"
                    )
                    if not guard_path.is_file():
                        missing_guard.append(str(task_id))
                evidence = record.get("evidence")
                if isinstance(evidence, dict):
                    want_collect = bool(evidence.get("collect"))
                    want_freeze = bool(evidence.get("freeze"))
                    want_prompts = bool(evidence.get("debate_prompts"))
                    if (
                        evidence.get("language") == "python"
                        and record.get("junit_sha256")
                    ):
                        # A python eval ran ⟹ collection + freeze ran.
                        want_collect = True
                        want_freeze = True
                    agent = record.get("agent")
                    debate = (
                        agent.get("debate", {})
                        if isinstance(agent, dict) else {}
                    )
                    rounds = (
                        debate.get("rounds_used", 0)
                        if isinstance(debate, dict) else 0
                    )
                    if mode == "B" and int(rounds or 0) > 0:
                        # A debate ran ⟹ its prompts were recorded.
                        want_prompts = True
                    paths = _evidence_paths(root, str(mode), str(task_id))
                    if want_collect:
                        if not paths["collect_out"].is_file():
                            missing_evidence.append(
                                f"{mode}/{task_id}:missing-collect-out"
                            )
                        if not paths["collect_err"].is_file():
                            missing_evidence.append(
                                f"{mode}/{task_id}:missing-collect-err"
                            )
                    if want_freeze and not paths["freeze"].is_file():
                        missing_evidence.append(
                            f"{mode}/{task_id}:missing-pip-freeze"
                        )
                    if want_prompts and not paths["debate_prompts"].is_file():
                        missing_evidence.append(
                            f"{mode}/{task_id}:missing-debate-prompts"
                        )
    return {
        "runs": runs,
        "missing_guard_reports": missing_guard,
        "junit_mismatches": junit_mismatch,
        "missing_evidence_reports": missing_evidence,
    }
