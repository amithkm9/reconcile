# Feedback loop: azur, cold start, simulated reviewer (3% error)

No historical lettrage. Week 1 runs hand-tuned rules with a conservative threshold; the reviewer's decisions become labels; every 4 weeks a model is retrained on all labels so far and its threshold chosen on the most recent quarter of them. Each row is one calendar month of bank lines (by line date). From October the bank's transfer labels use a new layout; the last column counts auto-posts the new-format guard held back for review.

Caveat: review labels are a biased sample (only lines that reached the queue), so the threshold is chosen on a stream that over-represents hard cases.

| month | labels so far | scorer in use | automation | auto precision | 95% lower bound | held by format guard |
|---|---|---|---|---|---|---|
| 2025-01 | 375 | rules | 76.5% | 100.0% | 99.4% | 11 |
| 2025-02 | 904 | rules | 71.9% | 100.0% | 99.4% | 9 |
| 2025-03 | 1475 | rules | 72.7% | 100.0% | 99.4% | 9 |
| 2025-04 | 2002 | rules | 71.8% | 100.0% | 99.3% | 8 |
| 2025-05 | 2510 | rules | 73.4% | 100.0% | 99.4% | 6 |
| 2025-06 | 3067 | rules | 72.9% | 100.0% | 99.4% | 11 |
| 2025-07 | 3630 | rules | 71.7% | 100.0% | 99.4% | 6 |
| 2025-08 | 4096 | rules | 75.4% | 100.0% | 99.4% | 8 |
| 2025-09 | 4699 | rules | 70.2% | 100.0% | 99.4% | 7 |
| 2025-10 | 5168 | rules | 74.3% | 100.0% | 99.4% | 6 |
| 2025-11 | 5696 | rules | 73.6% | 100.0% | 99.4% | 7 |
| 2025-12 | 6284 | rules | 70.0% | 100.0% | 99.4% | 9 |
