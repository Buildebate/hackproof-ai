"""Command-line interface for HackProof AI.

Dual-mode design: uses :mod:`typer` when the optional ``cli`` extra is
installed, otherwise falls back to the standard-library :mod:`argparse`
backend. Both backends are thin wrappers over the shared command
functions (:func:`cmd_verify`, :func:`cmd_test_guard`), so behaviour and
exit codes are identical either way.

Exit codes (canonical, documented in ``--help``):

* ``0`` — verified clean (pipeline green / guard clean);
* ``1`` — pipeline failure (network, auth, synthesis/debate/patch/red-green);
* ``2`` — usage error (bad arguments, bad issue URL, unknown ref, bad repo);
* ``3`` — guard/tamper breach (:class:`RewardHackingDetectedError` details).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional, Sequence

try:
    from hackproof import __version__
except ImportError:  # pragma: no cover - defensive fallback
    __version__ = "0.1.0"

try:
    import typer

    _TYPER_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when typer is missing
    typer = None  # type: ignore[assignment]
    _TYPER_AVAILABLE = False

DESCRIPTION = (
    "Autonomous Zero-Trust Adversarial Engineering Engine for "
    "Meta Global AI Hackathon 2026"
)

INFO_TEXT = (
    f"HackProof AI {__version__}\n"
    f"{DESCRIPTION}\n"
    f"Backend: {'typer' if _TYPER_AVAILABLE else 'argparse (stdlib fallback)'}"
)

EXIT_OK = 0
EXIT_PIPELINE_FAILURE = 1
EXIT_USAGE = 2
EXIT_TAMPER = 3

EXIT_CODES_HELP = (
    "Exit codes: 0 verified-clean, 1 pipeline failure, "
    "2 usage error, 3 guard/tamper breach."
)

RUNNER_CHOICES = ("local", "oci")
LANGUAGE_CHOICES = ("python", "typescript")
ISSUE_URL_HELP = "GitHub issue URL, e.g. https://github.com/<owner>/<repo>/issues/42"

_MAX_LOCALIZE_FILES = 2000
_RETRIEVAL_WIDE_K = 20
_MAX_CHUNK_CHARS = 8000
_MAX_CODE_CONTEXT_CHARS = 12000
_MAX_TOP1_FILE_CHARS = 48000
_MAX_BUILDER_CONTEXT_CHARS = 60000
_GIT_TIMEOUT_SEC = 60
_ISSUE_FETCH_TIMEOUT_SEC = 30

__all__ = [
    "DESCRIPTION",
    "INFO_TEXT",
    "__version__",
    "EXIT_OK",
    "EXIT_PIPELINE_FAILURE",
    "EXIT_USAGE",
    "EXIT_TAMPER",
    "EXIT_CODES_HELP",
    "RUNNER_CHOICES",
    "LANGUAGE_CHOICES",
    "IssueFetchError",
    "app",
    "append_finops_entry",
    "cmd_test_guard",
    "cmd_verify",
    "fetch_issue",
    "main",
    "parse_issue_url",
    "read_tests_baseline_at_ref",
    "resolved_repo",
]


class IssueFetchError(RuntimeError):
    """A GitHub issue could not be fetched (network/auth/rate-limit)."""


def _err(message: str) -> None:
    print(f"hackproof: error: {message}", file=sys.stderr)


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Split a GitHub issue/PR URL into ``(owner, repo, number)``.

    Raises:
        ValueError: The URL is not a ``github.com/<owner>/<repo>/`` +
            ``(issues|pull)/<n>`` address.
    """
    text = url.strip().rstrip("/")
    prefix = "https://github.com/"
    if not text.startswith(prefix):
        raise ValueError(
            f"not a GitHub issue URL: {url!r} "
            f"(expected https://github.com/<owner>/<repo>/issues/<n>)"
        )
    parts = text[len(prefix) :].split("/")
    if len(parts) != 4 or parts[2] not in ("issues", "pull") or not parts[3].isdigit():
        raise ValueError(
            f"not a GitHub issue URL: {url!r} "
            f"(expected https://github.com/<owner>/<repo>/issues/<n>)"
        )
    return parts[0], parts[1], int(parts[3])


def _looks_like_url(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        "://" in lowered
        or lowered.startswith("git@")
        or lowered.endswith(".git")
        or lowered.startswith("github.com/")
    )


