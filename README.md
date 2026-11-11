# Reconcile

Reconcile reads a care home's weekly bank export and its invoice list, and works out which payment settles which resident's invoice. It posts only the matches it can prove. Everything else goes to a clerk, with a suggested answer and the evidence for it. It drafts payment reminders only when no unexplained money could belong to that family.

It is built for French EHPAD groups. A UK client and a client with a scrambled export format are included to show that onboarding a new export format is a configuration file, not code.

> **Status:** a work sample on synthetic data with an exact answer key. It has not run on real books.

![Review queue: one bank line, the suggested invoice, and the reasons](docs/img/review_queue.png)

## Result

On a held-out quarter for three simulated care-home groups, the system auto-posted **2,319 bank lines, and every one was exactly right**: a 95% lower bound of 99.2–99.8% precision per client.

Two facts belong next to that number:
- **Most of those are direct debits.** They are 55.1–74.9% of the receivable lines in this simulation (an assumed mix), and matching them by mandate reference is the easy part.
- **Everything else is mostly reviewed.** For family transfers, partial payments and bulk payments, the system auto-posts only 0.3–6.2%. It queues the rest for a clerk, with the right answer as the first suggestion 59–69% of the time.

Reminders: 1–2 of the 136–255 drafts per client were for a share already paid, and no overdue unpaid share was missed.

The value is less in the automation rate than in the safety around it:
- nothing is posted unless certified;
- an ambiguous payment is never guessed;
- a family that may have paid is not chased;
- every euro is accounted for after every run;
- a new client is onboarded from its own export files with a YAML template.

## See it in two minutes

