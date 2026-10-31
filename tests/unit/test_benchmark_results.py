"""Step 4.2: benchmark scoring + results-directory validation (offline).

Covers ``benchmarks/bench_lib.py`` with static JUnit fixtures (parser
vectors, not fabricated run results): pytest/vitest parsing, red
judging, F2P/P2P scoring against a measured base, run accounting, and
the results-dir audit (JUnit SHA cross-check + Mode B guard presence).
A final test audits the REAL ``benchmarks/results/`` tree: it passes on
whatever runs exist (including zero, pre-matrix) and fails only on
genuine inconsistency.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH_DIR))

import bench_lib
import run_benchmark
from hackproof.patcher import PatchApplicationError
from bench_lib import (
    audit_results_dir,
    guard_files_for,
    index_pytest_cases,
    index_vitest_cases,
    judge_red,
    load_manifest,
    parse_junit,
    score_task,
    summarize_results,
    task_by_id,
)

RESULTS_DIR = BENCH_DIR / "results"
MANIFEST_PATH = BENCH_DIR / "datasets" / "tasks_manifest.json"

PYTEST_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="4">
  <testcase classname="test_requests.RequestsTestCase" name="test_no_content_length" file="test_requests.py">
    <failure message="assert False">traceback</failure>
  </testcase>
  <testcase classname="RequestsTestCase" name="test_basic_building" file="test_requests.py" />
  <testcase classname="test_requests.RequestsTestCase" name="test_broken" file="test_requests.py">
    <error message="collection">boom</error>
  </testcase>
  <testcase classname="test_requests.RequestsTestCase" name="test_skip_me" file="test_requests.py">
    <skipped message="not today" />
  </testcase>
</testsuite>
"""

# Step 4.3a (R1/R2): old pytest (<6) omits the `file` attribute; real
# attribute spellings lifted from the archived 4.2 base reports.
PYTEST_JUNIT_NO_FILE = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3">
  <testcase classname="test_requests.RequestsTestCase" name="test_no_content_length">
    <failure message="assert False">traceback</failure>
  </testcase>
  <testcase classname="testing.test_unittest" name="test_simple_unittest" />
  <testcase classname="elsewhere.mod.Klass" name="test_stray" />
</testsuite>
"""

PYTEST_JUNIT_MODULE_LEVEL = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2">
  <testcase classname="testing.code.test_code" name="test_ne" file="testing/code/test_code.py" />
  <testcase classname="testing.code.test_code.TestExceptionInfo" name="test_bad_getsource" file="testing/code/test_code.py" />
</testsuite>
"""

VITEST_JUNIT_ATTR = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="apiWatch.spec.ts">
    <testcase classname="packages/runtime-core/__tests__/apiWatch.spec.ts" name="api: watch &gt; should be executed correctly" file="packages/runtime-core/__tests__/apiWatch.spec.ts" />
    <testcase classname="packages/runtime-core/__tests__/apiWatch.spec.ts" name="api: watch &gt; effect" file="packages/runtime-core/__tests__/apiWatch.spec.ts">
      <failure message="expected true">traceback</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

VITEST_JUNIT_EMBEDDED = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="vitest">
  <testcase classname="" name="packages/runtime-core/__tests__/hydration.spec.ts &gt; SSR hydration &gt; text node" file="" />
