# Design decisions

Each entry: what was decided, the alternative, and why. These are the questions I would expect in a review.

## Domain

**A non-profit EHPAD group.** It is private-law, so its own team does lettrage and dunning, and it is approved for social aid, so départements pay ASH. A public EHPAD collects through the comptable public (Hélios, titres de recette), which is a different process. That is out of scope.

**No mutuelle as a payer.** Complementary insurers do not pay EHPAD hébergement. The French payers modelled are the resident (mostly direct debit), family (obligés alimentaires), the département for ASH (bulk transfer + nominative statement), the département for APA (a lump sum per facility that settles no invoice, so `non_ar`), and guardians (UDAF). The UK payers are self-funders (DD), family top-ups, local authorities (BACS + remittance advice), and the NHS (FNC/CHC). There are no insurers in the UK model: pre-funded care cover was withdrawn in 2010.

**Simplifications, stated.**
- Death credit notes are netted into the final invoice.
- Credit notes for hospital stays are issued with the invoice they reduce.
- Local authorities pay monthly, not on 4-weekly cycles.
- DD collections arrive as one bank line per resident. Real SEPA DD batches settle as one bulk credit with a detail file, which is structurally the same as a remittance-backed bulk payment.

**The payment-method mix is an assumption.** No public source gives the share of EHPAD residents paying by direct debit, so the mix sits in `config/*.yaml` and is labelled as an assumption. Everything else that matters is a configurable rate.

## Data and evaluation

**Synthetic data, truth by construction.** The generator simulates payment *intents* and resolves them with the same lettrage conventions the matcher uses: referenced invoice first, then oldest open first, and residuals of €2 or less are settled. An exact match therefore means the matcher picked the right invoices, not that it reproduced the generator's arithmetic by luck.

**Fighting circularity.** I wrote the generator, so a model can learn my noise. Four mitigations:
1. Test months contain corruption types that never appear before month 10: a bank label change, married names from new accounts, and a new remittance layout.
2. A noise sweep regenerates the client at 0.5× to 3× messiness.
3. Thirty hard cases were hand-written before any matching code.
4. A transparent rules baseline is always reported next to the model.

**Decision unit = a bank line's full allocation.** A 40-invoice transfer with 39 correct lines is one wrong decision. Edge-level and amount-weighted precision are reported as well, but never as the headline.

**Clopper-Pearson lower bound, not the point estimate.** 297 correct out of 300 is "99%" but certifies only about 97.4%. The auto threshold is the lowest score whose one-sided 95% lower bound is ≥ 0.99. When nothing can be certified, nothing is auto-posted. Scanning thresholds adds a little selection bias, which is why the untouched test months are the real check.

**Threshold sets differ by scorer, on purpose.** The rules scorer is not trained, so all nine history months are out-of-sample for it. The model is trained on months 1–7, and its threshold is chosen on a two-month shadow run (months 8–9) where it decides live with the review queue worked. That is how it would be switched on in production.

**Shadow run, not teacher-forced rows, for the model's threshold.** Training rows come from historical lettrage, where every earlier payment is perfectly lettered. Live state is not like that: lines wait in review and some are rejected. Thresholding on the shadow run prices that train/serve skew in. As it turned out, the main gap was the held-out format change (below), not the state skew.

## Matching

**Candidates first, then one scorer for every match type.** Every candidate has the same shape: edges to invoice shares, an unapplied residual, and signals. So one classifier, or one rule set, ranks a direct debit, a partial payment and a 40-line département transfer on the same scale.

**Remittance statements are the primary bulk path; subset-sum is the fallback.** This is how commercial cash-application tools work. Without a statement, the search starts from all of the period's open shares and allows at most 2–3 exclusions and 2 arrears. The simplest explanation wins: only the first level that has any solution counts. More than one valid subset means the line is never auto-posted.

**Greedy allocation, with thresholds applied after it.** Greedy is a ½-approximation of the optimal assignment. The report measures the gap against exhaustive search on small conflict components: greedy found a lower total score in 0–3 of the ~100–190 components checked per evaluation. That is too rare to justify an ILP solver here, but it is measured rather than assumed. Thresholds apply to the allocation actually chosen, not to raw candidates.

**Truncated references.** UK payment schemes cut references at 18 characters (`UNPAID DD MCH00008`). A token that is the unique prefix of one known mandate is a mandate hit. A cut invoice number is resolved among that payer's own open invoices.

**Names: order-insensitive, but a lone shared word is penalised.** `B PATEL` matches `Barbara Patel`. `RICAHRD CLAIRE` (a typo) must not match every other Claire. The payer name is read from the label only, because reference words dilute it.

## Safety

**New-format guard with probation.** A label layout never seen in the lettered history cannot be auto-posted. Lines that would otherwise have been auto-posted in it go to review instead. Why it exists: on Azur, the learned model certified 99.0% on months 8–9 but measured 98.2% on the test months. It was 99.8% in-distribution and 90.2% on the bank's new label format.

