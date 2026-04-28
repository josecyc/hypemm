"""Tests for config loading and run_dir derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hypemm.config import derive_run_dir, load_config


def _minimal_toml() -> str:
    return (
        "[strategy]\n"
        'pairs = [{coin_a = "LINK", coin_b = "SOL"}]\n'
        "\n"
        "[infra]\n"
        'market_dir = "data/market/binance_futures/2y"\n'
    )


class TestDeriveRunDir:
    def test_backtest_path(self) -> None:
        p = Path("/repo/configs/backtest/optimized_4pair_6y.toml")
        assert derive_run_dir(p) == Path("data/runs/backtest/optimized_4pair_6y")

    def test_paper_path(self) -> None:
        p = Path("/repo/configs/paper/optimized_4pair.toml")
        assert derive_run_dir(p) == Path("data/runs/paper/optimized_4pair")

    def test_live_path(self) -> None:
        p = Path("/repo/configs/live/min_size_4pair.toml")
        assert derive_run_dir(p) == Path("data/runs/live/min_size_4pair")

    def test_rejects_path_not_under_configs(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not under a 'configs/' directory"):
            derive_run_dir(tmp_path / "mode" / "name.toml")

    def test_rejects_extra_nesting(self, tmp_path: Path) -> None:
        bad = tmp_path / "configs" / "mode" / "deeper" / "name.toml"
        bad.parent.mkdir(parents=True)
        bad.write_text("")
        with pytest.raises(ValueError, match="must be configs/<mode>/<name>.toml"):
            derive_run_dir(bad)

    def test_rejects_top_level_config(self, tmp_path: Path) -> None:
        bad = tmp_path / "configs" / "name.toml"
        bad.parent.mkdir(parents=True)
        bad.write_text("")
        with pytest.raises(ValueError, match="must be configs/<mode>/<name>.toml"):
            derive_run_dir(bad)


class TestLoadConfig:
    def test_run_dir_is_injected_from_path(self, tmp_path: Path) -> None:
        cfg = tmp_path / "configs" / "paper" / "test_strategy.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(_minimal_toml())

        app = load_config(cfg)
        assert app.infra.run_dir == Path("data/runs/paper/test_strategy")
        assert app.infra.market_dir == Path("data/market/binance_futures/2y")

    def test_data_dir_in_toml_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "configs" / "paper" / "x.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            "[strategy]\n"
            'pairs = [{coin_a = "LINK", coin_b = "SOL"}]\n'
            "[infra]\n"
            'data_dir = "data"\n'
        )
        with pytest.raises(ValueError, match="'data_dir' is no longer a config field"):
            load_config(cfg)

    def test_run_dir_in_toml_raises(self, tmp_path: Path) -> None:
        cfg = tmp_path / "configs" / "paper" / "x.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            "[strategy]\n"
            'pairs = [{coin_a = "LINK", coin_b = "SOL"}]\n'
            "[infra]\n"
            'run_dir = "data/foo"\n'
        )
        with pytest.raises(ValueError, match="'run_dir' must not be set in TOML"):
            load_config(cfg)

    def test_derived_paths_from_runtime_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "configs" / "live" / "min_size_4pair.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(_minimal_toml())

        app = load_config(cfg)
        assert app.infra.candles_dir == Path("data/market/binance_futures/2y/candles")
        assert app.infra.funding_dir == Path("data/market/binance_futures/2y/funding")
        assert app.infra.reports_dir == Path("data/runs/live/min_size_4pair")
        assert app.infra.paper_trades_dir == Path("data/runs/live/min_size_4pair")
