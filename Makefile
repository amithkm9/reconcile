CLIENT ?= azur
SCORER ?= rules
WEEK ?= 2025-12-31
PY := uv run python

.PHONY: data onboard mapping-eval hard-cases eval sweep feedback run export app test lint all experiments

data:            ## synthetic exports + hidden ground truth for azur, maple, client3 (+ mapping-eval variants)
	$(PY) -m reconcile.generate --variants

onboard:         ## profile a client's exports, propose a template, confirm, save templates/$(CLIENT).yaml (YES=1 to skip the prompt)
	$(PY) -m reconcile.mapping.onboard --client $(CLIENT) $(if $(YES),--yes,)

mapping-eval:    ## proposed templates vs true mapping on 3 clients + 12 variant schemas
	$(PY) -m reconcile.experiments mapping

hard-cases:      ## the 30 hand-written cases through the real pipeline; writes reports/hard_cases.md
	$(PY) -m reconcile.hardcases

eval:            ## history -> thresholds -> weekly test runs; writes reports/eval_$(CLIENT)_*.md and state/
	$(PY) -m reconcile.pipeline eval --client $(CLIENT) --scorer $(SCORER)

sweep:           ## noise sweep: regenerate at 0.5x..3x messiness, full protocol each time
	$(PY) -m reconcile.experiments sweep --client $(CLIENT) --scorer $(SCORER)

feedback:        ## cold start + simulated reviewer + periodic retraining, a year of weekly runs
	$(PY) -m reconcile.experiments feedback --client $(CLIENT)

run:             ## one weekly run on saved state (idempotent)
	$(PY) -m reconcile.pipeline run --client $(CLIENT) --week $(WEEK)

export:          ## lettrage file for the billing software
	$(PY) -m reconcile.pipeline export --client $(CLIENT)

app:             ## Streamlit review queue + reminder approval on the saved state
	uv run streamlit run app/review_app.py

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

all: data
	for c in azur maple client3; do $(MAKE) eval CLIENT=$$c SCORER=rules || exit 1; done
	$(MAKE) mapping-eval
	$(MAKE) hard-cases

experiments:     ## the slower reports: learned model on all 3 clients, noise sweep, feedback loop
	for c in azur maple client3; do $(MAKE) eval CLIENT=$$c SCORER=model || exit 1; done
	$(MAKE) sweep CLIENT=azur SCORER=rules
	$(MAKE) feedback CLIENT=azur
