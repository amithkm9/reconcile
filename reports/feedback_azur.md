# Feedback loop: azur, cold start, simulated reviewer (3% error)

No historical lettrage. Week 1 runs hand-tuned rules with a conservative threshold; the reviewer's decisions become labels; every 4 weeks a model is retrained on all labels so far and its threshold chosen on the most recent quarter of them. Each row is one 4-week window of live decisions.

Caveat: review labels are a biased sample (only lines that reached the queue), so the threshold is chosen on a stream that over-represents hard cases.

| window ending | labels so far | scorer in use | automation | auto precision | 95% lower bound |
|---|---|---|---|---|---|
| 2025-01-26 | 375 | rules | 77.0% | 100.0% | 99.4% |
| 2025-02-23 | 904 | rules | 71.9% | 100.0% | 99.4% |
| 2025-03-23 | 1432 | rules | 72.9% | 100.0% | 99.4% |
| 2025-04-20 | 1982 | rules | 71.8% | 100.0% | 99.3% |
| 2025-05-18 | 2465 | rules | 75.0% | 100.0% | 99.4% |
| 2025-06-15 | 2961 | rules | 74.9% | 100.0% | 99.4% |
| 2025-07-13 | 3463 | rules | 74.4% | 100.0% | 99.4% |
| 2025-08-10 | 3776 | rules | 80.6% | 100.0% | 99.4% |
| 2025-09-07 | 4203 | rules | 77.4% | 100.0% | 99.4% |
| 2025-10-05 | 4697 | rules | 52.8% | 100.0% | 98.2% |
| 2025-11-02 | 5164 | rules | 66.8% | 100.0% | 99.1% |
| 2025-11-30 | 5651 | rules | 75.0% | 100.0% | 99.4% |
| 2025-12-28 | 6197 | rules | 72.2% | 100.0% | 99.4% |
