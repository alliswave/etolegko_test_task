"""Проверки корневого запуска после разделения модулей."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_launcher_resolves_package_and_prints_help() -> None:
    """Корневой сценарий импортирует одноимённый пакет и показывает справку."""
    project_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "download_speed.py", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert "URL" in completed.stdout
    assert completed.stderr == ""
