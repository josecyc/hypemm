"""Final synthesis: combine Step 1-3 results into a go/no-go verdict."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hypemm.config import StrategyConfig

logger = logging.getLogger(__name__)


def load_json(path: Path) -> dict[str, object]:
    """Load a JSON file or return empty dict if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)  # type: ignore[no-any-return]


def run_synthesis(reports_dir: Path, config: StrategyConfig) -> str:
    """Combine analysis results and produce overall verdict.

    Returns "GO", "NO-GO", or "CONDITIONAL".
    """
    bt = load_json(reports_dir / "backtest_summary.json")
    corr = load_json(reports_dir / "correlation_analysis.json")
    ob = load_json(reports_dir / "orderbook_analysis.json")

    missing = []
    if not bt:
        missing.append("backtest_summary.json")
    if not corr:
        missing.append("correlation_analysis.json")
    if not ob:
        missing.append("orderbook_analysis.json")

    if missing:
        logger.warning("Missing data files: %s", ", ".join(missing))
        return "INCOMPLETE"

    v1 = str(bt.get("verdict", "UNKNOWN"))
    v2 = str(corr.get("verdict", "UNKNOWN"))
    v3 = str(ob.get("verdict", "UNKNOWN"))

    logger.info("Step 1 (Backtest):    %s", v1)
    logger.info("Step 2 (Correlation): %s", v2)
    logger.info("Step 3 (Orderbook):   %s", v3)

    verdicts = [v1, v2, v3]
    n_pass = sum(1 for v in verdicts if v == "PASS")
    n_fail = sum(1 for v in verdicts if v == "FAIL")

    if n_pass == 3:
        overall = "GO"
    elif n_fail >= 2:
        overall = "NO-GO"
    else:
        overall = "CONDITIONAL"

    logger.info("Overall verdict: %s", overall)
    return overall
