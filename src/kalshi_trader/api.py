from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from kalshi_trader.auth import RequestSigner
from kalshi_trader.domain import (
    ONE,
    MarketQuote,
    OrderIntent,
    OrderResult,
    OrderStatus,
    OrderStyle,
    Outcome,
    PortfolioSnapshot,
    Position,
    dollars_to_cents_up,
    format_dollars,
    parse_utc,
)
from kalshi_trader.state import StateStore


class KalshiAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        *,
        signer: RequestSigner | None = None,
        timeout_seconds: float = 5.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
            headers={"User-Agent": "kalshi-auto-trader/0.1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool,
        params: dict[str, str | int | float | bool | None] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if authenticated and self.signer is None:
            raise KalshiAPIError("authenticated request attempted without credentials")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            headers = self.signer.headers(method, url) if authenticated and self.signer else {}
            try:
                response = await self._client.request(
                    method, url, headers=headers, params=params, json=json
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
            else:
                if response.status_code < 400:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise KalshiAPIError("Kalshi returned invalid JSON") from exc
                    if not isinstance(payload, dict):
                        raise KalshiAPIError("Kalshi returned an unexpected response shape")
                    return payload
                message = _error_message(response)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    raise KalshiAPIError(message, status_code=response.status_code)
                last_error = KalshiAPIError(message, status_code=response.status_code)
                if attempt == self.max_retries:
                    break
            delay = min(2.0, 0.2 * (2**attempt)) + random.uniform(0, 0.1)
            await asyncio.sleep(delay)
        raise KalshiAPIError(f"request failed after retries: {last_error}") from last_error

    async def get_market(self, ticker: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/markets/{ticker}", authenticated=False)
        market = payload.get("market")
        if not isinstance(market, dict):
            raise KalshiAPIError("market response is malformed")
        return market

    async def get_orderbook(self, ticker: str, *, depth: int = 1) -> OrderBook:
        payload = await self._request(
            "GET", f"/markets/{ticker}/orderbook", authenticated=False, params={"depth": depth}
        )
        orderbook = payload.get("orderbook_fp")
        if not isinstance(orderbook, dict):
            raise KalshiAPIError("orderbook response is malformed")
        return OrderBook(
            yes_bids=_levels(orderbook.get("yes_dollars")),
            no_bids=_levels(orderbook.get("no_dollars")),
        )

    async def list_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = "open",
        max_markets: int = 1000,
    ) -> list[dict[str, Any]]:
        """Page through /markets. `status` follows the API: open, closed, settled, or None."""
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(markets) < max_markets:
            params: dict[str, str | int | float | bool | None] = {
                "limit": min(200, max_markets - len(markets)),
                "series_ticker": series_ticker,
                "event_ticker": event_ticker,
                "status": status,
                "cursor": cursor,
            }
            payload = await self._request(
                "GET",
                "/markets",
                authenticated=False,
                params={k: v for k, v in params.items() if v is not None},
            )
            page = payload.get("markets", [])
            if not isinstance(page, list):
                raise KalshiAPIError("markets response is malformed")
            markets.extend(item for item in page if isinstance(item, dict))
            cursor = payload.get("cursor") or None
            if not cursor or not page:
                break
        return markets[:max_markets]

    async def event_is_mutually_exclusive(self, event_ticker: str) -> bool:
        try:
            payload = await self._request("GET", f"/events/{event_ticker}", authenticated=False)
        except KalshiAPIError:
            return False
        event = payload.get("event")
        return isinstance(event, dict) and bool(event.get("mutually_exclusive"))

    async def get_quote(self, ticker: str) -> MarketQuote:
        market, book = await asyncio.gather(
            self.get_market(ticker), self.get_orderbook(ticker, depth=1)
        )
        return quote_from_book(ticker, market, book)

    async def place_order(self, intent: OrderIntent) -> OrderResult:
        book_side, price = intent.api_book_side_and_price()
        payload: dict[str, object] = {
            "ticker": intent.ticker,
            "client_order_id": intent.client_order_id,
            "side": book_side,
            "count": f"{intent.count}.00",
            "price": format_dollars(price),
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            # A maker order must never cross; the exchange rejects it instead of filling.
            "post_only": intent.style is OrderStyle.MAKER,
            "cancel_order_on_pause": True,
            # A sell only ever reduces the position we hold on that side.
            "reduce_only": not intent.is_buy,
            "subaccount": 0,
            "exchange_index": 0,
        }
        try:
            raw = await self._request(
                "POST", "/portfolio/events/orders", authenticated=True, json=payload
            )
        except KalshiAPIError as exc:
            if exc.status_code != 409:
                raise
            reconciled = await self.find_order(intent.ticker, intent.client_order_id)
            if reconciled is None:
                raise KalshiAPIError(
                    "duplicate client order ID reported but order could not be reconciled",
                    status_code=409,
                ) from exc
            return reconciled
        return _v2_order_result(raw, intent.client_order_id)

    async def find_order(self, ticker: str, client_order_id: str) -> OrderResult | None:
        for status in ("resting", "executed", "canceled"):
            raw = await self._request(
                "GET",
                "/portfolio/orders",
                authenticated=True,
                params={"ticker": ticker, "status": status, "limit": 1000},
            )
            orders = raw.get("orders", [])
            if isinstance(orders, list):
                for order in orders:
                    if isinstance(order, dict) and order.get("client_order_id") == client_order_id:
                        return _legacy_order_result(order)
        return None

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"/portfolio/events/orders/{order_id}", authenticated=True)

    async def position(self, ticker: str) -> Position:
        raw = await self._request(
            "GET",
            "/portfolio/positions",
            authenticated=True,
            params={"ticker": ticker, "limit": 100, "count_filter": "position"},
        )
        positions = raw.get("market_positions", [])
        if not isinstance(positions, list):
            raise KalshiAPIError("positions response is malformed")
        for entry in positions:
            if not isinstance(entry, dict) or entry.get("ticker") != ticker:
                continue
            # position_fp is signed: positive means long YES, negative means long NO.
            signed = Decimal(str(entry.get("position_fp", entry.get("position", "0"))))
            count = int(abs(signed))
            if count == 0:
                return Position.flat(ticker)
            cost = dollars_to_cents_up(abs(Decimal(str(entry.get("market_exposure_dollars", "0")))))
            outcome = Outcome.YES if signed > 0 else Outcome.NO
            return Position(ticker=ticker, outcome=outcome, count=count, cost_cents=cost)
        return Position.flat(ticker)

    async def portfolio_snapshot(self, ticker: str, state: StateStore) -> PortfolioSnapshot:
        balance_raw, positions_raw, orders_raw = await asyncio.gather(
            self._request("GET", "/portfolio/balance", authenticated=True),
            self._request(
                "GET",
                "/portfolio/positions",
                authenticated=True,
                params={"limit": 1000, "count_filter": "position"},
            ),
            self._request(
                "GET",
                "/portfolio/orders",
                authenticated=True,
                params={"limit": 1000, "status": "resting"},
            ),
        )
        balance_cents = int(balance_raw.get("balance", 0))
        equity_cents = int(balance_raw.get("portfolio_value", balance_cents))
        market_exposure = 0
        total_exposure = 0
        positions = positions_raw.get("market_positions", [])
        if not isinstance(positions, list):
            raise KalshiAPIError("positions response is malformed")
        for position in positions:
            if not isinstance(position, dict):
                continue
            exposure = dollars_to_cents_up(
                abs(Decimal(str(position.get("market_exposure_dollars", "0"))))
            )
            total_exposure += exposure
            if position.get("ticker") == ticker:
                market_exposure += exposure
        orders = orders_raw.get("orders", [])
        if not isinstance(orders, list):
            raise KalshiAPIError("orders response is malformed")
        open_order_count = 0
        for order in orders:
            if not isinstance(order, dict):
                continue
            open_order_count += 1
            exposure = _resting_order_exposure(order)
            total_exposure += exposure
            if order.get("ticker") == ticker:
                market_exposure += exposure
        return PortfolioSnapshot(
            balance_cents=balance_cents,
            total_exposure_cents=total_exposure,
            market_exposure_cents=market_exposure,
            open_orders=open_order_count,
            daily_pnl_cents=state.daily_pnl(equity_cents),
            observed_at=datetime.now(UTC),
        )


@dataclass(frozen=True)
class OrderBook:
    """Kalshi publishes two bid ladders; asks are the complement of the other side's bids."""

    yes_bids: list[tuple[Decimal, Decimal]]
    no_bids: list[tuple[Decimal, Decimal]]

    @property
    def two_sided(self) -> bool:
        return bool(self.yes_bids) and bool(self.no_bids)

    def best(self, side: str) -> tuple[Decimal, Decimal]:
        levels = self.yes_bids if side == "yes" else self.no_bids
        return max(levels, key=lambda item: item[0])


def quote_from_book(ticker: str, market: dict[str, Any], book: OrderBook) -> MarketQuote:
    if not book.two_sided:
        raise KalshiAPIError(f"{ticker} does not have a two-sided orderbook")
    yes_bid, yes_bid_size = book.best("yes")
    no_bid, no_bid_size = book.best("no")
    close_raw = market.get("close_time")
    close_time = parse_utc(str(close_raw)) if isinstance(close_raw, str) and close_raw else None
    return MarketQuote(
        ticker=ticker,
        yes_bid=yes_bid,
        yes_ask=ONE - no_bid,
        no_bid=no_bid,
        no_ask=ONE - yes_bid,
        yes_bid_size=yes_bid_size,
        yes_ask_size=no_bid_size,
        no_bid_size=no_bid_size,
        no_ask_size=yes_bid_size,
        observed_at=datetime.now(UTC),
        status=str(market.get("status", "unknown")),
        close_time=close_time,
    )


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Kalshi HTTP {response.status_code}"
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("code")
        if message:
            return f"Kalshi HTTP {response.status_code}: {message}"
    return f"Kalshi HTTP {response.status_code}"


def _levels(raw: object) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(raw, list):
        return []
    levels: list[tuple[Decimal, Decimal]] = []
    for level in raw:
        if not isinstance(level, list) or len(level) < 2:
            continue
        try:
            levels.append((Decimal(str(level[0])), Decimal(str(level[1]))))
        except InvalidOperation:
            continue
    return levels


def _v2_order_result(raw: dict[str, Any], client_order_id: str) -> OrderResult:
    filled = Decimal(str(raw.get("fill_count", "0")))
    remaining = Decimal(str(raw.get("remaining_count", "0")))
    status = OrderStatus.FILLED if remaining == 0 and filled > 0 else OrderStatus.RESTING
    return OrderResult(
        order_id=str(raw.get("order_id", "")),
        client_order_id=str(raw.get("client_order_id", client_order_id)),
        status=status,
        filled_count=filled,
        remaining_count=remaining,
        raw=raw,
    )


def _legacy_order_result(raw: dict[str, Any]) -> OrderResult:
    filled = Decimal(str(raw.get("fill_count_fp", "0")))
    remaining = Decimal(str(raw.get("remaining_count_fp", "0")))
    status_raw = str(raw.get("status", "unknown"))
    status = {
        "resting": OrderStatus.RESTING,
        "executed": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
    }.get(status_raw, OrderStatus.UNKNOWN)
    return OrderResult(
        order_id=str(raw.get("order_id", "")),
        client_order_id=str(raw.get("client_order_id", "")),
        status=status,
        filled_count=filled,
        remaining_count=remaining,
        raw=raw,
    )


def _resting_order_exposure(order: dict[str, Any]) -> int:
    remaining = Decimal(str(order.get("remaining_count_fp", "0")))
    outcome = str(order.get("outcome_side", order.get("side", "yes")))
    price_key = "yes_price_dollars" if outcome == "yes" else "no_price_dollars"
    price = Decimal(str(order.get(price_key, "0")))
    return dollars_to_cents_up(abs(remaining * price))
