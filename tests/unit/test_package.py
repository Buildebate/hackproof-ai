"""Scaffolding checks: package import, version, and basic CLI invocation."""

import sys

import pytest

import hackproof
from hackproof import cli


def test_package_imports() -> None:
    assert hackproof.__name__ == "hackproof"


def test_version() -> None:
    assert hackproof.__version__ == "0.2.0rc1"
    assert cli.__version__ == "0.2.0rc1"


def test_cli_version_via_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hackproof", "--version"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0
    assert "0.2.0rc1" in capsys.readouterr().out


def test_cli_info_via_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["hackproof", "info"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0
    assert "HackProof" in capsys.readouterr().out


def test_argparse_fallback_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._argparse_main(["--version"])
    assert exc_info.value.code == 0
    assert "hackproof 0.2.0rc1" in capsys.readouterr().out


def test_argparse_fallback_info(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._argparse_main(["info"]) == 0
    assert "HackProof" in capsys.readouterr().out


def test_argparse_fallback_bare_invocation_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._argparse_main([]) == 0
    assert "usage:" in capsys.readouterr().out


def test_typer_backend_when_available() -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    assert cli.app is not None
    runner = CliRunner()
    version_result = runner.invoke(cli.app, ["--version"])
    assert version_result.exit_code == 0
    assert "0.2.0rc1" in version_result.output
    info_result = runner.invoke(cli.app, ["info"])
    assert info_result.exit_code == 0
    assert "HackProof" in info_result.output
