# Pre-registration

This file was written in the first commit (`1912ff0`), before any matching, scoring or decision code existed, together with the 30 hard cases in `eval/hard_cases.yaml`. Neither file has been changed since; `git diff 1912ff0 HEAD -- eval/hard_cases.yaml` is empty. It is kept so that a reader can check the metrics were fixed before the results were known.

**Deviations from this plan, declared:**
1. **Rules thresholds use months 1–9, not 8–9.** The rules scorer is not trained, so all nine history months are out-of-sample for it, and months 8–9 alone held too few lines to certify 99% on the smaller clients. The learned model still uses the months 8–9 shadow run as planned.
2. **A new-format guard was added.** The bank's label change in month 10 broke the model's certified precision on the test months; see [decisions.md](decisions.md#safety).
3. **Reminders are per payer share, not per invoice.** "Wrong reminder" is therefore measured per share (a reminder to a payer whose own share was already paid). One invoice with a département share and a family share can produce two reminders.
4. **The test quarter is 14 weekly runs** (13 Sundays plus the 31 December stub week).

The original text follows unchanged, except that a reference to a working brief outside this repository was removed.

---

**Goal.** A payment-to-invoice matching agent for care-home groups. It is measured against exact ground truth, and onboarding a new client takes a mapping template, not code.

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
