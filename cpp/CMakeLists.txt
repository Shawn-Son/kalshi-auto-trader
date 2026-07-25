cmake_minimum_required(VERSION 3.18)
project(kalshi_fastcore LANGUAGES CXX)

add_library(kalshi_fastcore SHARED fast_risk.cpp)
target_compile_features(kalshi_fastcore PRIVATE cxx_std_20)
target_compile_options(kalshi_fastcore PRIVATE
    $<$<CXX_COMPILER_ID:AppleClang,Clang,GNU>:-O3 -Wall -Wextra -Wpedantic>
)
set_target_properties(kalshi_fastcore PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
)

add_executable(kalshi_fastcore_bench benchmark.cpp fast_risk.cpp)
target_compile_features(kalshi_fastcore_bench PRIVATE cxx_std_20)
target_compile_options(kalshi_fastcore_bench PRIVATE
    $<$<CXX_COMPILER_ID:AppleClang,Clang,GNU>:-O3 -Wall -Wextra -Wpedantic>
)
