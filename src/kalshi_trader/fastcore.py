from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path


class _RiskInput(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_int64),
        ("order_notional_cents", ctypes.c_int64),
        ("market_exposure_cents", ctypes.c_int64),
        ("total_exposure_cents", ctypes.c_int64),
        ("open_orders", ctypes.c_int64),
        ("balance_cents", ctypes.c_int64),
        ("max_count", ctypes.c_int64),
        ("max_order_notional_cents", ctypes.c_int64),
        ("max_market_exposure_cents", ctypes.c_int64),
        ("max_total_exposure_cents", ctypes.c_int64),
        ("max_open_orders", ctypes.c_int64),
        ("min_balance_cents", ctypes.c_int64),
    ]


@dataclass(frozen=True)
class NumericRisk:
    count: int
    order_notional_cents: int
    market_exposure_cents: int
    total_exposure_cents: int
    open_orders: int
    balance_cents: int
    max_count: int
    max_order_notional_cents: int
    max_market_exposure_cents: int
    max_total_exposure_cents: int
    max_open_orders: int
    min_balance_cents: int


class FastRiskCore:
    """Optional C++ C-ABI risk kernel. Python remains the safe fallback."""

    def __init__(self, library_path: Path | None = None) -> None:
        configured = library_path or (
            Path(os.environ["KALSHI_FASTCORE_LIB"]) if "KALSHI_FASTCORE_LIB" in os.environ else None
        )
        self._validate: object | None = None
        if configured is not None:
            library = ctypes.CDLL(str(configured))
            function = library.kalshi_validate_order
            function.argtypes = [ctypes.POINTER(_RiskInput)]
            function.restype = ctypes.c_int
            self._validate = function

    @property
    def native(self) -> bool:
        return self._validate is not None

    def validate(self, values: NumericRisk) -> int:
        native_input = _RiskInput(*values.__dict__.values())
        if self._validate is not None:
            return int(self._validate(ctypes.byref(native_input)))  # type: ignore[operator]
        return _python_validate(values)


def _python_validate(values: NumericRisk) -> int:
    if values.count <= 0 or values.count > values.max_count:
        return 1
    if (
        values.order_notional_cents <= 0
        or values.order_notional_cents > values.max_order_notional_cents
    ):
        return 2
    if (
        values.market_exposure_cents + values.order_notional_cents
        > values.max_market_exposure_cents
    ):
        return 3
    if values.total_exposure_cents + values.order_notional_cents > values.max_total_exposure_cents:
        return 4
    if values.open_orders >= values.max_open_orders:
        return 5
    if values.balance_cents < values.min_balance_cents:
        return 6
    return 0
