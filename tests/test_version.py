"""The version is written twice, for packaging and for the running app."""

from __future__ import annotations

import tomllib
from pathlib import Path

import app


def test_the_app_reports_the_packaged_version():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    packaged = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert app.__version__ == packaged