def _git(
    argv: list[str], cwd: Path | str, timeout: int = _GIT_TIMEOUT_SEC
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


@contextmanager
def resolved_repo(repo: str) -> Iterator[Path]:
    """Yield a local git checkout for ``repo`` (path or clone URL).

    Local paths are validated as git repositories and returned as-is.
    URLs are shallow-cloned into a temporary directory that is removed
    when the context exits.

    Raises:
        ValueError: Not an existing path, not a git repository, or not
            a recognized URL.
        RuntimeError: ``git`` is missing or the clone failed.
    """
    candidate = Path(repo).expanduser()
    if candidate.exists():
        if not candidate.is_dir():
            raise ValueError(f"repo path is not a directory: {repo}")
        try:
            proc = _git(["rev-parse", "--git-dir"], candidate)
        except FileNotFoundError as exc:
            raise RuntimeError("git binary not found on PATH") from exc
        if proc.returncode != 0:
            raise ValueError(f"repo path is not a git repository: {repo}")
        yield candidate.resolve()
        return
    if not _looks_like_url(repo):
        raise ValueError(f"repo not found and not a clone URL: {repo}")
    tmpdir = tempfile.TemporaryDirectory(prefix="hackproof-repo-")
    try:
        target = Path(tmpdir.name) / "repo"
        try:
            proc = _git(
                ["clone", "--depth", "1", repo, str(target)],
                tmpdir.name,
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("git binary not found on PATH") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"git clone failed for {repo}: {detail}")
        yield target
    finally:
        tmpdir.cleanup()


def read_tests_baseline_at_ref(
    repo_path: Path | str, ref: str, tests_rel: str = "tests"
) -> dict[str, str]:
    """Hash every ``tests/`` file at ``ref``; keys match the guard format.

    Keys are POSIX paths relative to the tests directory, identical to
    :meth:`ImmutableTestGuard.lock_test_baseline`, so the result plugs
    directly into :meth:`ImmutableTestGuard.verify_test_integrity`.

    Raises:
        ValueError: Unknown git ref.
        RuntimeError: ``git`` is missing.
    """
    repo = Path(repo_path)
    try:
        listing = _git(["ls-tree", "-r", "--name-only", ref, "--", tests_rel], repo)
    except FileNotFoundError as exc:
        raise RuntimeError("git binary not found on PATH") from exc
    if listing.returncode != 0:
        detail = listing.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"unknown git ref {ref!r}: {detail}")
    baseline: dict[str, str] = {}
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        name = line.strip()
        if not name:
            continue
        try:
            relative = PurePosixPath(name).relative_to(tests_rel).as_posix()
        except ValueError:
            continue
        shown = _git(["show", f"{ref}:{name}"], repo)
        if shown.returncode != 0:
            continue
        baseline[relative] = hashlib.sha256(shown.stdout).hexdigest()
    return baseline


def read_blob_at_ref(repo_path: Path | str, ref: str, name: str) -> bytes | None:
    """Return raw blob bytes for ``name`` at ``ref`` (None when missing)."""
    shown = _git(["show", f"{ref}:{name}"], Path(repo_path))
    if shown.returncode != 0:
        return None
    return shown.stdout


def cmd_test_guard(
    repo: str = ".",
    baseline: str = "HEAD",
    tests_dir: str = "tests",
) -> int:
    """Lock the ``tests/`` SHA-256 baseline at ``baseline`` and verify it.

    Returns:
        ``EXIT_OK`` when the working tree matches the ref baseline,
        ``EXIT_USAGE`` for bad repo/ref inputs, ``EXIT_TAMPER`` with
        per-file violation details on any breach.
    """
    from hackproof.sandbox.guards import (
        ImmutableTestGuard,
        RewardHackingDetectedError,
    )

    try:
        with resolved_repo(repo) as repo_path:
            tests_root = repo_path / tests_dir
            if not tests_root.is_dir():
                _err(f"tests directory not found: {tests_root}")
                return EXIT_USAGE
            try:
                ref_baseline = read_tests_baseline_at_ref(
                    repo_path, baseline, tests_dir
                )
            except ValueError as exc:
                _err(str(exc))
                return EXIT_USAGE
            try:
                ImmutableTestGuard.verify_test_integrity(tests_root, ref_baseline)
            except FileNotFoundError as exc:
                _err(str(exc))
                return EXIT_USAGE
            except RewardHackingDetectedError as exc:
                for violation in exc.violations:
                    print(f"TAMPER: {violation}")
                assertion_breaches = _assertion_breaches(
                    repo_path, baseline, tests_dir, ref_baseline
                )
                for breach in assertion_breaches:
                    print(f"TAMPER: {breach}")
                _err(
                    "test immutability guard breached: "
                    f"{len(exc.violations)} violation(s) vs {baseline}"
                )
                return EXIT_TAMPER
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except RuntimeError as exc:
        _err(str(exc))
        return EXIT_PIPELINE_FAILURE
    print(f"test-guard: clean — {tests_dir}/ matches {baseline}")
    return EXIT_OK


