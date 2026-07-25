#pragma once

#include <cstdint>

#if defined(_WIN32)
#define KALSHI_EXPORT __declspec(dllexport)
#else
#define KALSHI_EXPORT __attribute__((visibility("default")))
#endif

extern "C" {

struct KalshiRiskInput {
    std::int64_t count;
    std::int64_t order_notional_cents;
    std::int64_t market_exposure_cents;
    std::int64_t total_exposure_cents;
    std::int64_t open_orders;
    std::int64_t balance_cents;
    std::int64_t max_count;
    std::int64_t max_order_notional_cents;
    std::int64_t max_market_exposure_cents;
    std::int64_t max_total_exposure_cents;
    std::int64_t max_open_orders;
    std::int64_t min_balance_cents;
};

KALSHI_EXPORT int kalshi_validate_order(const KalshiRiskInput* input) noexcept;

}
