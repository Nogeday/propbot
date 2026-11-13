"""Filesystem anchors shared by the tests."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CONFIG_FIXTURES = FIXTURES / "config"
