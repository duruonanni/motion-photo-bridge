"""Guards against shipping stale instructions to PyPI.

The PyPI project page *is* the README at build time, so a README that still says
"install from git" while the package is on PyPI is a real user-facing bug.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

README = (REPO / "README.md").read_text(encoding="utf-8")
PYPROJECT = (REPO / "pyproject.toml").read_text(encoding="utf-8")

STALE = [
    "PyPI 上架前",           # release note that only made sense pre-0.2.2
    "py3-none-any.whl",      # pins a wheel filename with an old version
    "motion_photo_bridge-0.2.1",
]


def test_version_has_one_source() -> None:
    from motion_photo_bridge import __version__

    assert re.search(r'dynamic = \["version"\]', PYPROJECT), "pyproject must read the version from the package"
    assert not re.search(r'^version = "\d', PYPROJECT, re.M), "do not hardcode a second version in pyproject"
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_readme_installs_from_pypi() -> None:
    assert "pip install motion-photo-bridge" in README, "the quick path must use the PyPI install line"
    for marker in STALE:
        assert marker not in README, f"README still contains stale text: {marker!r}"


def test_readme_has_no_git_install_for_released_versions() -> None:
    git_installs = re.findall(r"pip install [^\n]*git\+https://github\.com/duruonanni[^\n]*", README)
    assert not git_installs, f"README still tells users to install from git: {git_installs}"
