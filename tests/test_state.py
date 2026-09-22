from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from kalshi_trader.domain import OrderAction, OrderResult, OrderStatus, Outcome
from kalshi_trader.state import StateError, StateStore


def test_intent_and_result_are_durable(tmp_path, intent) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.record_intent(intent)
    store.record_result(
        OrderResult(
            order_id="order-1",
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            filled_count=Decimal("2"),
            remaining_count=Decimal("0"),
        )
    )
    store.close()
    reopened = StateStore(path)
    assert reopened.order_intents() == [intent]
    assert reopened.status_summary()["orders"] == {"filled": 1}
    reopened.close()


def test_duplicate_client_id_is_rejected(tmp_path, intent) -> None:
    store = StateStore(tmp_path / "state.db")
    store.record_intent(intent)
    with pytest.raises(StateError, match="duplicate client_order_id"):
        store.record_intent(intent)
    store.close()


def test_paper_fill_is_cash_constrained(tmp_path, intent) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize_paper(10)
    with pytest.raises(StateError, match="insufficient cash"):
        store.apply_paper_fill(intent, fill_price=Decimal("0.51"), fee_cents=1)
    assert store.paper_account()[0] == 10
    store.close()


def test_paper_sell_realizes_pnl_and_reduces_position(tmp_path, intent) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize_paper(10_000)
    store.apply_paper_fill(intent, fill_price=Decimal("0.50"), fee_cents=2)
    assert store.paper_position("TEST").count == 2
    assert store.paper_account() == (10_000 - 102, 0)
    sell = replace(intent, client_order_id="client-2", action=OrderAction.SELL)
    store.apply_paper_sell(sell, fill_price=Decimal("0.60"), fee_cents=2)
    assert store.paper_position("TEST").is_flat
    # proceeds 120 - 2 fee = 118; cost basis released 102; realized +16
    assert store.paper_account() == (10_000 - 102 + 118, 16)
    store.close()


def test_paper_sell_cannot_exceed_position(tmp_path, intent) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize_paper(10_000)
    sell = replace(intent, action=OrderAction.SELL, outcome=Outcome.NO)
    with pytest.raises(StateError, match="paper position holds 0"):
        store.apply_paper_sell(sell, fill_price=Decimal("0.60"), fee_cents=0)
    store.close()


def test_action_and_style_round_trip(tmp_path, intent) -> None:
    store = StateStore(tmp_path / "state.db")
    sell = replace(intent, action=OrderAction.SELL)
    store.record_intent(sell)
    assert store.order_intents() == [sell]
    store.close()