def _assertion_breaches(
    repo_path: Path,
    ref: str,
    tests_dir: str,
    ref_baseline: dict[str, str],
) -> list[str]:
    """Detect assertion-count reductions among modified test files."""
    from hackproof.sandbox.guards import ImmutableTestGuard

    breaches: list[str] = []
    for relative in sorted(ref_baseline):
        pre = read_blob_at_ref(repo_path, ref, f"{tests_dir}/{relative}")
        if pre is None:
            continue
        post_path = repo_path / tests_dir / relative
        if not post_path.is_file():
            continue
        try:
            post = post_path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(post).hexdigest() == ref_baseline[relative]:
            continue
        pre_text = pre.decode("utf-8", "replace")
        post_text = post.decode("utf-8", "replace")
        if not ImmutableTestGuard.verify_no_assertion_tampering(pre_text, post_text):
            breaches.append(f"assertion-tampering: {relative}")
    return breaches


def fetch_issue(issue_url: str) -> tuple[str, str]:
    """Fetch ``(title, body)`` for a GitHub issue URL via the REST API.

    Uses ``GITHUB_TOKEN`` from the environment when present (higher rate
    limits); the token never appears in messages or tracebacks.

    Raises:
        IssueFetchError: Network failure, missing issue, or rate limit.
    """
    owner, repo, number = parse_issue_url(issue_url)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"hackproof-ai/{__version__}",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer <redacted>"
        request = urllib.request.Request(api_url, headers=headers)
        # Rebuild with the real token without ever logging it.
        request.remove_header("Authorization")
        request.add_header("Authorization", f"Bearer {token}")
    else:
        request = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(
            request, timeout=_ISSUE_FETCH_TIMEOUT_SEC
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise IssueFetchError(f"issue not found: {issue_url}") from exc
        if exc.code in (403, 429):
            raise IssueFetchError(
                "GitHub API rate limit reached; set GITHUB_TOKEN and retry"
            ) from exc
        raise IssueFetchError(
            f"GitHub API request failed for {issue_url}: HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IssueFetchError(
            f"cannot reach GitHub API for {issue_url}: {exc}"
        ) from exc
    title = str(payload.get("title", "") or "")
    body = str(payload.get("body", "") or "")
    if not title and not body:
        raise IssueFetchError(f"issue has no title or body: {issue_url}")
    return title, body


def collect_code_chunks(
    repo_path: Path | str, language: str
) -> list[dict[str, str]]:
    """Collect embeddable code chunks (path + content) for ``language``."""
    suffixes = (".py",) if language == "python" else (".ts", ".tsx")
    skip_dirs = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
    }
    chunks: list[dict[str, str]] = []
    root = Path(repo_path)
    candidates = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and not any(part in skip_dirs for part in path.parts)
    )
    for path in candidates[:_MAX_LOCALIZE_FILES]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        chunks.append(
            {
                "path": path.relative_to(root).as_posix(),
                "content": text[:_MAX_CHUNK_CHARS],
            }
        )
    return chunks


def repo_structure_sketch(repo_path: Path | str, limit: int = 60) -> str:
    """Compact one-line-per-entry repo layout sketch for debate grounding."""
    skip_dirs = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
    entries: list[str] = []
    root = Path(repo_path)
    for path in sorted(root.rglob("*")):
        if any(part in skip_dirs for part in path.relative_to(root).parts):
            continue
        if len(entries) >= limit:
            entries.append("...")
            break
        rel = path.relative_to(root).as_posix()
        entries.append(f"{rel}/" if path.is_dir() else rel)
    return "\n".join(entries)


