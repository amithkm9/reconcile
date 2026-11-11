# Noise sweep: azur, rules scorer

Every messiness rate (typos, missing references, partial and multi-month payments, missing statements, unknown credits...) multiplied by k; same seed; full protocol each time (thresholds re-chosen on history, reported on months 10-12).

| k | auto precision | 95% lower bound | automation | review: top suggestion right | review / week | wrong reminders |
|---|---|---|---|---|---|---|
| 0.5 | 100.0% | 99.8% | 76.8% | 87.1% | 31.4 | 0 |
| 1.0 | 100.0% | 99.8% | 76.2% | 69.1% | 30.7 | 1 |
| 1.5 | 100.0% | 99.8% | 75.8% | 63.8% | 31.4 | 0 |
| 2.0 | 100.0% | 99.8% | 76.2% | 61.1% | 31.2 | 1 |
| 3.0 | 100.0% | 99.8% | 74.7% | 60.7% | 34.4 | 4 |