The guard lifts by the same rule as the auto threshold: the 95% Clopper-Pearson lower bound on how often reviewers accepted the top suggestion, over the lines the guard held back, must reach 99%. That is about 299 accepted in a row. It took two wrong versions to get here, and both are worth knowing:
1. *Wrong sample.* Counting every reviewed line in the layout. Partials and multi-month payments go to review in any layout, so acceptance sat at 67% (264/393 on Azur) and the guard could never lift.
2. *Too weak a test.* 30 held lines at ≥ 97% accepted. The guard lifted on Azur and released a slice that was really 95.2% (rules) and 91.0% (model) right. For the model that meant 18 wrong auto-posts, and the test-set lower bound fell to 98.4%, below target. 30/30 certifies only about 90.5%.

These numbers are from runs superseded by the current code. The cost of the right rule: a format change means weeks to months of extra review on a small client. The real fix is operational, and it is not built here. A new layout should raise an alert, so that someone teaches the label parser the new layout in week one rather than waiting out probation.

**Reminder holds.** Chasing a family that already paid is the costliest error in this domain. A reminder is held when:
- any suggestion pending review (top or alternative) touches the invoice;
- unexplained cash in the last 90 days could belong to the resident, via any of their payers' names, or equals the open share, or equals the payer's 1–3 oldest open shares together (a catch-up payment);
- a direct debit was just returned;
- the resident has left or died (that goes to the estate process).

The price is measured and reported: reminders for invoices that really were unpaid, delayed by a hold.

**Money must balance or the run fails.** After every weekly run: credits = allocated + suspense + non-AR income, no bank line is over-allocated, and no invoice share is over-allocated. This check caught three real bugs:
1. Review picks were applied before the week's automatic posts.
2. A resident and a namesake relative paying the same invoice had their two shares merged into one.
3. A mapping put constant account-number columns in as amounts, which caused (1) above to surface differently. The data-quality gate below now catches this one at load time.

## Mapping and LLM use

**Onboarding = YAML.** No client header appears in Python (tested). Header synonyms live in a data file. Where words cannot settle a question, the data does:
- the date format is the one that parses the most values;
- a document type whose amounts are negative is a credit note;
- a payer paying for many residents is a public body.

**The mapping eval uses a header pool disjoint from the mapper's vocabulary.** Otherwise a 100% score would prove nothing. The deterministic mapper gets the 3 clients 100% right, and 70% of decisions right (388/551) on the 12 unseen-vocabulary variants.

**Data-quality gates.** A template can parse every value and still be wrong; the constant sort-code columns mapped as debit and credit did exactly that. On load, bank amounts that barely vary fail, and payer shares that do not sum to 100% fail. Wrong templates fail loudly instead of loading garbage. 11 of 15 eval schemas are refused, and 1 loads while wrong.

**The LLM is used only at onboarding, never in the per-payment path.**
- Matching thousands of bank lines a week must be deterministic, cheap and explainable.
- Proposing a mapping for an unfamiliar export happens once per client, gets human confirmation, and is exactly where language understanding helps.
- The model sees column *profiles* with person names masked (`<A>`), never rows. This is health-adjacent data (GDPR, HDS hosting in France).
- Calls go through one wrapper:
  - structured output (`messages.parse` with a pydantic schema);
  - no `temperature` (current models reject it), with reproducibility from a record/replay cache committed to `llm_cache/`;
  - every call logged with tokens, latency and cost;
  - any failure (no key, refusal, truncation, API error) falls back to the deterministic mapper.
- Prompt caching is not used: these prompts are below the cacheable-prefix minimum.

**The LLM half of the mapping eval has not been run.** No API key was available while building. The harness is there; one run with a key costs well under $1 and is then replayed from cache.

## Model vs rules

**Rules ship by default; the model is the upgrade where history allows.** Once the format guard is in place, both scorers measure 100% precision on all three clients' test months (95% lower bounds 99.2–99.8%).
- **Automation:** the model is slightly higher (Azur 77.6% vs 76.2%; Maple 60.9% vs 59.3%; Client 3 58.3% vs 53.9%).
- **Review suggestions:** on Azur the model's top suggestion is right far more often (95.6% vs 91.6%), which is less clerk time per reviewed line.
- **Where the model is weaker:** on the two smaller UK-style clients its review suggestions are worse (Maple 78.4% vs 80.8%, Client 3 74.4% vs 81.0%), and on Client 3 it produced 11 wrong reminders against 2 for rules.

The rules scorer's reasons are readable by a clerk and it needs no labels to start. So: rules on day one, and the model switched on per client once its shadow run certifies and beats rules on review effort.

**The review-label feedback loop did not produce a usable model.** With a cold start, a simulated reviewer (3% error) and retraining every 4 weeks, a model trained on review labels never certified 99%. Review labels over-represent the hard cases. The real label source is the client's historical lettrage, which is what this project trains on.

**Calibration is display-only.** Sigmoid calibration is monotone, so it never changes which candidates clear the auto threshold. It only makes the probability shown to a reviewer meaningful (see the reliability diagrams).

**Explanations are rule hits, not SHAP.** "Invoice number in the reference" is something a clerk can verify; a SHAP value on `days_from_due_min` is not.
