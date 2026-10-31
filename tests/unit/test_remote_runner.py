"""Step 1.3-addendum: RemoteSSHRunner (OCI gVisor tier) offline contract tests."""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hackproof.sandbox import (
    OciUnavailableError,
    RemoteSSHRunner,
    SandboxExecutionResult,
)


def _make_key(tmp_path: Path) -> str:
    key = tmp_path / "id_test"
    key.write_text("fake-key-material")
    return str(key)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _Recorder:
    """Fake SSH transport capturing argv for offline assertions."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: Any, timeout: float) -> Any:
        self.calls.append((list(argv), timeout))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _runner(tmp_path: Path, fake: Any, **kwargs: Any) -> RemoteSSHRunner:
    return RemoteSSHRunner(
        host="oci.example",
        user="ubuntu",
        key_path=_make_key(tmp_path),
        executor=fake,
        **kwargs,
    )


def test_ssh_argv_construction(tmp_path: Path) -> None:
    fake = _Recorder(_completed(0, stdout="ok\n"))
    runner = _runner(tmp_path, fake)
    result = runner.run_in_sandbox(["echo", "ok"], "/remote/work")
    assert isinstance(result, SandboxExecutionResult)
    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.timed_out is False
    assert result.duration_ms >= 0
    assert len(fake.calls) == 1
    argv, timeout = fake.calls[0]
    assert argv[0] == "ssh"
    assert timeout == 300.0
    assert "-i" in argv and "BatchMode=yes" in argv
    assert "ConnectTimeout=15" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ubuntu@oci.example" in argv
    assert "SendEnv" not in " ".join(argv)
    remote_script = argv[-1]
    assert "docker run" in remote_script
    assert "--runtime=runsc" in remote_script
    assert "--network=none" in remote_script
    assert "-v /remote/work:/remote/work" in remote_script
    assert "-w /remote/work" in remote_script
    assert "echo ok" in remote_script


def test_network_enabled_omits_network_none(tmp_path: Path) -> None:
    fake = _Recorder(_completed(0))
    runner = _runner(tmp_path, fake)
    runner.run_in_sandbox(["pytest"], "/remote/work", network_disabled=False)
    remote_script = fake.calls[0][0][-1]
    assert "--network=none" not in remote_script
    assert "--runtime=runsc" in remote_script


def test_shell_quoting_blocks_injection(tmp_path: Path) -> None:
    fake = _Recorder(_completed(0))
    runner = _runner(tmp_path, fake)
    evil = "a; rm -rf /"
    runner.run_in_sandbox(["echo", evil], "/remote/dir with space")
    remote_script = fake.calls[0][0][-1]
    assert shlex.quote(evil) in remote_script
    assert shlex.quote("/remote/dir with space") in remote_script
    assert "a; rm -rf /" not in remote_script.replace(shlex.quote(evil), "")


def test_missing_env_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OCI_SSH_HOST", raising=False)
    monkeypatch.delenv("OCI_SSH_USER", raising=False)
    monkeypatch.delenv("OCI_SSH_KEY", raising=False)
    with pytest.raises(OciUnavailableError, match="OCI_SSH_HOST"):
        RemoteSSHRunner()
    with pytest.raises(OciUnavailableError, match="OCI_SSH_USER"):
        RemoteSSHRunner(host="h")
    with pytest.raises(OciUnavailableError, match="OCI_SSH_KEY"):
        RemoteSSHRunner(host="h", user="u")


def test_missing_key_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OciUnavailableError, match="does not exist"):
        RemoteSSHRunner(host="h", user="u", key_path=str(tmp_path / "nope"))


def test_explicit_args_win_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCI_SSH_HOST", "env-host")
    monkeypatch.setenv("OCI_SSH_USER", "env-user")
    monkeypatch.setenv("OCI_SSH_KEY", str(tmp_path / "env-key"))
    (tmp_path / "env-key").write_text("x")
    fake = _Recorder(_completed(0))
    runner = RemoteSSHRunner(
        host="arg-host",
        user="arg-user",
        key_path=_make_key(tmp_path),
        executor=fake,
    )
    runner.run_in_sandbox(["true"], "/r")
    assert "arg-user@arg-host" in fake.calls[0][0]


def test_ssh_exit_255_raises_oci_unavailable(tmp_path: Path) -> None:
    fake = _Recorder(_completed(255, stderr="Permission denied (publickey)"))
    runner = _runner(tmp_path, fake)
    with pytest.raises(OciUnavailableError, match="Permission denied"):
        runner.run_in_sandbox(["pytest"], "/remote/work")


def test_remote_test_failure_is_returned_not_raised(tmp_path: Path) -> None:
    fake = _Recorder(_completed(1, stdout="F", stderr="AssertionError: x"))
    runner = _runner(tmp_path, fake)
    result = runner.run_in_sandbox(["pytest"], "/remote/work")
    assert result.exit_code == 1
    assert result.stdout == "F"
    assert "AssertionError" in result.stderr
    assert result.timed_out is False


def test_watchdog_timeout_returns_result(tmp_path: Path) -> None:
    boom = subprocess.TimeoutExpired(cmd="ssh", timeout=300, output="part", stderr="err")
    fake = _Recorder(boom)
    runner = _runner(tmp_path, fake)
    result = runner.run_in_sandbox(["sleep", "999"], "/remote/work", timeout_sec=300)
    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.stdout == "part"
    assert result.stderr == "err"


def test_missing_ssh_binary_maps_to_oci_error(tmp_path: Path) -> None:
    fake = _Recorder(FileNotFoundError("ssh"))
    runner = _runner(tmp_path, fake)
    with pytest.raises(OciUnavailableError, match="ssh binary"):
        runner.run_in_sandbox(["echo"], "/remote/work")


def test_cwd_is_remote_path_without_local_validation(tmp_path: Path) -> None:
    fake = _Recorder(_completed(0))
    runner = _runner(tmp_path, fake)
    result = runner.run_in_sandbox(["pwd"], "/remote/only/does-not-exist-locally")
    assert result.exit_code == 0
    assert "/remote/only/does-not-exist-locally" in fake.calls[0][0][-1]
    assert not Path("/remote/only/does-not-exist-locally").exists()


def test_input_validation(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _Recorder(_completed(0)))
    with pytest.raises(ValueError, match="non-empty list"):
        runner.run_in_sandbox([], "/r")
    with pytest.raises(ValueError, match="non-empty list"):
        runner.run_in_sandbox(["ok", 42], "/r")  # type: ignore[list-item]
    with pytest.raises(ValueError, match="positive"):
        runner.run_in_sandbox(["ok"], "/r", timeout_sec=0)
    with pytest.raises(ValueError, match="non-empty remote path"):
        runner.run_in_sandbox(["ok"], "")


def test_custom_image_and_relaxed_host_check(tmp_path: Path) -> None:
    fake = _Recorder(_completed(0))
    runner = _runner(
        tmp_path, fake, image="custom:1", strict_host_key_checking=False
    )
    runner.run_in_sandbox(["pytest"], "/r")
    argv = fake.calls[0][0]
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "custom:1" in argv[-1]


def test_runtime_override_runc(tmp_path: Path) -> None:
    fake = _Recorder(_completed(0))
    runner = _runner(tmp_path, fake, runtime="runc")
    runner.run_in_sandbox(["pytest"], "/r")
    remote_script = fake.calls[0][0][-1]
    assert "--runtime=runc" in remote_script
    assert "--runtime=runsc" not in remote_script


def test_runtime_rejects_unknown(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown container runtime"):
        _runner(tmp_path, _Recorder(_completed(0)), runtime="weird;rm")
    with pytest.raises(ValueError, match="unknown container runtime"):
        _runner(tmp_path, _Recorder(_completed(0)), runtime="")


@pytest.mark.skipif(
    not os.environ.get("OCI_SSH_HOST"), reason="live OCI SSH not configured"
)
def test_live_oci_smoke() -> None:
    runner = RemoteSSHRunner()
    result = runner.run_in_sandbox(["echo", "ok"], "/tmp", timeout_sec=120)
    assert result.exit_code == 0
    assert "ok" in result.stdout
