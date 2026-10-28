"""Command-line interface for HackProof AI.

Dual-mode design: uses :mod:`typer` when the optional ``cli`` extra is
installed, otherwise falls back to the standard-library :mod:`argparse`
backend, so ``hackproof --version`` and ``hackproof info`` always work.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

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

__all__ = ["DESCRIPTION", "INFO_TEXT", "__version__", "app", "main"]


if _TYPER_AVAILABLE:
    assert typer is not None
    app = typer.Typer(
        help=DESCRIPTION,
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
else:
    app = None


def _argparse_main(argv: Optional[Sequence[str]] = None) -> int:
    """Argparse backend used when :mod:`typer` is not installed."""
    parser = argparse.ArgumentParser(prog="hackproof", description=DESCRIPTION)
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"hackproof {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.add_parser("info", help="Show HackProof AI build information.")
    args = parser.parse_args(argv)
    if args.command == "info":
        print(INFO_TEXT)
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
