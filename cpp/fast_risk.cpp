#include "fast_risk.h"

extern "C" KALSHI_EXPORT int kalshi_validate_order(const KalshiRiskInput* input) noexcept {
    if (input == nullptr) {
        return 99;
    }
    if (input->count <= 0 || input->count > input->max_count) {
        return 1;
    }
    if (input->order_notional_cents <= 0 ||
        input->order_notional_cents > input->max_order_notional_cents) {
        return 2;
    }
    if (input->market_exposure_cents + input->order_notional_cents >
        input->max_market_exposure_cents) {
        return 3;
    }
    if (input->total_exposure_cents + input->order_notional_cents >
        input->max_total_exposure_cents) {
        return 4;
    }
    if (input->open_orders >= input->max_open_orders) {
        return 5;
    }
    if (input->balance_cents < input->min_balance_cents) {
        return 6;
    }
    return 0;
}
