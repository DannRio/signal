from __future__ import annotations

import sys

from .machine_cli import main as machine_main

_LAUNCHER_NAMES = {
    "signal-machine",
    "signal-machine.exe",
    "signal-machine-script.py",
    "signal-machine-script.pyw",
}


def _launcher_basename(value: str) -> str:
    """Return a basename for either Windows or POSIX path syntax."""

    normalized = value.strip('"').replace("\\", "/")
    return normalized.rsplit("/", 1)[-1].lower()


def normalize_launcher_args(argv: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Return CLI arguments without duplicated console-launcher paths.

    Standard Python console scripts call an entry point without passing the
    executable path. Some Windows launch/shim combinations can expose that
    path again as the first argument. Argparse would then interpret the path
    as the subcommand. Strip only known signal-machine launcher names and
    preserve all ordinary user arguments.
    """

    values = list(sys.argv[1:] if argv is None else argv)
    while values and _launcher_basename(values[0]) in _LAUNCHER_NAMES:
        values.pop(0)
    return values


def main(argv: list[str] | tuple[str, ...] | None = None) -> int:
    return machine_main(normalize_launcher_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
