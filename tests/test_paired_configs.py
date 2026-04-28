"""Live and paper configs sharing a stem must run the same strategy.

A live instance always runs alongside a paper twin at the same notional + risk
so divergence flags real live-vs-simulated discrepancies, not config drift.
This test fails CI if the two get out of sync.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from hypemm.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "configs" / "live"
PAPER_DIR = REPO_ROOT / "configs" / "paper"


def _paired_stems() -> list[str]:
    return sorted(cfg.stem for cfg in LIVE_DIR.glob("*.toml") if (PAPER_DIR / cfg.name).exists())


@pytest.mark.parametrize("stem", _paired_stems())
def test_live_and_paper_share_strategy(stem: str) -> None:
    live = load_config(LIVE_DIR / f"{stem}.toml")
    paper = load_config(PAPER_DIR / f"{stem}.toml")

    assert asdict(live.strategy) == asdict(
        paper.strategy
    ), f"strategy params drifted between live/{stem}.toml and paper/{stem}.toml"
    assert asdict(live.risk) == asdict(
        paper.risk
    ), f"risk thresholds drifted between live/{stem}.toml and paper/{stem}.toml"


def test_every_live_config_has_a_paper_twin() -> None:
    live_stems = {cfg.stem for cfg in LIVE_DIR.glob("*.toml")}
    paper_stems = {cfg.stem for cfg in PAPER_DIR.glob("*.toml")}
    missing = live_stems - paper_stems
    assert not missing, (
        f"live configs without paper twins: {sorted(missing)}. "
        "Every live instance must run alongside a paper twin with the same stem."
    )
