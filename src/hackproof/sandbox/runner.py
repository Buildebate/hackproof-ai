"""Local and remote (OCI gVisor) air-gapped subprocess runners.

Tier 1 runs commands on the local machine with a scrubbed environment.
Tier 2 forwards commands over SSH to the sovereign OCI host where they
execute inside Docker + gVisor ``runsc`` with ``--network=none``.
Both tiers share the :class:`SandboxExecutionResult` contract.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_TERMINATE_GRACE_SEC = 5.0
_SSH_FAILURE_EXIT = 255
_TIMEOUT_EXIT_CODE = 124
_DEFAULT_CONNECT_TIMEOUT_SEC = 15
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
    "RemoteSSHRunner",
    "OciUnavailableError",
    "build_air_gapped_env",
]


class OciUnavailableError(RuntimeError):
    """OCI Tier 2 transport is unreachable or misconfigured.

    Raised for infrastructure failures only (missing credentials, SSH
    connection/auth errors, missing ssh binary). A failing remote *test*
    command is never raised — it is returned as a result, so callers can
    distinguish infra failure from test failure.
    """


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


def _default_ssh_executor(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run argv capturing text output (default RemoteSSHRunner transport)."""
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout
    )


class RemoteSSHRunner:
    """Execute commands on OCI inside Docker + gVisor (``--network=none``).

    No local fallback exists by design: if SSH fails, :class:`OciUnavailableError`
    is raised instead of silently running elsewhere. No local secrets are ever
    forwarded — the SSH argv carries only the key *path*, and no ``SendEnv``
    is configured.
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        key_path: str | Path | None = None,
        image: str = "python:3.11-slim",
        connect_timeout_sec: int = _DEFAULT_CONNECT_TIMEOUT_SEC,
        strict_host_key_checking: bool = True,
        executor: Callable[
            [Sequence[str], float], subprocess.CompletedProcess[str]
        ]
        | None = None,
    ) -> None:
        """Bind OCI credentials (explicit args win over ``OCI_*`` env vars).

        Raises:
            OciUnavailableError: Unset host/user/key or missing key file,
                with an actionable message naming the env var.
        """
        resolved_host = host or os.environ.get("OCI_SSH_HOST")
        resolved_user = user or os.environ.get("OCI_SSH_USER")
        resolved_key = key_path or os.environ.get("OCI_SSH_KEY")
        if not resolved_host:
            raise OciUnavailableError(
                "OCI host is not configured: pass host= or set OCI_SSH_HOST "
                "(Step 1.0 SSH bridge)"
            )
        if not resolved_user:
            raise OciUnavailableError(
                "OCI user is not configured: pass user= or set OCI_SSH_USER"
            )
        if not resolved_key:
            raise OciUnavailableError(
                "OCI SSH key is not configured: pass key_path= or set OCI_SSH_KEY"
            )
        key_file = Path(resolved_key)
        if not key_file.is_file():
            raise OciUnavailableError(
                f"OCI SSH key file does not exist: {key_file} "
                "(check OCI_SSH_KEY / Step 1.0 SSH bridge)"
            )
        self._host = resolved_host
        self._user = resolved_user
        self._key = str(key_file)
        self._image = image
        self._connect_timeout_sec = connect_timeout_sec
        self._strict_host_key_checking = strict_host_key_checking
        self._executor = executor or _default_ssh_executor

    def _build_ssh_argv(self, remote_script: str) -> list[str]:
        """Assemble the non-interactive SSH argv (BatchMode, no SendEnv)."""
        return [
            "ssh",
            "-i",
            self._key,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self._connect_timeout_sec}",
            "-o",
            "StrictHostKeyChecking=yes"
            if self._strict_host_key_checking
            else "StrictHostKeyChecking=accept-new",
            f"{self._user}@{self._host}",
            remote_script,
        ]

    def _build_remote_script(
        self, cmd: Sequence[str], cwd: str, network_disabled: bool
    ) -> str:
        """Wrap cmd in a gVisor ``docker run`` invocation (shell-quoted)."""
        quoted = " ".join(shlex.quote(part) for part in cmd)
        network_flag = "--network=none " if network_disabled else ""
        return (
            f"docker run --rm --runtime=runsc {network_flag}"
            f"-v {shlex.quote(cwd)}:{shlex.quote(cwd)} "
            f"-w {shlex.quote(cwd)} {shlex.quote(self._image)} {quoted}"
        )

    def run_in_sandbox(
        self,
        cmd: list[str],
        cwd: Path | str,
        timeout_sec: int = 300,
        network_disabled: bool = True,
    ) -> SandboxExecutionResult:
        """Run cmd on OCI and return the shared result contract.

        Args:
            cmd: Non-empty argv list (executed remotely, shell-quoted).
            cwd: REMOTE working directory (must exist on OCI; it is never
                validated against the local filesystem).
            timeout_sec: Positive wall-clock budget (client-side watchdog).
            network_disabled: Pass ``--network=none`` to the container.

        Raises:
            ValueError: Empty cmd or non-positive timeout.
            OciUnavailableError: SSH transport failure (exit 255, missing
                ssh binary, timeout of the watchdog is returned, not raised).
        """
        if not cmd or not all(isinstance(part, str) for part in cmd):
            raise ValueError("cmd must be a non-empty list of str")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        remote_cwd = str(cwd)
        if not remote_cwd:
            raise ValueError("cwd must be a non-empty remote path")
        remote_script = self._build_remote_script(cmd, remote_cwd, network_disabled)
        argv = self._build_ssh_argv(remote_script)
        started = time.perf_counter()
        try:
            completed = self._executor(argv, float(timeout_sec))
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return SandboxExecutionResult(
                exit_code=_TIMEOUT_EXIT_CODE,
                stdout=_coerce_output(exc.stdout),
                stderr=_coerce_output(exc.stderr),
                duration_ms=duration_ms,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise OciUnavailableError(
                f"ssh binary is not available locally: {exc}. "
                "Install OpenSSH client on the command terminal."
            ) from exc
        except OSError as exc:
            raise OciUnavailableError(f"SSH transport failed: {exc}") from exc
        duration_ms = int((time.perf_counter() - started) * 1000)
        if completed.returncode == _SSH_FAILURE_EXIT:
            raise OciUnavailableError(
                "SSH to OCI failed (exit 255). "
                f"stderr: {completed.stderr.strip() or '<empty>'} "
                "Check host/user/key and network route to OCI."
            )
        return SandboxExecutionResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=duration_ms,
            timed_out=False,
        )


def _coerce_output(value: object) -> str:
    """Best-effort decode of partial TimeoutExpired output."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