def build_code_context(
    repo_path: Path | str,
    snippets: list[str],
    files: list[str],
) -> tuple[str, str]:
    """Build the short head context plus the builder-enriched context.

    Step 4.3b.3 (R6): the builder role (the only role writing SEARCH
    text) additionally receives the FULL top-1 localized file so it no
    longer confabulates regions hidden past the 12000-char head
    (A-1142, A-7432). Every other consumer keeps the short head so
    wave token spend stays bounded (FinOps scoping, ~+15k chars on
    ~2 calls per task instead of ~12 calls).

    Returns ``(code_context, builder_code_context)``; when the top-1
    file is missing or unreadable both collapse to the short head.
    """
    code_context = "\n\n".join(snippets)[:_MAX_CODE_CONTEXT_CHARS]
    if not files:
        return code_context, code_context
    try:
        top1_text = Path(repo_path, files[0]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return code_context, code_context
    top1_text = top1_text[:_MAX_TOP1_FILE_CHARS]
    builder_code_context = (
        f"TOP-1 FULL FILE {files[0]}:\n{top1_text}\n\n"
        + "\n\n".join(snippets)
    )[:_MAX_BUILDER_CONTEXT_CHARS]
    return code_context, builder_code_context


def localize_issue(
    repo_path: Path | str, issue_text: str, language: str, top_k: int = 5
) -> tuple[list[str], list[str], int]:
    """Localize relevant code via hybrid dense + keyword retrieval.

    Dense (Contriever Transformer, offline-hash fallback) and keyword
    (stdlib BM25) channels each retrieve a wide net; test/doc chunks are
    downranked per channel; reciprocal-rank fusion merges ranks; the top
    ``top_k`` fused paths win.

    Returns:
        ``(snippets, files, elapsed_ms)`` where snippets are
        ``"path:\\ncontent"`` hits ordered best-first.

    Raises:
        ImportError: The ``retrieval`` extra is not installed.
        RuntimeError: No embeddable code files found.
    """
    started = time.monotonic()
    try:
        from hackproof.retrieval import ContrieverEmbeddingModel, FaissCodeIndexer
    except ImportError as exc:
        raise ImportError(
            "semantic retrieval requires the 'retrieval' extra: "
            "pip install 'hackproof-ai[retrieval]'"
        ) from exc
    from hackproof.retrieval.bm25 import BM25Index
    from hackproof.retrieval.fusion import downrank_test_and_doc, rrf_fuse

    chunks = collect_code_chunks(repo_path, language)
    if not chunks:
        raise RuntimeError(f"no embeddable {language} files found")
    paths = [str(chunk["path"]) for chunk in chunks]
    wide_k = min(_RETRIEVAL_WIDE_K, len(chunks))
    model = ContrieverEmbeddingModel()
    index = FaissCodeIndexer(dim=model.dim)
    index.add_chunks(chunks, model.encode_code_chunks(chunks))
    query = model.encode_queries([issue_text])
    dense_hits = index.search(query[0], top_k=wide_k)
    dense_ranked = downrank_test_and_doc(
        [(str(hit["path"]), float(score)) for hit, score in dense_hits]
    )
    keyword_corpus = [
        f"{path}\n{chunk['content']}" for path, chunk in zip(paths, chunks)
    ]
    keyword_ranked = downrank_test_and_doc(
        [
            (paths[doc_id], score)
            for doc_id, score in BM25Index(keyword_corpus).top(
                issue_text, n=wide_k
            )
        ]
    )
    fused = rrf_fuse(
        [
            [path for path, _ in dense_ranked],
            [path for path, _ in keyword_ranked],
        ]
    )
    by_path = dict(zip(paths, chunks))
    winners = [doc_id for doc_id, _ in fused[:top_k]]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    snippets = [f"{path}:\n{by_path[path]['content']}" for path in winners]
    return snippets, winners, elapsed_ms


def append_finops_entry(
    ledger_path: Path | str,
    issue_url: str,
    rounds_used: int,
    converged: bool,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    per_call: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Append one verify-run entry to the FinOps ledger (creating it).

    Cost is honestly unpriced at append time: ``est_cost_usd`` stays
    ``0.0`` with ``priced: False`` instead of a fabricated dollar figure;
    pricing lands afterwards via ``benchmarks/reprice_ledger.py`` from the
    ledger ``pricing`` rate card (Step 4.3b.9). The ledger file keeps its
    existing budget (default ``150.0``) and ``total_spent_usd`` is the
    rounded sum of priced entries.
    """
    path = Path(ledger_path)
    ledger: dict[str, object]
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            ledger = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            ledger = {}
    else:
        ledger = {}
    budget = ledger.get("budget_usd", 150.0)
    try:
        budget_usd = float(budget)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        budget_usd = 150.0
    entries = ledger.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    entry: dict[str, object] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "issue": issue_url,
        "rounds_used": rounds_used,
        "converged": converged,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "est_cost_usd": 0.0,
        "priced": False,
        "per_call": list(per_call),
    }
    entries.append(entry)
    total_spent = 0.0
    for existing in entries:
        if isinstance(existing, dict) and existing.get("priced"):
            try:
                total_spent += float(existing.get("est_cost_usd", 0.0))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    # Preserve unknown top-level keys (pricing, cost_basis): Wave-1
    # appends must not drop the rate card established in Step 4.2.
    ledger["budget_usd"] = budget_usd
    ledger["total_spent_usd"] = round(total_spent, 2)
    ledger["entries"] = entries
    path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    return entry


IssueFetcher = Callable[[str], tuple[str, str]]
ClientFactory = Callable[[str], object]


def _default_client_factory(model: str) -> object:
    from hackproof.llm.meta_client import MetaClient

    return MetaClient(model)


def cmd_verify(
    issue: str,
    repo: str = ".",
    runner: str = "local",
    language: str = "python",
    ledger: str = "finops_ledger.json",
    issue_fetcher: Optional[IssueFetcher] = None,
    client_factory: Optional[ClientFactory] = None,
    runner_override: Optional[object] = None,
) -> int:
    """Run the full localize → red → debate → patch → green pipeline.

    Args:
        issue: GitHub issue URL (canonical ``--issue`` value).
        repo: Local git path or clone URL (``--repo``).
        runner: ``local`` (air-gapped subprocess) or ``oci`` (gVisor
            over SSH via ``RemoteSSHRunner``).
        language: ``python`` or ``typescript``.
        ledger: FinOps ledger path receiving the token-usage entry.
        issue_fetcher: Injectable issue source (tests); live GitHub
            REST by default.
        client_factory: Injectable model client factory (tests);
            live :class:`MetaClient` per model by default.
        runner_override: Injectable sandbox runner (tests).

    Returns:
        ``EXIT_OK`` on verified red-to-green, ``EXIT_USAGE`` for bad
        inputs, ``EXIT_TAMPER`` on guard breach, ``EXIT_PIPELINE_FAILURE``
        for every other pipeline failure.
    """
    from hackproof.debate.orchestrator import DebateOrchestrator
    from hackproof.debate.roles.architectural_builder import (
        extract_blocks,
        revise_patch,
    )
    from hackproof.patcher.workspace_patcher import (
        PatchApplicationError,
        WorkspacePatcher,
    )
    from hackproof.provenance.tgres_mint import TGRESProvenanceVerifier
    from hackproof.sandbox.guards import (
        ImmutableTestGuard,
        RewardHackingDetectedError,
    )
    from hackproof.sandbox.runner import LocalSubprocessRunner, RemoteSSHRunner
    from hackproof.synthesis.spark_synthesizer import RegressionTestSynthesizer

    try:
        owner, repo_name, number = parse_issue_url(issue)
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    if runner not in RUNNER_CHOICES:
        _err(f"invalid --runner {runner!r} (choose from: local, oci)")
        return EXIT_USAGE
    if language not in LANGUAGE_CHOICES:
        _err(f"invalid --language {language!r} (choose from: python, typescript)")
        return EXIT_USAGE

    if runner_override is not None:
        sandbox_runner = runner_override
    elif runner == "oci":
        from hackproof.sandbox.runner import OciUnavailableError

        try:
            sandbox_runner = RemoteSSHRunner()
        except OciUnavailableError as exc:
            _err(str(exc))
            return EXIT_PIPELINE_FAILURE
    else:
        sandbox_runner = LocalSubprocessRunner()

    fetcher = issue_fetcher or fetch_issue
    factory = client_factory or _default_client_factory

    try:
        with resolved_repo(repo) as repo_path:
            print(f"verify: issue {owner}/{repo_name}#{number} @ {repo_path}")
            try:
                title, body = fetcher(issue)
            except IssueFetchError as exc:
                _err(str(exc))
                return EXIT_PIPELINE_FAILURE
            except ValueError as exc:
                _err(str(exc))
                return EXIT_USAGE
            issue_text = f"{title}\n\n{body}".strip()

            try:
                spark_client = factory("muse-spark-1.3")
                wonder_client = factory("muse-wonder-1.0")
            except ValueError as exc:
                _err(str(exc))
                return EXIT_PIPELINE_FAILURE
            except RuntimeError as exc:
                if "META_API_KEY" in str(exc) or "api key" in str(exc).lower():
                    _err(f"{exc} (set META_API_KEY and retry)")
                else:
                    _err(str(exc))
                return EXIT_PIPELINE_FAILURE

            try:
                snippets, files, localize_ms = localize_issue(
                    repo_path, issue_text, language
                )
            except ImportError as exc:
                _err(str(exc))
                return EXIT_PIPELINE_FAILURE
            except RuntimeError as exc:
                _err(str(exc))
                return EXIT_PIPELINE_FAILURE
            print(
                f"verify: localized {len(files)} file(s) in {localize_ms} ms "
                f"(top: {', '.join(files[:3])})"
            )
            code_context, builder_code_context = build_code_context(
                repo_path, snippets, files
            )
            structure = repo_structure_sketch(repo_path)

            patcher = WorkspacePatcher(runner=sandbox_runner)  # type: ignore[arg-type]
            synthesizer = RegressionTestSynthesizer(
                spark_client,  # type: ignore[arg-type]
                runner=sandbox_runner,  # type: ignore[arg-type]
            )
            try:
                worktree_ctx = patcher.isolated_worktree(repo_path)
            except (RuntimeError, ValueError, PatchApplicationError) as exc:
                _err(str(exc))
                return EXIT_PIPELINE_FAILURE
            with worktree_ctx as worktree:
                tests_root = worktree / "tests"
                try:
                    baseline = ImmutableTestGuard.lock_test_baseline(tests_root)
                except (FileNotFoundError, NotADirectoryError):
                    baseline = {}
                    print("verify: warning: no tests/ in worktree; guard degraded")
                print(f"verify: guard locked ({len(baseline)} file(s))")

                # 4. red baseline: synthesize the repro and prove it fails.
                tests_out = worktree / "tests"
                tests_out.mkdir(parents=True, exist_ok=True)
                try:
                    repro_path = synthesizer.synthesize_test(
                        issue_description=issue_text,
                        code_snippets=snippets,
                        repo_structure=structure,
                        language=language,
                        output_dir=tests_out,
                    )
                except ValueError as exc:
                    _err(str(exc))
                    return EXIT_PIPELINE_FAILURE
                try:
                    red = synthesizer.verify_red_baseline(
                        repro_path, worktree, language
                    )
                except ValueError as exc:
                    _err(str(exc))
                    return EXIT_PIPELINE_FAILURE
                if not red.accepted:
                    _err(f"red baseline rejected: {red.reason}")
                    return EXIT_PIPELINE_FAILURE
                print(
                    f"verify: red baseline accepted "
                    f"(failures={red.failures}, errors={red.errors})"
                )

                # 5. asymmetric debate.
                orchestrator = DebateOrchestrator(
                    spark_client,  # type: ignore[arg-type]
                    wonder_client,  # type: ignore[arg-type]
                )
                report = orchestrator.run(
                    issue_description=issue_text,
                    code_context=code_context,
                    repo_structure=structure,
                    builder_code_context=builder_code_context,
                )
                print(
                    f"verify: debate converged={report.converged} "
                    f"rounds={report.rounds_used} "
                    f"score={report.convergence_score:.2f} "
                    f"tokens={report.total_tokens} "
                    f"duration={report.duration_ms} ms"
                )

                # 6. deterministic patch: first localized file matching
                # every block exactly once wins (ladder exact/ws/fuzzy).
                # Step 4.3b.3 (R6): on total failure, exactly ONE builder
                # revision against the patcher ground truth (region /
                # did-you-mean), then one re-attempt.
                patch_text = report.patch_candidate
                blocks = extract_blocks(patch_text)
                if not blocks:
                    _err(
                        "debate produced no complete SEARCH/REPLACE blocks; "
                        "refusing to guess a patch"
                    )
                    return EXIT_PIPELINE_FAILURE
                applied_rel: Optional[str] = None
                patch_errors: list[str] = []
                revise_usage: dict[str, int] | None = None
                for attempt in (0, 1):
                    patch_errors = []
                    for candidate in files:
                        try:
                            patcher.apply_blocks(worktree, candidate, blocks)
                        except (FileNotFoundError, PatchApplicationError) as exc:
                            patch_errors.append(f"{candidate}: {exc}")
                            continue
                        applied_rel = candidate
                        break
                    if applied_rel is not None or attempt == 1:
                        break
                    failure_report = "; ".join(patch_errors)[:4000]
                    print(
                        "verify: patch missed; single builder revision "
                        "against patcher ground truth ..."
                    )
                    patch_text = revise_patch(
                        wonder_client,  # type: ignore[arg-type]
                        issue_description=issue_text,
                        code_context=builder_code_context,
                        previous_patch=patch_text,
                        failure_report=failure_report,
                    )
                    seen = getattr(wonder_client, "last_usage", None)
                    if isinstance(seen, dict):
                        revise_usage = {
                            key: int(seen.get(key, 0))
                            for key in (
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                            )
                        }
                    blocks = extract_blocks(patch_text)
                    if not blocks:
                        _err(
                            "revision produced no complete SEARCH/REPLACE "
                            "blocks; refusing to guess a patch"
                        )
                        return EXIT_PIPELINE_FAILURE
                if applied_rel is None:
                    _err(
                        "patch matches no localized file exactly once: "
                        + "; ".join(patch_errors[:3])
                    )
                    return EXIT_PIPELINE_FAILURE
                print(
                    f"verify: patch applied ({len(blocks)} block(s) -> "
                    f"{applied_rel})"
                )

                # 7. green verification + guard re-check + machine proof.
                try:
                    green = patcher.verify_post_patch(worktree, language)
                except ValueError as exc:
                    _err(str(exc))
                    return EXIT_PIPELINE_FAILURE
                if not green.passed:
                    _err(
                        f"post-patch suite not green "
                        f"(exit={green.exit_code}, "
                        f"timed_out={green.timed_out})"
                    )
                    return EXIT_PIPELINE_FAILURE
                print(f"verify: green verified (exit={green.exit_code})")
                try:
                    guard_report = ImmutableTestGuard.verify_test_integrity(
                        tests_root, baseline
                    )
                except RewardHackingDetectedError as exc:
                    for violation in exc.violations:
                        print(f"TAMPER: {violation}")
                    _err(
                        "test immutability guard breached after patch: "
                        f"{len(exc.violations)} violation(s)"
                    )
                    return EXIT_TAMPER
                receipt = TGRESProvenanceVerifier.mint_receipt(
                    base_commit=_worktree_head(worktree),
                    test_hash=_sha256_path(repro_path),
                    patch_hash=hashlib.sha256(
                        report.patch_candidate.encode("utf-8")
                    ).hexdigest(),
                    red_junit_hash=_sha256_path(Path(red.junit_path)),
                    green_junit_hash=_sha256_path(Path(green.junit_path)),
                )
                machine_proof = TGRESProvenanceVerifier.verify_red_to_green(
                    Path(red.junit_path), green.exit_code, guard_report
                )
                if not machine_proof:
                    _err("red-to-green machine proof failed")
                    return EXIT_PIPELINE_FAILURE
                print(f"verify: TGRES receipt {receipt['receipt_sha256']}")

                ledger_per_call: list[dict[str, object]] = [
                    {
                        "role": usage.role,
                        "model": usage.model,
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    }
                    for usage in report.token_usage
                ]
                ledger_prompt = report.total_prompt_tokens
                ledger_completion = report.total_completion_tokens
                ledger_total = report.total_tokens
                if revise_usage is not None:
                    ledger_prompt += revise_usage["prompt_tokens"]
                    ledger_completion += revise_usage["completion_tokens"]
                    ledger_total += revise_usage["total_tokens"]
                    ledger_per_call.append(
                        {
                            "role": "ArchitecturalBuilder-retry",
                            "model": "muse-wonder-1.0",
                            "prompt_tokens": revise_usage["prompt_tokens"],
                            "completion_tokens": revise_usage[
                                "completion_tokens"
                            ],
                            "total_tokens": revise_usage["total_tokens"],
                        }
                    )
                append_finops_entry(
                    ledger,
                    issue,
                    rounds_used=report.rounds_used,
                    converged=report.converged,
                    prompt_tokens=ledger_prompt,
                    completion_tokens=ledger_completion,
                    total_tokens=ledger_total,
                    per_call=ledger_per_call,
                )
                print(f"verify: verified clean — {owner}/{repo_name}#{number}")
                return EXIT_OK
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except RuntimeError as exc:
        _err(str(exc))
        return EXIT_PIPELINE_FAILURE


def _worktree_head(worktree: Path) -> str:
    proc = _git(["rev-parse", "HEAD"], worktree)
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.decode("utf-8", "replace").strip()


def _sha256_path(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing:" + path.name


if _TYPER_AVAILABLE:
    assert typer is not None
    app = typer.Typer(
        help=f"{DESCRIPTION}\n\n{EXIT_CODES_HELP}",
        add_completion=False,
        no_args_is_help=True,
    )

    def _version_callback(value: bool) -> None:
        if value:
            typer.echo(f"hackproof {__version__}")
            raise typer.Exit()

    @app.callback()
    def _typer_main(
        version: Optional[bool] = typer.Option(
            None,
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ) -> None:
        """HackProof AI command-line interface."""

    @app.command()
    def info() -> None:
        """Show HackProof AI build information."""
        typer.echo(INFO_TEXT)

    @app.command()
    def verify(
        issue: str = typer.Option(..., "--issue", help=ISSUE_URL_HELP),
        repo: str = typer.Option(".", "--repo", help="Local git path or clone URL."),
        runner: str = typer.Option(
            "local", "--runner", help="Sandbox tier: 'local' or 'oci'."
        ),
        language: str = typer.Option(
            "python", "--language", help="Repro/suite language: python|typescript."
        ),
        ledger: str = typer.Option(
            "finops_ledger.json", "--ledger", help="FinOps ledger path."
        ),
    ) -> None:
        """Run the full red-to-green verify pipeline for a GitHub issue."""
        raise typer.Exit(
            cmd_verify(
                issue=issue,
                repo=repo,
                runner=runner,
                language=language,
                ledger=ledger,
            )
        )

    @app.command(name="test-guard")
    def test_guard(
        baseline: str = typer.Option(
            ..., "--baseline", help="Git ref holding the locked tests/ baseline."
        ),
        repo: str = typer.Option(".", "--repo", help="Local git path or clone URL."),
        tests_dir: str = typer.Option(
            "tests", "--tests-dir", help="Tests directory relative to repo root."
        ),
    ) -> None:
        """Verify working-tree tests/ against the baseline ref."""
        raise typer.Exit(
            cmd_test_guard(repo=repo, baseline=baseline, tests_dir=tests_dir)
        )
else:
    app = None


def _argparse_main(argv: Optional[Sequence[str]] = None) -> int:
    """Argparse backend used when :mod:`typer` is not installed."""
    parser = argparse.ArgumentParser(
        prog="hackproof",
        description=f"{DESCRIPTION}\n\n{EXIT_CODES_HELP}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"hackproof {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.add_parser("info", help="Show HackProof AI build information.")
    verify_parser = subparsers.add_parser(
        "verify", help="Run the full red-to-green verify pipeline."
    )
    verify_parser.add_argument("--issue", required=True, help=ISSUE_URL_HELP)
    verify_parser.add_argument(
        "--repo", default=".", help="Local git path or clone URL."
    )
    verify_parser.add_argument(
        "--runner",
        default="local",
        choices=list(RUNNER_CHOICES),
        help="Sandbox tier: 'local' or 'oci'.",
    )
    verify_parser.add_argument(
        "--language",
        default="python",
        choices=list(LANGUAGE_CHOICES),
        help="Repro/suite language.",
    )
    verify_parser.add_argument(
        "--ledger", default="finops_ledger.json", help="FinOps ledger path."
    )
    guard_parser = subparsers.add_parser(
        "test-guard", help="Verify working-tree tests/ against a baseline ref."
    )
    guard_parser.add_argument(
        "--baseline",
        required=True,
        help="Git ref holding the locked tests/ baseline.",
    )
    guard_parser.add_argument(
        "--repo", default=".", help="Local git path or clone URL."
    )
    guard_parser.add_argument(
        "--tests-dir", default="tests", help="Tests directory relative to repo root."
    )
    args = parser.parse_args(argv)
    if args.command == "info":
        print(INFO_TEXT)
    elif args.command == "verify":
        return cmd_verify(
            issue=args.issue,
            repo=args.repo,
            runner=args.runner,
            language=args.language,
            ledger=args.ledger,
        )
    elif args.command == "test-guard":
        return cmd_test_guard(
            repo=args.repo, baseline=args.baseline, tests_dir=args.tests_dir
        )
    else:
        parser.print_help()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Console-script entry point: ``hackproof = "hackproof.cli:main"``."""
    if _TYPER_AVAILABLE:
        assert typer is not None and app is not None
        if argv is not None:
            sys.argv = ["hackproof", *argv]
        app()
    else:
        raise SystemExit(_argparse_main(argv))


if __name__ == "__main__":  # pragma: no cover
    main()
