# PLAN

**Goal.** A payment-to-invoice matching agent for care-home groups. It is measured against exact ground truth, and onboarding a new client takes a mapping template, not code. The full brief, with metric definitions, is in the parent folder (`netter_recon_claude_code_prompt_v2.md`).

## Structure
- `reconcile/generate/`: simulates intents, renders client files, writes truth to `_truth/`.
- `reconcile/mapping/`: profile, propose (LLM + fallback), confirm, validate.
- Matching path:
  - `normalize`
  - `candidates`: DD/reference, remittance, bulk subset, name + FIFO.
  - `features`
  - `scorer`: rules, then GBM.
  - `allocate`: greedy.
  - `decide`: Clopper–Pearson threshold.
- `state`: sqlite; `run_week` is the single code path for weekly runs and the batch eval.
- `reminders`, `audit`, `export`, `review` (shared by the Streamlit app and the simulated reviewer).
- `evaluate`, `experiments`: shift, sweep, feedback, mapping eval.

## Metric definitions (fixed before the matching code)
- **Decision unit.** One bank line's full allocation, as a set of (invoice, cents). It counts as correct only if it equals the truth exactly.
- **Outcomes per line.** `auto`, `review`, `unapplied`, `non_ar`. Invoice status is derived from the outcomes.
- **Splits (by month).** 1–7 train, 8–9 threshold selection, 10–12 test. The test months include held-out corruption.
- **Auto threshold.** The lowest score whose one-sided 95% Clopper–Pearson lower bound on precision, on months 8–9, is at least 0.99.
- **Wrong reminder.** A draft for an invoice that truth says was paid (within €2) by the draft date.

## Biggest risks
1. **Circularity.** The data is synthetic and the generator is mine. Mitigations: held-out corruption types in the test months, a noise sweep, hard cases written before the code, and a rules baseline reported next to the model.
2. **Too few items to certify 0.99.** If so, report the bound and the n honestly and auto-post nothing.
3. **Hard-coded client knowledge.** Mitigations: a held-out Client 3 schema, a test that no client header appears in the Python code, and a test that onboarding changes no `.py` file.
