from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_trader.domain import OrderResult, OrderStatus
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
