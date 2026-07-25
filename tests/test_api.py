from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_trader.api import KalshiClient
from kalshi_trader.auth import RequestSigner
from kalshi_trader.domain import OrderIntent, Outcome


@pytest.mark.asyncio
async def test_orderbook_is_normalized_to_yes_and_no_quotes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [["0.4400", "12.00"]],
                        "no_dollars": [["0.5300", "9.00"]],
                    }
                },
            )
        return httpx.Response(200, json={"market": {"status": "open"}})

    client = KalshiClient(
        "https://example.test/trade-api/v2", transport=httpx.MockTransport(handler)
    )
    quote = await client.get_quote("TEST")
    await client.close()
    assert quote.yes_bid == Decimal("0.4400")
    assert quote.yes_ask == Decimal("0.4700")
    assert quote.no_bid == Decimal("0.5300")
    assert quote.no_ask == Decimal("0.5600")
    assert quote.yes_ask_size == Decimal("9.00")


@pytest.mark.asyncio
async def test_no_order_uses_v2_ask_side(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        assert request.url.path.endswith("/portfolio/events/orders")
        assert '"side":"ask"' in payload
        assert '"price":"0.3500"' in payload
        assert request.headers["KALSHI-ACCESS-KEY"] == "key-id"
        return httpx.Response(
            201,
            json={
                "order_id": "order-1",
                "client_order_id": "client-1",
                "fill_count": "0.00",
                "remaining_count": "2.00",
            },
        )

    client = KalshiClient(
        "https://example.test/trade-api/v2",
        signer=RequestSigner("key-id", key_path),
        transport=httpx.MockTransport(handler),
    )
    intent = OrderIntent(
        client_order_id="client-1",
        ticker="TEST",
        outcome=Outcome.NO,
        count=2,
        limit_price=Decimal("0.65"),
        fair_probability=Decimal("0.20"),
        signal_generated_at=datetime.now(UTC),
        model_version="v1",
    )
    result = await client.place_order(intent)
    await client.close()
    assert result.order_id == "order-1"
    assert result.status.value == "resting"
