CLIENT ?= azur
SCORER ?= rules
WEEK ?= 2025-12-31
PY := uv run python

.PHONY: data onboard mapping-eval eval run export test lint all

data:            ## synthetic exports + hidden ground truth for azur, maple, client3 (+ mapping-eval variants)
	$(PY) -m reconcile.generate --variants

onboard:         ## profile a client's exports, propose a template, confirm, save templates/$(CLIENT).yaml (YES=1 to skip the prompt)
	$(PY) -m reconcile.mapping.onboard --client $(CLIENT) $(if $(YES),--yes,)

mapping-eval:    ## proposed templates vs true mapping on 3 clients + 12 variant schemas
	$(PY) -m reconcile.experiments mapping

eval:            ## history -> thresholds -> weekly test runs; writes reports/eval_$(CLIENT)_*.md and state/
	$(PY) -m reconcile.pipeline eval --client $(CLIENT) --scorer $(SCORER)

run:             ## one weekly run on saved state (idempotent)
	$(PY) -m reconcile.pipeline run --client $(CLIENT) --week $(WEEK)

export:          ## lettrage file for the billing software
	$(PY) -m reconcile.pipeline export --client $(CLIENT)

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

all: data
	for c in azur maple client3; do $(MAKE) eval CLIENT=$$c SCORER=rules || exit 1; done
	$(MAKE) mapping-eval
