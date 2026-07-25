.PHONY: install test lint check cpp paper

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest

lint:
	python3 -m ruff check .
	python3 -m mypy src

check: lint test

cpp:
	cmake -S cpp -B build/cpp -DCMAKE_BUILD_TYPE=Release
	cmake --build build/cpp --config Release

paper:
	kalshi-trader run --config config/paper.toml --once
