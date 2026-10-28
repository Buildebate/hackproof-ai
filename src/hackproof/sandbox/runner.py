"""Local air-gapped subprocess runner for untrusted test execution."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_TERMINATE_GRACE_SEC = 5.0
_PROXY_VARS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}
_ENDPOINT_SUFFIXES = ("_ENDPOINT", "_BASE_URL")

__all__ = [
    "SandboxExecutionResult",
    "LocalSubprocessRunner",
    "build_air_gapped_env",
]


def build_air_gapped_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy the environment minus proxy vars and networking endpoints.

    Removes proxy variables (both cases) and any ``*_ENDPOINT`` /
    ``*_BASE_URL`` overrides, then pins ``HF_HUB_OFFLINE=1`` so ML
    tooling cannot silently reach the network. All other variables
    (``PATH``, ``HOME``, ...) pass through untouched.
    """
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k in _PROXY_VARS]:
        del env[key]
    for key in [k for k in env if k.upper().endswith(_ENDPOINT_SUFFIXES)]:
        del env[key]
    env["HF_HUB_OFFLINE"] = "1"
    return env


@dataclass(frozen=True)
class SandboxExecutionResult:
    """Outcome of one sandboxed command."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


class LocalSubprocessRunner:
    """Execute commands in a clean, optionally air-gapped environment."""

    def run_in_sandbox(
        self,
        cmd: list[str],
        cwd: Path | str,
        timeout_sec: int = 60,
        network_disabled: bool = True,
    ) -> SandboxExecutionResult:
        """Run cmd with capture, timeout (SIGTERM then SIGKILL), and env scrub.

        Args:
            cmd: Non-empty argv list.
            cwd: Existing working directory for the child.
            timeout_sec: Positive wall-clock budget in seconds.
            network_disabled: Scrub proxy/endpoint vars when True.

        Raises:
            ValueError: Empty cmd or non-positive timeout.
            FileNotFoundError: Missing cwd.
            NotADirectoryError: cwd is not a directory.
        """
        if not cmd or not all(isinstance(part, str) for part in cmd):
            raise ValueError("cmd must be a non-empty list of str")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        workdir = Path(cwd)
        if not workdir.exists():
            raise FileNotFoundError(f"cwd does not exist: {workdir}")
        if not workdir.is_dir():
            raise NotADirectoryError(f"cwd is not a directory: {workdir}")
        env = build_air_gapped_env() if network_disabled else dict(os.environ)
        started = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=_TERMINATE_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
        duration_ms = int((time.perf_counter() - started) * 1000)
        return SandboxExecutionResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
