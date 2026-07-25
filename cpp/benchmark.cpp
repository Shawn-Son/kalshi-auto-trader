#include "fast_risk.h"

#include <chrono>
#include <cstdint>
#include <iostream>

int main() {
    const KalshiRiskInput input{
        2, 100, 200, 500, 1, 10'000, 5, 250, 1'000, 2'500, 10, 1'000,
    };
    constexpr std::int64_t iterations = 10'000'000;
    volatile int result = 0;
    const auto start = std::chrono::steady_clock::now();
    for (std::int64_t index = 0; index < iterations; ++index) {
        result = kalshi_validate_order(&input);
    }
    const auto elapsed = std::chrono::steady_clock::now() - start;
    const auto nanoseconds =
        std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count();
    std::cout << "result=" << result
              << " ns/check=" << static_cast<double>(nanoseconds) / iterations << '\n';
}
