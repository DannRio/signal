from __future__ import annotations

from signal_bot.launcher import normalize_launcher_args


def test_normalize_launcher_args_preserves_standard_cli_arguments() -> None:
    assert normalize_launcher_args(["dashboard", "--port", "8081"]) == [
        "dashboard",
        "--port",
        "8081",
    ]


def test_normalize_launcher_args_removes_windows_executable_path() -> None:
    assert normalize_launcher_args(
        [
            r"C:\Trading\Signal\signal\.venv\Scripts\signal-machine.exe",
            "dashboard",
        ]
    ) == ["dashboard"]


def test_normalize_launcher_args_removes_repeated_launcher_tokens() -> None:
    assert normalize_launcher_args(
        [
            "signal-machine",
            r"C:\Trading\Signal\signal\.venv\Scripts\signal-machine.exe",
            "scan",
            "--auto-universe",
        ]
    ) == ["scan", "--auto-universe"]


def test_normalize_launcher_args_does_not_remove_other_executables() -> None:
    assert normalize_launcher_args(["python.exe", "dashboard"]) == [
        "python.exe",
        "dashboard",
    ]
