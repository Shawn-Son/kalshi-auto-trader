from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_trader.config import ConfigError, load_config


def test_paper_config_loads() -> None:
    config = load_config(Path("config/paper.toml"))
    assert config.runtime.environment == "paper"
    assert config.risk.max_order_notional_cents <= config.risk.max_market_exposure_cents


def test_live_requires_all_interlocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = Path("config/live.toml").read_text()
    path = tmp_path / "live.toml"
    path.write_text(source.replace("allow_live = false", "allow_live = true"))
    config = load_config(path)
    monkeypatch.delenv("KALSHI_LIVE_CONFIRM", raising=False)
    with pytest.raises(ConfigError, match="live mode is locked"):
        config.assert_live_unlocked(True)
    monkeypatch.setenv("KALSHI_LIVE_CONFIRM", "I_ACCEPT_REAL_MONEY_RISK")
    config.assert_live_unlocked(True)


def test_secrets_are_not_read_from_toml() -> None:
    assert "KALSHI_API_KEY_ID" not in Path("config/live.toml").read_text()
    assert "PRIVATE_KEY" not in Path("config/live.toml").read_text()