You need [uv](https://docs.astral.sh/uv/). No API key is needed.

```bash
make all     # generate 3 clients, run the evaluation, write reports/ (about 2 minutes)
make app     # open the review queue and reminder approval at http://localhost:8501
```

Then, for any bank line:

```bash
uv run python -m reconcile.pipeline explain --client azur --line b3c146d4
```

```
bank line b3c146d4312128e5  2025-12-29  941.16
  label:     VIREMENT RECU /DE MME PETIT RENEE
  reference: /MOTIF FA2025-007208
outcome: review
thresholds: auto >= 0.9959, review >= 0.7311
top: reference  score 0.9759
    FA2025-007208  Renée Petit (family)  941.16
    + invoice number in the reference
    + amount settles the invoice share(s) exactly
    + oldest open invoice first
    - several payers could match this line
    + same amount as this payer's recent payments
alt 1: name_fifo  score 0.1192
    FA2025-004064  Thérèse Petit (family)  941.16
...
```

The evidence is strong but sits just under the certified threshold, because several payers named Petit have open invoices. So a clerk confirms it with one click. `reports/lettrage_azur.csv` is what the billing software would receive. All names in it are generated.

## Why the numbers can be trusted

1. **Truth by construction.** The generator simulates who meant to pay what, then renders it into each client's export formats. The answer key never reaches the pipeline.
2. **Fixed before the code.** The metric definitions and the 30 hard cases were committed in the first commit (`1912ff0`), before any matching code, and have not changed since. Four deviations are declared in [docs/preregistration.md](docs/preregistration.md).
3. **A quarter it never saw.** Thresholds are certified on months 1–9 only. The test quarter adds problems that never occur earlier: the bank changes its label format, family members pay under married names from new accounts, a département changes its statement layout.
4. **A bound, not a point estimate.** The auto threshold is the lowest score whose 95% Clopper–Pearson lower bound on precision is ≥ 99%. When nothing certifies, nothing is posted. This happens to the learned model on two clients ([decisions](docs/decisions.md#model-vs-rules)).
5. **Reproducible.** `make all` regenerates the data and the rules results from scratch, byte for byte; `make experiments` adds the model comparison, noise sweep and feedback loop.

## Results (rules scorer, test months 10–12, 14 weekly runs)

Every number below is in `reports/eval_<client>_rules.md` or `reports/metrics_<client>_rules.json`.

| | Azur (FR, 600 residents) | Maple (UK, 300) | Client 3 (UK, scrambled schema) |
|---|---|---|---|
| Auto-posted lines (wrong) | 1473 (0) | 453 (0) | 393 (0) |
| Precision, 95% lower bound | 99.8% | 99.3% | 99.2% |
| Automation, all receivable lines | 76.2% | 59.3% | 53.8% |
| Automation, excluding direct debits | 5.3% | 6.2% | 0.3% |
| Review queue: first suggestion right | 69.1% | 59.0% | 63.6% |
| Review lines per week | 30.7 | 20.9 | 22.9 |
| Left unapplied (suspense) | 30 | 18 | 19 |
| Reminder drafts / for a share already paid | 255 / 1 | 151 / 2 | 136 / 2 |
| Overdue unpaid shares never drafted or held | 0 | 0 | 0 |
| Money balanced after every weekly run | yes | yes | yes |

A learned model (gradient boosting) is compared in [docs/decisions.md](docs/decisions.md#model-vs-rules):
- **On Azur** it certifies, and in review its first suggestion is right 90.8% of the time against 69.1%.
- **On the two smaller clients** it falls just short of certifying (98.99% and 98.98%), so it posts nothing.

Rules therefore ship first, and the model is switched on per client.

### What it does with each kind of payment

There are 30 hand-written situations, run through the real weekly pipeline: **30/30 pass** ([reports/hard_cases.md](reports/hard_cases.md)). They use fixed thresholds (auto ≥ 0.95) so they do not depend on one dataset. Under Azur's certified threshold of 0.996, the rows marked auto at 0.98 below would go to review instead.

| Situation | Outcome |
|---|---|
| Direct debit returned unpaid (`REJET PRLV`) | posted automatically, invoice re-opened |
| Daughter pays under her married name, from an unknown account | review, correct invoice suggested first |
| Same daughter next month, after a clerk confirmed her once | auto (the alias was learned) |
| Partial payment, €600 of €2,400 | review: never automatic |
| Département transfer whose statement arrives a week later | auto (0.98), via the payer's open shares for the period |
| Guardian payment that several combinations of invoices fit | review: "several combinations fit (never auto)" |
| APA lump sum from the département | recognised as not a receivable |
| Reminder due, but unexplained cash from the family arrived last week | reminder held |

On the real test quarter, married-name payments are much harder than this hand-written case: the right answer is first for only 1 of 7 on Azur.

## Onboarding a new client

```bash
make onboard CLIENT=client3   # profile the exports, propose a template, ask for confirmation
make eval CLIENT=client3      # history -> thresholds -> 14 weekly runs -> report
```

Client 3 has different headers, a signed amount instead of debit/credit columns, other category codes and a junk column. The proposal flags what it inferred from the data rather than from headers, for the human to confirm:

```
billing_export.csv   doc_type value     ADJ  -> credit  conf=0.50  <-- inferred from data, please check
funding_sources.csv  payer_type value   TPT  -> family  conf=0.50  <-- inferred from data, please check
```

No Python file contains a client's header (a test enforces it). The committed templates are exactly what `onboard` proposes.

**How well the mapper does on export formats it has never seen** ([reports/mapping_eval.md](reports/mapping_eval.md)):
- **The 3 clients:** 100% correct.
- **12 extra schemas** whose headers come from a vocabulary the mapper does not know: none onboards correctly unattended. 388/551 mapping decisions are right (70.4%).
- **What the checks catch:** 11 of the 12 are refused loudly by the drift and data-quality checks, so the mistake surfaces at onboarding, not in the books.
- **The failure that matters:** 1 (`variant_11_maple`) loads while wrong.

## Where it fails

1. **The bank's new label format.** In the test quarter, Azur's bank changes how transfers are labelled. Lines in the new format (420 of 1,933) are never auto-posted, and the right answer is first for only 67.9% of them, against 99.7% for unchanged lines. A guard holds them for review until reviewed lines of the new layout certify ≥ 99%. A looser guard I tried released a slice that was 91% right. The real fix, not built, is an alert on a new layout so someone updates the label parser in week one.
2. **Wrong reminders: 1 / 2 / 2.** All five come from the held-out changes: a married name from a new account, or a typo in the new label format. Each one is listed in the eval reports with the bank line that had paid it.
3. **The holds have a price.** Holds delayed about 330 reminders per client per quarter for shares that really were unpaid. That is the cost of chasing almost no one who already paid.
4. **Messier data costs review effort, not precision.** In the noise sweep, every messiness rate was multiplied by 0.5× to 3×:
   - precision stayed 100% (99.8% bound) throughout;
   - the first suggestion in review dropped from 87.1% to 60.7% right;
   - wrong reminders went from 0 to 4.
5. **Learning from the review queue alone did not work.** With a cold start, no history and retraining every 4 weeks, a model trained on review labels never certified. Review labels over-represent hard cases; a client's historical lettering is the real training signal.

## Where the LLM is used, and where it is not

- **Used:** only at onboarding, to propose a mapping template from column profiles. Words that could be names are masked before anything is sent; a test checks that no resident or family name from the three clients appears.
  - Every proposal is validated: only discovered files, existing columns and allowed values.
  - The human confirms the final template, with each disagreement against the deterministic mapper flagged.
  - Any failure falls back to the deterministic mapper.
- **Not used:** anywhere in the weekly path. Matching thousands of lines a week has to be deterministic, cheap and explainable, and reminders are template text.
- **Not yet measured:** no API key was available while building. Every result above comes from the deterministic mapper.

## Limitations

- **Synthetic data.** I wrote both the generator and the matcher, so circularity is the main risk. The mitigations:
  - held-out changes in the test quarter;
  - a noise sweep;
  - hard cases written before the matcher;
  - a rules baseline next to the model.

  None of it replaces a week on real books.
- **Simplifications,** listed in [docs/decisions.md](docs/decisions.md#domain). Examples:
  - direct debits arrive as one line per resident;
  - local authorities pay monthly;
  - credit notes arrive with their invoice.
- **The review app** is a single-operator tool on localhost, with no login.
- **What remains open** is listed in [docs/review-log.md](docs/review-log.md#not-fixed).

## What I would do with your data

[docs/first-weeks.md](docs/first-weeks.md): week 1 onboard and replay a year of your lettering; week 2 shadow mode next to your clerks; week 3 switch on only what certified on your history. It starts with seven questions to ask in week 1.

## Repository guide

| Document | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Code map, data flow, where each invariant is enforced and tested |
| [docs/decisions.md](docs/decisions.md) | Every design decision, the alternative, and why; model vs rules |
| [docs/review-log.md](docs/review-log.md) | What independent correctness, security, quality and documentation reviews found, and what was fixed |
| [docs/glossary.md](docs/glossary.md) | Lettrage, ASH, APA, RUM, UDAF, and the project's own terms |
| [docs/preregistration.md](docs/preregistration.md) | Metrics and risks as written before the matching code |
| `reports/` | Every evaluation report and plot. Plots: [noise sweep](reports/noise_sweep_azur_rules.png), [cold-start feedback loop](reports/feedback_azur.png), reliability diagrams ([Azur](reports/reliability_azur.png), [Maple](reports/reliability_maple.png), [Client 3](reports/reliability_client3.png)) |

```bash
make all           # data + rules evaluation + mapping eval + hard cases
make experiments   # learned model on 3 clients, noise sweep, feedback loop
make test          # 75 tests
make lint          # ruff + ty
make run CLIENT=azur WEEK=2025-12-31   # one weekly run on saved state; rerunning imports nothing new
make export CLIENT=azur                # lettrage CSV for the billing software
```

![Reminders: drafts waiting for approval, and holds with their reason](docs/img/reminders.png)

## About

<!-- Fill in before sending: your name, contact, and why this problem. -->
Built by **[your name]** ([email / LinkedIn]) as a work sample for a forward-deployed engineering role.

I wrote it with an AI coding assistant. I specified the problem, the metrics and the hard cases before any matching code. I made the design decisions in [docs/decisions.md](docs/decisions.md), and I checked every number against the reports. The assistant wrote most of the code. I then had the repository independently reviewed for correctness, security and quality, and fixed what was found ([review log](docs/review-log.md)).
