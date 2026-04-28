"""Repo structure invariants.

These tests catch the common ways the layout decays: configs sneaking into the
wrong place, doc references going stale after a rename, or configs that no
longer load. Keep this file small — only structural invariants belong here,
not style or content checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hypemm.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = REPO_ROOT / "configs"
CURRENT_STATE = REPO_ROOT / "docs" / "CURRENT_STATE.md"


def _all_configs() -> list[Path]:
    return sorted(CONFIGS_DIR.glob("**/*.toml"))


@pytest.mark.parametrize(
    "config_path", _all_configs(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_every_config_loads(config_path: Path) -> None:
    load_config(config_path)


def test_no_files_at_configs_root() -> None:
    stragglers = [p.name for p in CONFIGS_DIR.iterdir() if p.is_file()]
    assert not stragglers, (
        "configs/ root must contain only mode subdirs (backtest/paper/testnet/live); "
        f"found loose files: {stragglers}"
    )


def test_current_state_references_real_configs() -> None:
    text = CURRENT_STATE.read_text()
    referenced = re.findall(r"configs/(?:backtest|paper|testnet|live)/[\w./-]+?\.toml", text)
    missing = sorted({r for r in referenced if not (REPO_ROOT / r).exists()})
    assert not missing, (
        f"docs/CURRENT_STATE.md references configs that no longer exist: {missing}. "
        "Update the doc when you rename or remove a deployed config."
    )
