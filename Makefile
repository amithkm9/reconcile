CLIENT ?= azur
PY := uv run python

.PHONY: data test lint

data:
	$(PY) -m reconcile.generate --variants

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .
