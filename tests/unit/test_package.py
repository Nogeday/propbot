"""Smoke test: the package is installed into the project environment and importable."""

import re

import propbot


def test_version_is_a_release_string() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", propbot.__version__)
