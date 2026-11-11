# Hard cases

30 hand-written situations from `eval/hard_cases.yaml`, run through the real weekly pipeline with the rules scorer and fixed thresholds (auto >= 0.95, review >= 0.30). 30/30 pass.

| case | expected | got | score | first reason | result |
|---|---|---|---|---|---|
| dd_exact | auto | auto | 1.000 | + direct-debit mandate matches | pass |
| dd_return_reopens_invoice | auto | auto | 1.000 | + reverses a collected direct debit | pass |
| family_daughter_with_invoice_number | auto | auto | 0.982 | + invoice number in the reference | pass |
| family_daughter_free_text | auto | auto | 0.982 | + payer name matches the payer table | pass |
| daughter_married_name_unknown_account | review | review | 0.881 | + resident's surname in the label | pass |
| married_name_learned_from_review | auto/review | auto | 0.999 | + payer name matches the payer table | pass |
| typo_in_payer_name | auto/review | auto | 0.971 | + payer name matches the payer table | pass |
| partial_600_of_2400 | review | review | 0.622 | + payer name matches the payer table | pass |
| two_months_at_once | auto/review | auto | 0.982 | + payer name matches the payer table | pass |
| same_amount_two_open_months_no_reference | auto/review | auto | 0.971 | + payer name matches the payer table | pass |
| overpayment | review | review | 0.378 | + invoice number in the reference | pass |
| duplicate_payment_nothing_left_open | unapplied/review | unapplied | - | - | pass |
| ash_bulk_with_statement | auto | auto | 0.996 | + every statement line matched to a resident | pass |
| ash_bulk_statement_new_layout | auto/review | auto | 0.996 | + every statement line matched to a resident | pass |
| ash_bulk_statement_arrives_late | auto/review | auto | 0.982 | + transfer equals the payer's open shares for the period exactly | pass |
| ash_bulk_one_resident_excluded | review | review | 0.818 | + only one combination of open shares fits the amount | pass |
| guardian_bulk_exact | auto | auto | 0.982 | + transfer equals the payer's open shares for the period exactly | pass |
| guardian_bulk_ambiguous | review | review | 0.378 | - several combinations of invoices fit the amount (never auto) | pass |
| apa_dotation_is_not_a_receivable | non_ar | non_ar | - | - | pass |
| bank_interest | non_ar | non_ar | - | - | pass |
| unknown_payer | unapplied | unapplied | 0.119 | + resident's surname in the label | pass |
| deposit_refund_debit | non_ar | non_ar | - | - | pass |
| dd_split_share | auto | auto | 1.000 | + direct-debit mandate matches | pass |
| family_split_share_no_reference | auto/review | auto | 0.989 | + payer name matches the payer table | pass |
| reminder_drafted_when_really_unpaid | FA2025-000402: drafted | FA2025-000402: drafted | - | - | pass |
| reminder_held_for_deceased_resident | FA2025-000802: held:deceased | FA2025-000802: held:deceased | - | - | pass |
| reminder_held_by_unapplied_cash | FA2025-000202: held:unapplied_cash | unapplied; FA2025-000202: held:unapplied_cash | - | - | pass |
| reminder_held_by_pending_review | FA2025-000202: held:pending_review | review; FA2025-000202: held:pending_review | 0.622 | + payer name matches the payer table | pass |
| reminder_held_after_dd_return | FA2025-000102: held:dd_return | FA2025-000102: held:dd_return | - | - | pass |
| small_residual_is_settled | FA2025-000202: none | FA2025-000202: none | - | - | pass |