</testsuite>
"""


def _task() -> dict:
    return {
        "id": "demo__pkg-1",
        "language": "python",
        "fail_to_pass": ["test_x.py::TestX::test_fix"],
        "pass_to_pass": ["test_x.py::TestX::test_keep",
                         "test_x.py::TestX::test_net"],
        "hidden_golden": ["test_x.py::TestX::test_keep"],
    }


def test_parse_junit_pytest_statuses() -> None:
    cases = parse_junit(PYTEST_JUNIT.encode("utf-8"))
    by_name = {case["name"]: case["status"] for case in cases}
    assert by_name == {
        "test_no_content_length": "failed",
        "test_basic_building": "passed",
        "test_broken": "error",
        "test_skip_me": "skipped",
    }
    assert cases[0]["message"] == "assert False"


def test_parse_junit_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_junit(b"not xml at all <<<")
    with pytest.raises(ValueError):
        parse_junit(b"<testsuite name='empty' tests='0'></testsuite>")


def test_index_pytest_canonical_ids() -> None:
    index = index_pytest_cases(parse_junit(PYTEST_JUNIT.encode("utf-8")))
    assert index["test_requests.py::RequestsTestCase::test_no_content_length"] == "failed"
    # Bare classnames and dotted module paths normalize identically.
    assert index["test_requests.py::RequestsTestCase::test_basic_building"] == "passed"
    assert index["test_requests.py::RequestsTestCase::test_broken"] == "error"


def test_index_pytest_no_file_attr_falls_back_to_classname() -> None:
    files = {"test_requests.py", "testing/test_unittest.py"}
    index = index_pytest_cases(
        parse_junit(PYTEST_JUNIT_NO_FILE.encode("utf-8")), known_files=files
    )
    assert index["test_requests.py::RequestsTestCase::test_no_content_length"] == "failed"
    assert index["testing/test_unittest.py::test_simple_unittest"] == "passed"
    # Unresolvable classnames stay missing (loud), never invented.
    assert not any("test_stray" in node for node in index)


def test_index_pytest_module_function_has_no_class_segment() -> None:
    index = index_pytest_cases(parse_junit(PYTEST_JUNIT_MODULE_LEVEL.encode("utf-8")))
    assert index["testing/code/test_code.py::test_ne"] == "passed"
    assert index["testing/code/test_code.py::TestExceptionInfo::test_bad_getsource"] == "passed"


def test_index_vitest_both_spellings() -> None:
    attr = index_vitest_cases(parse_junit(VITEST_JUNIT_ATTR.encode("utf-8")))
    assert attr["packages/runtime-core/__tests__/apiWatch.spec.ts > api: watch > should be executed correctly"] == "passed"
    assert attr["packages/runtime-core/__tests__/apiWatch.spec.ts > api: watch > effect"] == "failed"
    embedded = index_vitest_cases(
        parse_junit(VITEST_JUNIT_EMBEDDED.encode("utf-8"))
    )
    assert embedded["packages/runtime-core/__tests__/hydration.spec.ts > SSR hydration > text node"] == "passed"


def test_index_vitest_names_with_angle_brackets_verbatim() -> None:
    # Step 4.3a (R3): real spellings from the archived 11589/11899 reports.
    junit = (
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
        '  <testsuite name="apiWatch.spec.ts">\n'
        '    <testcase classname="packages/runtime-core/__tests__/apiWatch.spec.ts"'
        ' name="api: watch &gt; watching sources: ref&lt;any[]&gt;" />\n'
        "  </testsuite>\n"
        '  <testsuite name="compileStyle.spec.ts">\n'
        '    <testcase classname="packages/compiler-sfc/__tests__/compileStyle.spec.ts"'
        ' name="SFC scoped CSS &gt; deprecated syntax &gt; &gt;&gt;&gt; (deprecated syntax)" />\n'
        "  </testsuite>\n</testsuites>\n"
    )
    index = index_vitest_cases(parse_junit(junit.encode("utf-8")))
    assert index["packages/runtime-core/__tests__/apiWatch.spec.ts > api: watch > watching sources: ref<any[]>"] == "passed"
    assert index["packages/compiler-sfc/__tests__/compileStyle.spec.ts > SFC scoped CSS > deprecated syntax > >>> (deprecated syntax)"] == "passed"


def test_judge_red_accepts_failure() -> None:
    verdict = judge_red(1, PYTEST_JUNIT.encode("utf-8"))
    # Fixture carries an <error> case -> broken test, must reject.
    assert verdict["accepted"] is False
    assert "error" in str(verdict["reason"])
    clean_red = PYTEST_JUNIT.replace(
        '  <testcase classname="test_requests.RequestsTestCase" '
        'name="test_broken" file="test_requests.py">\n'
        '    <error message="collection">boom</error>\n'
        "  </testcase>\n",
        "",
    )
    accepted = judge_red(1, clean_red.encode("utf-8"))
    assert accepted["accepted"] is True
    assert accepted["failures"] == 1
    assert accepted["errors"] == 0


def test_judge_red_rejects_pass_and_timeout() -> None:
    green = (
        "<testsuite><testcase classname='C' name='t' file='f.py' />"
        "</testsuite>"
    )
    assert judge_red(0, green.encode("utf-8"))["accepted"] is False
    assert judge_red(1, b"garbage", timed_out=True)["accepted"] is False


def test_score_task_resolved() -> None:
    task = _task()
    base = {
        "test_x.py::TestX::test_fix": "failed",
        "test_x.py::TestX::test_keep": "passed",
        "test_x.py::TestX::test_net": "passed",
    }
    agent = {
        "test_x.py::TestX::test_fix": "passed",
        "test_x.py::TestX::test_keep": "passed",
        "test_x.py::TestX::test_net": "passed",
    }
    scoring = score_task(task, base, agent)
    assert scoring["resolved_functional"] is True
    assert scoring["f2p_fixed"] == ["test_x.py::TestX::test_fix"]
    assert scoring["p2p_broken"] == []
    assert scoring["p2p_env_limited"] == []


def test_score_task_env_limited_p2p_excluded() -> None:
    task = _task()
    base = {
        "test_x.py::TestX::test_fix": "failed",
        "test_x.py::TestX::test_keep": "passed",
        "test_x.py::TestX::test_net": "failed",
    }
    agent = {
        "test_x.py::TestX::test_fix": "passed",
        "test_x.py::TestX::test_keep": "passed",
        "test_x.py::TestX::test_net": "failed",
    }
    scoring = score_task(task, base, agent)
    assert scoring["resolved_functional"] is True
    assert scoring["p2p_env_limited"] == ["test_x.py::TestX::test_net"]
    assert scoring["p2p_broken"] == []


def test_score_task_broken_p2p_and_cold_f2p_fail() -> None:
    task = _task()
    base = {
        "test_x.py::TestX::test_fix": "failed",
        "test_x.py::TestX::test_keep": "passed",
        "test_x.py::TestX::test_net": "passed",
    }
    agent = dict(base)
    agent["test_x.py::TestX::test_fix"] = "passed"
    agent["test_x.py::TestX::test_keep"] = "failed"
    scoring = score_task(task, base, agent)
    assert scoring["resolved_functional"] is False
    assert scoring["p2p_broken"] == ["test_x.py::TestX::test_keep"]
    cold_base = dict(base)
    cold_base["test_x.py::TestX::test_fix"] = "passed"
    cold = score_task(task, cold_base, agent)
    assert cold["resolved_functional"] is False
    assert cold["f2p_not_red_at_base"] == ["test_x.py::TestX::test_fix"]


def test_summarize_results_accounting() -> None:
    rows = [
        {"mode": "A", "status": "resolved"},
        {"mode": "A", "status": "failed"},
        {"mode": "B", "status": "infra_error"},
        {"mode": "B", "status": "resolved"},
    ]
    summary = summarize_results(rows)
    assert summary["total_runs"] == 4
    assert summary["by_mode"]["A"] == {
        "resolved": 1, "failed": 1, "infra_error": 0,
    }
    assert summary["by_mode"]["B"]["infra_error"] == 1
    with pytest.raises(ValueError):
        summarize_results([{"mode": "A", "status": "mystery"}])


def test_guard_files_derive_from_test_ids() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    root_case = task_by_id(manifest, "psf__requests-1142")
    assert guard_files_for(root_case) == ["test_requests.py"]
    ts_case = task_by_id(manifest, "vuejs__core-11870")
    assert guard_files_for(ts_case) == [
        "packages/runtime-core/__tests__/helpers/renderList.spec.ts"
    ]


def test_audit_results_dir_detects_gaps(tmp_path: Path) -> None:
    out = tmp_path / "results"
    (out / "results" / "B").mkdir(parents=True)
    (out / "junit_reports" / "B").mkdir(parents=True)
    junit = PYTEST_JUNIT.encode("utf-8")
    (out / "junit_reports" / "B" / "t1.xml").write_bytes(junit)
    (out / "results" / "B" / "t1.json").write_text(
        json.dumps(
            {
                "task": "t1", "mode": "B", "status": "resolved",
                "junit_sha256": hashlib.sha256(junit).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    audit = audit_results_dir(out)
    assert audit["junit_mismatches"] == []
    # Guard report absent -> detected.
    assert audit["missing_guard_reports"] == ["t1"]
    (out / "guard_reports" / "B").mkdir(parents=True)
    (out / "guard_reports" / "B" / "t1.json").write_text("{}", encoding="utf-8")
    audit = audit_results_dir(out)
    assert audit["missing_guard_reports"] == []
    # Tampered JUnit bytes -> SHA drift detected.
    (out / "junit_reports" / "B" / "t1.xml").write_bytes(junit + b" ")
    audit = audit_results_dir(out)
    assert audit["junit_mismatches"] == ["B/t1:sha-drift"]


def test_real_results_dir_is_consistent() -> None:
    audit = audit_results_dir(RESULTS_DIR)
    assert audit["junit_mismatches"] == []
    assert audit["missing_guard_reports"] == []
    # Grandfathered 4.2/4.3a records carry no evidence block: the
    # Step 4.3b.7 bundle requirements must not fire on the archive.
    assert audit["missing_evidence_reports"] == []
    runs = [r for r in audit["runs"] if isinstance(r, dict)]
    summary = summarize_results(
        [r for r in runs if r.get("mode") in ("A", "B")]
    )
    assert summary["total_runs"] == len(
        [r for r in runs if r.get("mode") in ("A", "B")]
    )
    for run in runs:
        if run.get("mode") in ("A", "B"):
            assert run.get("status") in ("resolved", "failed", "infra_error")
        assert run.get("task")


def test_bench_lib_exports_stable() -> None:
    for name in (
        "parse_junit", "judge_red", "score_task", "summarize_results",
        "audit_results_dir", "guard_files_for", "resolve_manifest_ids",
        "remap_collected_index", "parse_collect_output",
    ):
        assert callable(getattr(bench_lib, name)), name


def test_parse_collect_output() -> None:
    text = (
        "testing/test_a.py::test_one\n"
        "testing/test_a.py::TestC::test_two[x y]\n"
        "\n"
        "ERROR testing/test_b.py - collection error\n"
        "  indented::noise\n"
        "1 warning in 0.01s\n"
    )
    assert bench_lib.parse_collect_output(text) == [
        "testing/test_a.py::test_one",
        "testing/test_a.py::TestC::test_two[x y]",
    ]


def test_resolve_manifest_ids_exact_and_truncated() -> None:
    manifest = [
        "f.py::test_plain",
        "f.py::TestC::test_p[a",
        "f.py::TestC::test_q",
    ]
    collected = [
        "f.py::test_plain",
        "f.py::TestC::test_p[a b]",
        "f.py::TestC::test_p[a c]",
        "f.py::TestC::test_qq",
    ]
    resolution = bench_lib.resolve_manifest_ids(manifest, collected)
    assert resolution["f.py::test_plain"] == ["f.py::test_plain"]
    # Truncated ID expands over the whitespace boundary (both matches).
    assert resolution["f.py::TestC::test_p[a"] == [
        "f.py::TestC::test_p[a b]",
        "f.py::TestC::test_p[a c]",
    ]
    # Bare stem without boundary never prefix-matches a longer name.
    assert resolution["f.py::TestC::test_q"] == []


def test_remap_collected_index_last_wins() -> None:
    resolution = {
        "m1": ["c1"],
        "m2": ["c2a", "c2b"],
        "m3": [],
    }
    collected_index = {"c1": "failed", "c2a": "passed", "c2b": "failed"}
    remapped = bench_lib.remap_collected_index(resolution, collected_index)
    assert remapped == {"m1": "failed", "m2": "failed", "m3": "missing"}
    remapped = bench_lib.remap_collected_index(
        resolution, {"c1": "passed", "c2a": "failed"}
    )
    assert remapped["m2"] == "missing"


def test_manifest_ids_and_flatten_helpers() -> None:
    task = _task()
    ids = run_benchmark.manifest_ids_of(task)
    assert ids == [
        "test_x.py::TestX::test_fix",
        "test_x.py::TestX::test_keep",
        "test_x.py::TestX::test_net",
    ]
    flat = run_benchmark.flatten_resolution(
        {"a": ["c1", "c2"], "b": ["c2", "c3"]}
    )
    assert flat == ["c1", "c2", "c3"]


def test_pytest_eval_argv_exact_nodes() -> None:
    argv = run_benchmark.pytest_eval_argv(
        ["test_x.py::TestX::test_fix", "test_x.py::TestX::test_keep"],
        "eval.xml",
    )
    assert argv[:2] == ["./.bench-venv/bin/python", "-m"]
    assert "--junitxml" in argv and "eval.xml" in argv
    assert "test_x.py::TestX::test_fix" in argv
    assert "test_x.py::TestX::test_keep" in argv
    assert "bash" not in argv[0]
    assert "-o" not in argv and "addopts=" not in argv


def test_vitest_eval_argv_node_routed() -> None:
    task = {
        "id": "demo__pkg-2",
        "language": "typescript",
        "fail_to_pass": ["pkg/__tests__/a.spec.ts > suite > it"],
        "pass_to_pass": [],
        "hidden_golden": [],
    }
    argv = run_benchmark.vitest_eval_argv(task, "eval.xml")
    # Node ELF entry point; the real JS entrypoint is an argument
    # (runsc-safe). The .bin/vitest pnpm shim is a shell script and
    # dies under node with SyntaxError (Step 4.2 live evidence).
    assert argv[:2] == ["node", "./node_modules/vitest/vitest.mjs"]
    assert "--reporter=junit" in argv
    assert "pkg/__tests__/a.spec.ts" in argv


def test_scm_repos_and_fingerprint() -> None:
    assert "pytest-dev/pytest" in run_benchmark.SCM_VERSIONED_REPOS
    assert "psf/requests" not in run_benchmark.SCM_VERSIONED_REPOS
    assert run_benchmark.OCI_RUNTIME == "runc"
    py_fp = run_benchmark.prep_fingerprint(
        {"language": "python", "repo": "psf/requests",
         "id": "psf__requests-6028"}
    )
    assert py_fp["image"] == run_benchmark.PY_IMAGE
    assert py_fp["history"] == "shallow"
    scm_fp = run_benchmark.prep_fingerprint(
        {"language": "python", "repo": "pytest-dev/pytest",
         "id": "pytest-dev__pytest-7432"}
    )
    assert scm_fp["history"] == "full"
    assert scm_fp["image"] == "python:3.8-slim"
    assert run_benchmark.py_image_for({"id": "psf__requests-6028"}) == (
        run_benchmark.PY_IMAGE
    )
    ts_fp = run_benchmark.prep_fingerprint(
        {"language": "typescript", "repo": "vuejs/core"}
    )
    assert ts_fp["image"] == run_benchmark.TS_IMAGE
    assert py_fp != ts_fp


class _ScriptedRunner:
    """Fake sandbox runner replaying scripted results."""

    def __init__(self, script: list[tuple[int, str, str]]) -> None:
        self._script = list(script)
        self.calls = 0

    def run_in_sandbox(self, cmd, cwd, timeout_sec=60,
                       network_disabled=True):  # noqa: ANN001, ANN202
        from types import SimpleNamespace

        self.calls += 1
        exit_code, stdout, stderr = self._script[
            min(self.calls - 1, len(self._script) - 1)
        ]
        return SimpleNamespace(
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            duration_ms=1, timed_out=False,
        )


def test_robust_run_retries_silent_flake(tmp_path: Path) -> None:
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        runner = _ScriptedRunner([(2, "", ""), (0, "ok", "")])
        res = run_benchmark.robust_run(
            runner, ["true"], ".", 60, True, tee, "stage",
            max_attempts=2,
        )
        assert res.exit_code == 0
        assert runner.calls == 2
    finally:
        tee.close()


def test_local_fixup_venv_repoints_pth(tmp_path: Path) -> None:
    prep = tmp_path / "prep"
    dest = tmp_path / "A"
    site = dest / ".bench-venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    pth = site / "__editable__.demo-1.pth"
    pth.write_text(f"{prep}/src\n", encoding="utf-8")
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        run_benchmark.local_fixup_venv(prep, dest, tee)
    finally:
        tee.close()
    assert pth.read_text(encoding="utf-8").strip() == f"{dest}/src"
    pth.write_text("import something", encoding="utf-8")
    tee = run_benchmark.Tee(tmp_path / "t2.log")
    try:
        with pytest.raises(RuntimeError, match="unexpected content"):
            run_benchmark.local_fixup_venv(prep, dest, tee)
    finally:
        tee.close()


def test_robust_run_no_retry_on_loud_failure(tmp_path: Path) -> None:
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        runner = _ScriptedRunner([(1, "", "boom"), (0, "ok", "")])
        res = run_benchmark.robust_run(
            runner, ["false"], ".", 60, True, tee, "stage",
            max_attempts=3,
        )
        assert res.exit_code == 1
        assert runner.calls == 1
        assert res.stderr == "boom"
    finally:
        tee.close()


# ---------------------------------------------------------------------------
# Step 4.3b.3 (R6): single grounded patch revision in the benchmark loop
# ---------------------------------------------------------------------------


class _ReviseClient:
    """Scripted Wonder stand-in: one canned revision + fake usage."""

    def __init__(self, revised_patch: str) -> None:
        self._revised = revised_patch
        self.calls: list[tuple[str, str]] = []
        self.usage_log = [
            {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            }
        ]

    def debate_round(self, role: str, prompt: str, history: list) -> str:
        self.calls.append((role, prompt))
        return self._revised


def test_apply_blocks_single_retry_applies(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "a.py").write_text("x = 1\n", encoding="utf-8")
    revised = (
        "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
    )
    client = _ReviseClient(revised)
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        usage = run_benchmark.UsageCollector()
        applied = run_benchmark.apply_blocks_to_candidates(
            "task-1", clone, ["a.py"], [(None, "NOPE\n", "x = 2\n")], [],
            tee,
            retry_client=client, retry_issue="issue", retry_context="ctx",
            retry_patch_text="old-patch", usage=usage,
            retry_archive=tmp_path / "patch_text_retry.txt",
        )
        assert applied == ["a.py"]
        assert (clone / "a.py").read_text(encoding="utf-8") == "x = 2\n"
        assert len(client.calls) == 1
        assert client.calls[0][0] == "ArchitecturalBuilder"
        assert "SEARCH not found" in client.calls[0][1]
        assert (tmp_path / "patch_text_retry.txt").read_text(
            encoding="utf-8"
        ) == revised
        assert usage.per_call[-1]["role"] == "ArchitecturalBuilder-retry"
        assert usage.per_call[-1]["total_tokens"] == 8
    finally:
        tee.close()


def test_apply_blocks_without_retry_client_raises(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "a.py").write_text("x = 1\n", encoding="utf-8")
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        with pytest.raises(PatchApplicationError, match="exactly once"):
            run_benchmark.apply_blocks_to_candidates(
                "task-1", clone, ["a.py"], [(None, "NOPE\n", "x = 2\n")],
                [], tee,
            )
        assert (clone / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    finally:
        tee.close()


# ---------------------------------------------------------------------------
# Step 4.3b.4 (R7): era image pin + bounded collect fallback + stderr archive
# ---------------------------------------------------------------------------

MUTABLE_MAPPING_STDERR = (
    "ERROR collecting test_requests.py\n"
    "E   AttributeError: module 'collections' has no attribute "
    "'MutableMapping'\n"
)


def _py_task(task_id: str) -> dict:
    return {
        "id": task_id,
        "language": "python",
        "repo": "psf/requests",
        "fail_to_pass": ["test_requests.py::TestRequests::test_binary_put"],
        "pass_to_pass": [],
        "hidden_golden": [],
    }


def test_py_image_for_pins_2931_to_era() -> None:
    assert run_benchmark.ERA_PY_IMAGE == "python:3.8-slim"
    assert run_benchmark.py_image_for({"id": "psf__requests-2931"}) == (
        "python:3.8-slim"
    )
    fp = run_benchmark.prep_fingerprint(_py_task("psf__requests-2931"))
    assert fp["image"] == "python:3.8-slim"
    # Siblings untouched: pinned stay era, modern stay 3.11.
    assert run_benchmark.py_image_for({"id": "psf__requests-1142"}) == (
        "python:3.8-slim"
    )
    assert run_benchmark.py_image_for({"id": "psf__requests-6028"}) == (
        run_benchmark.PY_IMAGE
    )


def test_should_era_fallback_matrix() -> None:
    fb = run_benchmark.should_era_fallback
    # Era signatures trigger (4.2 2931 shape: exit 2, 0 IDs).
    reason = fb("python:3.11-slim", 2, "", MUTABLE_MAPPING_STDERR, [])
    assert reason is not None and reason.startswith("era-signature:")
    lineno = fb(
        "python:3.11-slim", 2, "",
        'TypeError: required field "lineno" missing from alias', [],
    )
    assert lineno is not None and lineno.startswith("era-signature:")
    # Loud empty failure without a signature: generic bounded retry.
    generic = fb("python:3.11-slim", 2, "", "some other boom", [])
    assert generic == "collect-exit-2-empty"
    # Negatives: ids present, already era, exit-0-empty.
    assert fb("python:3.11-slim", 2, "", MUTABLE_MAPPING_STDERR, ["a"]) is None
    assert fb("python:3.8-slim", 2, "", MUTABLE_MAPPING_STDERR, []) is None
    assert fb("python:3.11-slim", 0, "", "", []) is None
    assert fb("python:3.11-slim", 0, "", "warnings only", []) is None


def test_collect_remote_full_archives_stderr(tmp_path: Path, monkeypatch) -> None:
    runner = _ScriptedRunner([(2, "", MUTABLE_MAPPING_STDERR)])
    seen: dict = {}

    def _fake_runner(cfg, image):  # noqa: ANN001, ANN202
        seen["image"] = image
        return runner

    monkeypatch.setattr(run_benchmark, "oci_sandbox_runner", _fake_runner)
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        ids, report = run_benchmark.collect_ids_remote_full(
            None, _py_task("psf__requests-2931"), "/fake/run", tee, "probe"
        )
    finally:
        tee.close()
    assert ids == []
    assert report["exit_code"] == 2
    assert report["image"] == "python:3.8-slim"
    assert report["n_ids"] == 0
    assert "MutableMapping" in report["stderr_tail"]
    assert seen["image"] == "python:3.8-slim"
    log = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "probe-collect stderr" in log
    assert "MutableMapping" in log


def test_collect_with_fallback_rescues_once(
    tmp_path: Path, monkeypatch
) -> None:
    images: list[str] = []

    def _fake_full(  # noqa: ANN001, ANN202
        cfg, task, run_dir, tee, stage, image=None, archive=None
    ):
        img = image or run_benchmark.py_image_for(task)
        images.append(img)
        if len(images) == 1:
            return [], {
                "image": img, "exit_code": 2, "n_ids": 0,
                "stdout_tail": "", "stderr_tail": MUTABLE_MAPPING_STDERR,
            }
        return ["test_requests.py::TestRequests::test_binary_put"], {
            "image": img, "exit_code": 0, "n_ids": 1,
            "stdout_tail": "test_requests.py::TestRequests::test_binary_put",
            "stderr_tail": "",
        }

    monkeypatch.setattr(
        run_benchmark, "collect_ids_remote_full", _fake_full
    )
    reprepped: list[str] = []
    monkeypatch.setattr(
        run_benchmark, "reprep_run_dir_era",
        lambda cfg, task, run_dir, tee: reprepped.append(run_dir),
    )
    refreshed: list[str] = []
    monkeypatch.setattr(
        run_benchmark, "refresh_shared_prep_era",
        lambda cfg, task, tee, reason: refreshed.append(reason),
    )
    task = _py_task("psf__requests-6028")
    run_benchmark._ERA_PINNED_RUNTIME.discard(task["id"])
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        ids, report = run_benchmark.collect_ids_remote_with_fallback(
            None, task, "/fake/run", tee, "base"
        )
        assert ids == ["test_requests.py::TestRequests::test_binary_put"]
        # Exactly two collects: original image, then the era image once.
        assert images == [run_benchmark.PY_IMAGE, "python:3.8-slim"]
        assert reprepped == ["/fake/run"]
        assert report["fallback"] is not None
        assert report["fallback"]["reason"].startswith("era-signature:")
        assert report["fallback"]["from_image"] == run_benchmark.PY_IMAGE
        assert report["fallback"]["to_image"] == "python:3.8-slim"
        assert report["fallback"]["first_exit"] == 2
        assert report["fallback"]["retry_n_ids"] == 1
        # Rescue pins the task for the rest of this process (loudly).
        assert task["id"] in run_benchmark._ERA_PINNED_RUNTIME
        assert run_benchmark.py_image_for(task) == "python:3.8-slim"
        assert len(refreshed) == 1
    finally:
        tee.close()
        run_benchmark._ERA_PINNED_RUNTIME.discard(task["id"])
    log = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "era-fallback" in log


def test_collect_with_fallback_no_retry_on_era(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def _fake_full(  # noqa: ANN001, ANN202
        cfg, task, run_dir, tee, stage, image=None, archive=None
    ):
        img = image or run_benchmark.py_image_for(task)
        calls.append(img)
        return [], {
            "image": img, "exit_code": 2, "n_ids": 0,
            "stdout_tail": "", "stderr_tail": MUTABLE_MAPPING_STDERR,
        }

    monkeypatch.setattr(
        run_benchmark, "collect_ids_remote_full", _fake_full
    )
    task = _py_task("psf__requests-2931")
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        ids, report = run_benchmark.collect_ids_remote_with_fallback(
            None, task, "/fake/run", tee, "base"
        )
    finally:
        tee.close()
    assert ids == []
    assert calls == ["python:3.8-slim"]
    assert report["fallback"] is None


def test_prep_is_fresh_rejects_image_change(
    tmp_path: Path, monkeypatch
) -> None:
    task = _py_task("psf__requests-2931")
    task["base_ref"] = "abc123"
    task["dataset_sha256"] = "def456"
    want = run_benchmark.prep_fingerprint(task)
    assert want["image"] == "python:3.8-slim"

    def _read_old(cfg, path, timeout=60):  # noqa: ANN001, ANN202
        stale = {
            "base_ref": "abc123",
            "dataset_sha256": "def456",
            "fingerprint": {**want, "image": "python:3.11-slim"},
        }
        return json.dumps(stale).encode("utf-8")

    monkeypatch.setattr(run_benchmark, "ssh_read_file", _read_old)
    run_benchmark._REMOTE_HOME_CACHE["u@h"] = "/home/u"
    cfg = run_benchmark.SshConfig(host="h", user="u", key_path="k")
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        # 4.2 marker (3.11 venv) is stale under the 4.3b.4 pin.
        assert run_benchmark.prep_is_fresh(cfg, task, want, tee) is False
    finally:
        tee.close()

    def _read_fresh(cfg, path, timeout=60):  # noqa: ANN001, ANN202
        fresh = {
            "base_ref": "abc123",
            "dataset_sha256": "def456",
            "fingerprint": want,
        }
        return json.dumps(fresh).encode("utf-8")

    monkeypatch.setattr(run_benchmark, "ssh_read_file", _read_fresh)
    tee = run_benchmark.Tee(tmp_path / "t2.log")
    try:
        assert run_benchmark.prep_is_fresh(cfg, task, want, tee) is True
    finally:
        tee.close()
        run_benchmark._REMOTE_HOME_CACHE.pop("u@h", None)


def test_reprep_run_dir_era_refuses_outside_root(tmp_path: Path) -> None:
    run_benchmark._REMOTE_HOME_CACHE["u@h"] = "/home/u"
    cfg = run_benchmark.SshConfig(host="h", user="u", key_path="k")
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        with pytest.raises(ValueError, match="outside runs root"):
            run_benchmark.reprep_run_dir_era(
                cfg, _py_task("x"), "/etc/evil", tee
            )
        # The runs root itself is equally refused (mirror ssh_rmtree).
        with pytest.raises(ValueError, match="outside runs root"):
            run_benchmark.reprep_run_dir_era(
                cfg, _py_task("x"), "/home/u/hackproof-bench/runs", tee
            )
    finally:
        tee.close()
        run_benchmark._REMOTE_HOME_CACHE.pop("u@h", None)


# ---------------------------------------------------------------------------
# Step 4.3b.5 (R8): suffix + longstr + exhaustion resolution fallbacks
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
# OCI probe archive, 2026-10-30T22:20:41Z (probe_collect.py, exit 0,
# python:3.11-slim, 214 IDs): byte-exact ground truth for 6028.
PROBE_6028_SHA256 = (
    "322b1d008cec9a2902d60720c35ba97b39e22bba2fce0a238300c071f3199233"
)


def test_resolve_suffix_fallback_paths() -> None:
    resolve = bench_lib.resolve_manifest_ids
    node = "tests/test_x.py::TestP::test_p"
    # Unique path-suffix: manifest basename vs absolute collected path.
    res = resolve(
        [f"{node}[/data.csv]"],
        [f"{node}[/]", f"{node}[/home/u/work/tests/data.csv]"],
    )
    assert res[f"{node}[/data.csv]"] == [
        f"{node}[/home/u/work/tests/data.csv]"
    ]
    # Ambiguous suffix (two collected share it) stays loud.
    res = resolve(
        [f"{node}[/data.csv]"],
        [f"{node}[/a/data.csv]", f"{node}[/b/data.csv]"],
    )
    assert res[f"{node}[/data.csv]"] == []
    # Cross-node suffix never matches.
    res = resolve(
        [f"{node}[/data.csv]"],
        ["tests/test_y.py::TestQ::test_q[/home/u/data.csv]"],
    )
    assert res[f"{node}[/data.csv]"] == []
    # Slash guard: short tokens must not suffix-pair ([0] vs [10]).
    res = resolve([f"{node}[0]"], [f"{node}[10]"])
    assert res[f"{node}[0]"] == []
    # Exact still wins over suffix.
    res = resolve(
        [f"{node}[/data.csv]"],
        [f"{node}[/data.csv]", f"{node}[/home/u/data.csv]"],
    )
    assert res[f"{node}[/data.csv]"] == [f"{node}[/data.csv]"]


def test_resolve_longstr_backmatch() -> None:
    resolve = bench_lib.resolve_manifest_ids
    node = "tests/test_x.py::test_auth"
    long_head = "http://example.com/" + "p" * 100  # 119 chars > 100
    assert len(long_head) > 100
    manifest = f"{node}[{long_head}-auth0]"
    short = f"{node}[url0-auth0]"
    # Full spelling back-matches its pytest-8 short rendering.
    res = resolve([manifest], [short, f"{node}[other-auth1]"])
    assert res[manifest] == [short]
    # Short head (would render full) never back-matches.
    res = resolve(
        [f"{node}[http://x.co-auth0]"], [short]
    )
    assert res[f"{node}[http://x.co-auth0]"] == []
    # Tail mismatch refuses.
    res = resolve([manifest], [f"{node}[url0-auth9]"])
    assert res[manifest] == []
    # Non-short collected head refuses.
    res = resolve([manifest], [f"{node}[urlx-auth0]"])
    assert res[manifest] == []
    # Ambiguous short candidates refuse.
    res = resolve(
        [manifest],
        [f"{node}[url0-auth0]", f"{node}[link0-auth0]"],
    )
    assert res[manifest] == []


def test_resolve_exhaustion_singletons() -> None:
    resolve = bench_lib.resolve_manifest_ids
    node = "tests/test_x.py::TestP::test_p"
    # Singleton leftovers, equal counts, hidden true value: forced pair.
    res = resolve(
        [f"{node}[/]", f"{node}[/a.py]", f"{node}[/__init__.py]"],
        [f"{node}[/]", f"{node}[/home/u/a.py]", f"{node}[path2]"],
    )
    assert res[f"{node}[/__init__.py]"] == [f"{node}[path2]"]
    # Unequal case counts: no forcing (version skew stays loud).
    res = resolve(
        [f"{node}[/]", f"{node}[/__init__.py]"],
        [f"{node}[/]", f"{node}[/home/u/a.py]", f"{node}[path2]"],
    )
    assert res[f"{node}[/__init__.py]"] == []
    # Collected leftover with visible spelling: no forcing.
    res = resolve(
        [f"{node}[/]", f"{node}[/__init__.py]"],
        [f"{node}[/]", f"{node}[/other.py]"],
    )
    assert res[f"{node}[/__init__.py]"] == []
    # Non-path manifest leftover: no forcing (slash guard).
    res = resolve([f"{node}[foo0]"], [f"{node}[bar0]"])
    assert res[f"{node}[foo0]"] == []
    res = resolve([f"{node}[aaa-x0]"], [f"{node}[bbb-y0]"])
    assert res[f"{node}[aaa-x0]"] == []
    # Non-short collected leftover with dashes: no forcing.
    res = resolve([f"{node}[/a-b]"], [f"{node}[cc-dd]"])
    assert res[f"{node}[/a-b]"] == []


def test_6028_probe_fixture_resolves_fully() -> None:
    ids_path = FIXTURES_DIR / "psf__requests-6028.probe.ids.txt"
    verdict_path = FIXTURES_DIR / "psf__requests-6028.probe.json"
    blob = ids_path.read_text(encoding="utf-8")
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    # Fixture integrity: byte-exact OCI archive (sha quoted, not fresh).
    assert hashlib.sha256(blob.encode("utf-8")).hexdigest() == (
        PROBE_6028_SHA256
    )
    assert verdict["ids_sha256"] == PROBE_6028_SHA256
    assert verdict["collect_exit"] == 0
    collected = blob.splitlines()
    assert len(collected) == 214
    manifest = load_manifest(MANIFEST_PATH)
    task = task_by_id(manifest, "psf__requests-6028")
    manifest_ids = run_benchmark.manifest_ids_of(task)
    assert len(manifest_ids) == 187
    resolution = bench_lib.resolve_manifest_ids(manifest_ids, collected)
    unresolved = [m for m, v in resolution.items() if not v]
    assert unresolved == []
    # The 4 formerly-unmeasurable pairs, quoting archived spellings.
    unz = "tests/test_utils.py::TestExtractZippedPaths::test_unzipped_paths_unchanged"
    assert resolution[f"{unz}[/test_utils.py]"] == [
        f"{unz}[/home/ubuntu/hackproof-bench/runs/psf__requests-6028"
        "/probe/tests/test_utils.py]"
    ]
    assert resolution[f"{unz}[/location]"] == [
        f"{unz}[/etc/invalid/location]"
    ]
    assert resolution[f"{unz}[/__init__.py]"] == [f"{unz}[path2]"]
    auth = "tests/test_utils.py::test_get_auth_from_url"
    auth_manifest = (
        f"{auth}[http://%25%21%2A%27%28%29%3B%3A%40%26%3D%2B%24%2C%2F%3F"
        "%23%5B%5D%20:%25%21%2A%27%28%29%3B%3A%40%26%3D%2B%24%2C%2F%3F"
        "%23%5B%5D%20@request.com/url.html#test-auth0]"
    )
    assert resolution[auth_manifest] == [f"{auth}[url0-auth0]"]
    # Mechanism check: the head exceeds pytest's 100-char short-render
    # threshold (154 chars observed), tails agree on "-auth0".
    head = auth_manifest.split("[", 1)[1][:-1].rsplit("-", 1)[0]
    assert len(head) == 154
    assert len(head) > 100
    assert auth_manifest.endswith("-auth0]")
    assert resolution[auth_manifest][0].endswith("[url0-auth0]")


# ---------------------------------------------------------------------------
# Step 4.3b.6 (R10): red-before-debates + one grounded resynthesis
# ---------------------------------------------------------------------------

# Byte-exact B-10081 artifacts (2026-10-30): the vacuous red that burned
# 13 debate/patch calls before its exit-0 rejection.
VACUOUS_RED_10081 = (
    'import unittest\n'
    '\n'
    '@unittest.skip("hello")\n'
    'class MyTestCase(unittest.TestCase):\n'
    '    def setUp(self):\n'
    '        xxx\n'
    '    def test_one(self):\n'
    '        pass\n'
    '    def tearDown(self):\n'
    '        xxx\n'
)
VACUOUS_RED_JUNIT_10081 = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites>'
    '<testsuite name="pytest" errors="0" failures="0" skipped="1" '
    'tests="1" time="0.055" timestamp="2026-10-30T13:25:38.994213" '
    'hostname="bdb4202e04b0">'
    '<testcase classname="testing.test_repro_eba6c0c721f7.MyTestCase" '
    'name="test_one" time="0.001">'
    '<skipped type="pytest.skip" message="hello">'
    '/home/ubuntu/hackproof-bench/runs/pytest-dev__pytest-10081/B/testing/'
    'test_repro_eba6c0c721f7.py:7: hello'
    '</skipped></testcase></testsuite></testsuites>'
)
FIXED_RED = "def test_repro_issue():\n    assert False, 'boom'\n"
FAILING_RED_JUNIT = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuites><testsuite name="pytest" errors="0" failures="1" tests="1">'
    '<testcase classname="test_repro" name="test_repro_issue">'
    '<failure message="assert False">boom</failure>'
    '</testcase></testsuite></testsuites>'
)


class _ScriptedSpark:
    """Spark stand-in replaying canned red bodies + usage records."""

    def __init__(self, bodies: list[str]) -> None:
        self._bodies = list(bodies)
        self.issues: list[str] = []
        self.usage_log: list[dict] = []

    def generate_regression_test(
        self, issue_description: str, code_context: str
    ) -> str:
        self.issues.append(issue_description)
        self.usage_log.append(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "finish_reason": "stop",
            }
        )
        return self._bodies[
            min(len(self.issues) - 1, len(self._bodies) - 1)
        ]


class _ScriptedDebate:
    """Debate orchestrator recording run() calls (empty patch out)."""

    calls: list = []

    def __init__(self, spark, wonder) -> None:  # noqa: ANN001
        pass

    def run(self, **kwargs):
        from types import SimpleNamespace

        _ScriptedDebate.calls.append(kwargs)
        return SimpleNamespace(
            rounds_used=1, converged=True, convergence_score=1.0,
            total_tokens=7, duration_ms=3, verdicts=[],
            patch_candidate="", token_usage=[],
        )


def _prove_scripted(script: list[tuple[int, str]]):
    """Prove callback judging scripted (exit, junit) pairs for real."""
    calls: list = []

    def _prove(rel: str, data: bytes) -> dict:
        calls.append((rel, data))
        exit_code, junit = script[min(len(calls) - 1, len(script) - 1)]
        verdict = judge_red(exit_code, junit.encode("utf-8"), False)
        return {
            "rel": rel,
            "accepted": bool(verdict["accepted"]),
            "reason": str(verdict["reason"]),
            "exit_code": exit_code,
            "failures": int(verdict["failures"]),
            "errors": int(verdict["errors"]),
        }

    _prove.calls = calls  # type: ignore[attr-defined]
    return _prove


def _b_task() -> dict:
    return {
        "id": "demo__pkg-1",
        "language": "python",
        "repo": "demo/pkg",
        "fail_to_pass": ["tests/test_x.py::TestX::test_fix"],
        "pass_to_pass": [],
        "hidden_golden": [],
    }


def _wire_mode_b(
    monkeypatch, spark: _ScriptedSpark, tmp_path: Path
) -> tuple:
    """Stage a Mode B agent run with scripted LLM/debate/localize."""
    from types import SimpleNamespace

    _ScriptedDebate.calls.clear()
    monkeypatch.setattr(
        run_benchmark, "localize_for_task",
        lambda task, clone, issue, tee: ([], ["a.py"], 1, "test"),
    )
    monkeypatch.setattr(
        "hackproof.cli.build_code_context", lambda *a: ("ctx", "bctx")
    )
    monkeypatch.setattr(
        "hackproof.cli.repo_structure_sketch", lambda *a: "struct"
    )
    wonder = SimpleNamespace(usage_log=[])
    monkeypatch.setattr(
        "hackproof.llm.meta_client.MetaClient",
        lambda model: {"muse-spark-1.3": spark,
                       "muse-wonder-1.0": wonder}[model],
    )
    monkeypatch.setattr(
        "hackproof.debate.orchestrator.DebateOrchestrator", _ScriptedDebate
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    return clone, tmp_path / "prompts"


def test_red_retry_feedback_quotes_judge_reason() -> None:
    reason = "red run exited 0 (test passed on unpatched base)"
    feedback = run_benchmark.red_retry_feedback(
        {"reason": reason, "exit_code": 0, "failures": 0, "errors": 0}
    )
    assert reason in feedback
    assert "exit=0" in feedback
    assert "FAILS" in feedback


def test_mode_b_vacuous_red_rejected_then_rescued(
    tmp_path: Path, monkeypatch
) -> None:
    """10081-B shape: vacuous red rejected, retry accepted, debate runs."""
    assert len(VACUOUS_RED_JUNIT_10081.encode("utf-8")) == 485
    spark = _ScriptedSpark([VACUOUS_RED_10081, FIXED_RED])
    clone, prompt_dir = _wire_mode_b(monkeypatch, spark, tmp_path)
    prove = _prove_scripted(
        [(0, VACUOUS_RED_JUNIT_10081), (1, FAILING_RED_JUNIT)]
    )
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        usage = run_benchmark.UsageCollector()
        agent = run_benchmark.run_mode_b_agent(
            _b_task(), clone,
            {"problem_statement": "bug", "hints_text": "hint"},
            prompt_dir, tee, usage, prove_red=prove,
        )
    finally:
        tee.close()
    # The vacuous red was rejected by the real judge on archived XML...
    assert len(prove.calls) == 2
    assert len(spark.issues) == 2
    assert "red run exited 0 (test passed on unpatched base)" in (
        spark.issues[1]
    )
    # ...the retry was accepted, so the debate ran (empty patch out).
    assert agent["red_record"]["accepted"] is True
    assert len(_ScriptedDebate.calls) == 1
    assert agent["reason"] == (
        "debate produced no complete SEARCH/REPLACE blocks"
    )
    assert [e["role"] for e in usage.per_call] == [
        "SparkRedSynthesis", "SparkRedSynthesis-retry",
    ]
    assert (prompt_dir / "red_test.txt").read_bytes().decode("utf-8") == (
        VACUOUS_RED_10081
    )
    assert (prompt_dir / "red_test_retry.txt").read_bytes().decode(
        "utf-8"
    ) == FIXED_RED


def test_mode_b_double_reject_skips_debate(
    tmp_path: Path, monkeypatch
) -> None:
    """Two vacuous reds: no debate spend, loud red-rejected reason."""
    spark = _ScriptedSpark([VACUOUS_RED_10081, VACUOUS_RED_10081])
    clone, prompt_dir = _wire_mode_b(monkeypatch, spark, tmp_path)
    prove = _prove_scripted([(0, VACUOUS_RED_JUNIT_10081)] * 2)
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        usage = run_benchmark.UsageCollector()
        agent = run_benchmark.run_mode_b_agent(
            _b_task(), clone,
            {"problem_statement": "bug", "hints_text": "hint"},
            prompt_dir, tee, usage, prove_red=prove,
        )
    finally:
        tee.close()
    assert agent["ok"] is False
    assert agent["reason"].startswith("red-rejected after resynthesis")
    assert "exited 0" in agent["reason"]
    assert len(_ScriptedDebate.calls) == 0
    assert agent["debate"]["rounds_used"] == 0
    assert agent["red_record"]["accepted"] is False
    assert len(spark.issues) == 2
    assert [e["role"] for e in usage.per_call] == [
        "SparkRedSynthesis", "SparkRedSynthesis-retry",
    ]


# ---------------------------------------------------------------------------
# Step 4.3b.7 (R12): evidence bundle (collect/freeze/debate prompts) + audit
# ---------------------------------------------------------------------------


def test_collect_archive_writes_full_streams(
    tmp_path: Path, monkeypatch
) -> None:
    stdout = "tests/test_x.py::TestX::test_a\ntests/test_x.py::TestX::test_b\n"
    stderr = "warnings summary ...\n"
    runner = _ScriptedRunner([(0, stdout, stderr)])
    monkeypatch.setattr(
        run_benchmark, "oci_sandbox_runner", lambda cfg, image: runner
    )
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        task = _py_task("psf__requests-6028")
        task["fail_to_pass"] = ["tests/test_x.py::TestX::test_a"]
        ids, _ = run_benchmark.collect_ids_remote_full(
            None, task, "/fake/run", tee, "probe",
            archive=tmp_path / "collect" / "t1",
        )
    finally:
        tee.close()
    assert ids == [
        "tests/test_x.py::TestX::test_a", "tests/test_x.py::TestX::test_b",
    ]
    assert (tmp_path / "collect" / "t1.out").read_text(
        encoding="utf-8"
    ) == stdout
    assert (tmp_path / "collect" / "t1.err").read_text(
        encoding="utf-8"
    ) == stderr


def test_freeze_archive_writes_output_and_failure_note(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _ScriptedRunner([(0, "requests==2.9.0\npytest==7.4.4\n", "")])
    monkeypatch.setattr(
        run_benchmark, "oci_sandbox_runner", lambda cfg, image: runner
    )
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        run_benchmark.archive_venv_freeze_remote(
            None, {"id": "x"}, "/fake/run",
            tmp_path / "versions" / "t1.pip-freeze.txt", tee,
        )
    finally:
        tee.close()
    assert (tmp_path / "versions" / "t1.pip-freeze.txt").read_text(
        encoding="utf-8"
    ) == "requests==2.9.0\npytest==7.4.4\n"
    runner2 = _ScriptedRunner([(1, "", "no pip here")])
    monkeypatch.setattr(
        run_benchmark, "oci_sandbox_runner", lambda cfg, image: runner2
    )
    tee = run_benchmark.Tee(tmp_path / "t2.log")
    try:
        run_benchmark.archive_venv_freeze_remote(
            None, {"id": "x"}, "/fake/run",
            tmp_path / "versions" / "t2.pip-freeze.txt", tee,
        )
    finally:
        tee.close()
    note = (tmp_path / "versions" / "t2.pip-freeze.txt").read_text(
        encoding="utf-8"
    )
    assert "exit=1" in note and "no pip here" in note


class _DelegateClient:
    """Debate client double with FinOps attributes."""

    def __init__(self) -> None:
        self.calls: list = []
        self.model = "muse-spark-1.3"
        self.last_usage = {
            "prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
            "finish_reason": "stop",
        }

    def debate_round(self, role: str, prompt: str, history: list) -> str:
        self.calls.append((role, prompt, list(history)))
        return f"verdict-{role}"

    def synthesize_patch(self, consensus_text: str) -> str:
        self.calls.append(("PatchSynthesis", consensus_text, []))
        return "patch"


def test_debate_recorder_orders_turns_and_delegates() -> None:
    recorder = run_benchmark.DebatePromptRecorder()
    spark, wonder = _DelegateClient(), _DelegateClient()
    wrapped_spark = recorder.wrap(spark)
    wrapped_wonder = recorder.wrap(wonder)
    history: list = []
    assert wrapped_spark.debate_round("EdgeScout", "scout?", history) == (
        "verdict-EdgeScout"
    )
    history.append({"role": "EdgeScout", "text": "v1"})
    assert wrapped_wonder.debate_round("Builder", "build!", history) == (
        "verdict-Builder"
    )
    assert wrapped_wonder.synthesize_patch("consensus") == "patch"
    # Call order across both clients, prompts verbatim, history snapshotted.
    assert [t["role"] for t in recorder.turns] == [
        "EdgeScout", "Builder", "PatchSynthesis",
    ]
    assert recorder.turns[0]["prompt"] == "scout?"
    assert recorder.turns[0]["history"] == []
    assert recorder.turns[1]["history"] == [
        {"role": "EdgeScout", "text": "v1"}
    ]
    history.append({"role": "Builder", "text": "v2"})
    assert recorder.turns[1]["history"] == [
        {"role": "EdgeScout", "text": "v1"}
    ]
    assert recorder.turns[2]["prompt"] == "consensus"
    # Delegation intact: calls forwarded, FinOps attrs pass through.
    assert len(spark.calls) == 1 and len(wonder.calls) == 2
    assert wrapped_spark.model == "muse-spark-1.3"
    assert wrapped_wonder.last_usage["total_tokens"] == 4


def _evidence_out(tmp_path: Path, record: dict, files: dict[str, str]) -> Path:
    out = tmp_path / "results"
    (out / "results" / "B").mkdir(parents=True)
    (out / "results" / "B" / "t1.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    for rel, body in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return out


def _junit_bytes() -> bytes:
    return PYTEST_JUNIT.encode("utf-8")


def _new_b_record() -> dict:
    junit = _junit_bytes()
    return {
        "task": "t1", "mode": "B", "status": "resolved",
        "junit_sha256": hashlib.sha256(junit).hexdigest(),
        "agent": {"debate": {"rounds_used": 2, "converged": True}},
        "evidence": {
            "language": "python", "collect": True, "freeze": True,
            "debate_prompts": True,
        },
    }


def _bundle_files() -> dict[str, str]:
    return {
        "junit_reports/B/t1.xml": _junit_bytes().decode("utf-8"),
        "guard_reports/B/t1.json": "{}",
        "collect_reports/B/t1.out": "tests/test_x.py::T::t\n",
        "collect_reports/B/t1.err": "",
        "versions/B/t1.pip-freeze.txt": "requests==2.9.0\n",
        "prompts/B/t1/debate_prompts.json": '{"turns": []}',
    }


def test_audit_evidence_bundle_present_and_missing(tmp_path: Path) -> None:
    out = _evidence_out(tmp_path, _new_b_record(), _bundle_files())
    audit = audit_results_dir(out)
    assert audit["missing_evidence_reports"] == []
    assert audit["junit_mismatches"] == []
    assert audit["missing_guard_reports"] == []
    # Each missing artifact is detected with its own tag.
    for rel, tag in [
        ("collect_reports/B/t1.out", "B/t1:missing-collect-out"),
        ("collect_reports/B/t1.err", "B/t1:missing-collect-err"),
        ("versions/B/t1.pip-freeze.txt", "B/t1:missing-pip-freeze"),
        ("prompts/B/t1/debate_prompts.json", "B/t1:missing-debate-prompts"),
    ]:
        (out / rel).unlink()
        audit = audit_results_dir(out)
        assert audit["missing_evidence_reports"] == [tag]
        (out / rel).write_text(
            _bundle_files()[rel], encoding="utf-8"
        )
    # Flag-independent expectations: eval ran + debated ⟹ required
    # even when every flag claims False (skipped production caught).
    record = _new_b_record()
    record["evidence"] = {
        "language": "python", "collect": False, "freeze": False,
        "debate_prompts": False,
    }
    (out / "results" / "B" / "t1.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    for rel in [
        "collect_reports/B/t1.out", "collect_reports/B/t1.err",
        "versions/B/t1.pip-freeze.txt", "prompts/B/t1/debate_prompts.json",
    ]:
        (out / rel).unlink()
    audit = audit_results_dir(out)
    assert sorted(audit["missing_evidence_reports"]) == [
        "B/t1:missing-collect-err",
        "B/t1:missing-collect-out",
        "B/t1:missing-debate-prompts",
        "B/t1:missing-pip-freeze",
    ]


def test_audit_evidence_grandfathers_legacy_records(tmp_path: Path) -> None:
    # No evidence block (4.2 shape): nothing required, even with a
    # JUnit and debate rounds present.
    record = _new_b_record()
    del record["evidence"]
    out = _evidence_out(
        tmp_path, record,
        {
            "junit_reports/B/t1.xml": _junit_bytes().decode("utf-8"),
            "guard_reports/B/t1.json": "{}",
        },
    )
    audit = audit_results_dir(out)
    assert audit["missing_evidence_reports"] == []
    assert audit["junit_mismatches"] == []
    assert audit["missing_guard_reports"] == []


def test_mode_b_agent_archives_debate_prompts_and_flags(
    tmp_path: Path, monkeypatch
) -> None:
    """Accepted red → debate runs → prompts file + evidence flag land."""
    spark = _ScriptedSpark([FIXED_RED])
    clone, prompt_dir = _wire_mode_b(monkeypatch, spark, tmp_path)
    prove = _prove_scripted([(1, FAILING_RED_JUNIT)])
    tee = run_benchmark.Tee(tmp_path / "t.log")
    try:
        usage = run_benchmark.UsageCollector()
        agent = run_benchmark.run_mode_b_agent(
            _b_task(), clone,
            {"problem_statement": "bug", "hints_text": "hint"},
            prompt_dir, tee, usage, prove_red=prove,
        )
    finally:
        tee.close()
    assert len(_ScriptedDebate.calls) == 1
    prompts_path = prompt_dir / "debate_prompts.json"
    assert prompts_path.is_file()
    payload = json.loads(prompts_path.read_text(encoding="utf-8"))
    assert payload["turns"] == []  # scripted debate makes no client calls
    assert agent["evidence"] == {"debate_prompts": True}
    assert [e["role"] for e in usage.per_call] == ["SparkRedSynthesis"]
