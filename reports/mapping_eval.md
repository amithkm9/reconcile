# Mapping eval: proposed templates vs the true mapping

15 schemas: the 3 clients plus 12 generated variants (renamed/reordered columns, other separators, number and date conventions, signed vs debit/credit bank amounts, renamed category values, a junk column). Variant headers come from a generator-only pool, disjoint from the mapper's vocabulary.

Each *decision* is one thing the template must get right: a file's role, a field's column, a file's date format or decimal separator, the bank amount mode, or one category value mapping.

| Mapper | Decisions correct | Schemas fully correct | Templates that load and pass the data-quality gates (wrong ones that still load) |
|---|---|---|---|
| deterministic fallback | 526/689 (76.3%) | 3/15 | 4/15 (1 silently wrong) |
| claude-sonnet-5-5 | not run (no API key, no cached responses) | | |
| claude-haiku-5-5 | not run (no API key, no cached responses) | | |

## Per schema (fallback)

| schema | accuracy | loads | flags raised | seconds | wrong decisions |
|---|---|---|---|---|---|
| azur | 100.0% | True | 0 | 0.87 | - |
| maple | 100.0% | True | 0 | 0.57 | - |
| client3 | 100.0% | True | 2 | 0.76 | - |
| variant_00_azur | 82.6% | False | 4 | 0.1 | invoices.resident_id, invoices.original_invoice, payers.resident_id, payers.resident_name, payers.share_pct, payers.resident_exit_date, payers.payer_type=guardian, remittances.remittance_id |
| variant_01_maple | 75.6% | False | 4 | 0.11 | invoices.resident_id, invoices.resident_name, invoices.period, invoices.original_invoice, payers.payer_type, payers.resident_exit_date, payers.payer_type=resident, payers.payer_type=family, payers.payer_type=public_aid, payers.payer_type=health_funder, remittances.amount |
| variant_02_azur | 84.8% | False | 4 | 0.12 | invoices.resident_id, invoices.original_invoice, payers.resident_id, payers.resident_name, payers.payer_name, payers.payer_type=guardian, bank.date |
| variant_03_maple | 71.7% | False | 6 | 0.11 | invoices.issue_date, invoices.due_date, invoices.resident_name, invoices.period, invoices.original_invoice, payers.payer_type, payers.mandate_ref, payers.date_format, payers.payer_type=resident, payers.payer_type=family, payers.payer_type=public_aid, payers.payer_type=health_funder, remittances.remittance_id |
| variant_04_azur | 84.8% | False | 4 | 0.09 | invoices.resident_id, invoices.original_invoice, payers.resident_id, payers.resident_name, payers.payer_name, payers.resident_exit_date, payers.payer_type=guardian |
| variant_05_maple | 65.2% | False | 5 | 0.11 | invoices.resident_id, invoices.resident_name, invoices.facility, invoices.period, invoices.original_invoice, payers.resident_id, payers.payer_type, payers.mandate_ref, payers.payer_type=resident, payers.payer_type=family, payers.payer_type=public_aid, payers.payer_type=health_funder, remittances.remittance_id, bank.debit, bank.credit, bank.amount_mode |
| variant_06_azur | 39.1% | False | 14 | 0.09 | invoices.role, invoices.invoice_id, invoices.issue_date, invoices.due_date, invoices.resident_id, invoices.resident_name, invoices.facility, invoices.period, invoices.doc_type, invoices.amount, invoices.original_invoice, invoices.doc_type=invoice, invoices.doc_type=credit, payers.resident_id, payers.resident_name, payers.payer_name, payers.share_pct, payers.resident_exit_date, payers.payer_type=public_aid, payers.payer_type=guardian, remittances.role, remittances.remittance_id, remittances.date, remittances.payer_name, remittances.facility, remittances.beneficiary_name, remittances.period, remittances.amount |
| variant_07_maple | 63.0% | False | 5 | 0.13 | invoices.resident_id, invoices.period, invoices.original_invoice, payers.resident_id, payers.payer_type, payers.mandate_ref, payers.resident_exit_date, payers.date_format, payers.payer_type=resident, payers.payer_type=family, payers.payer_type=public_aid, payers.payer_type=health_funder, remittances.remittance_id, remittances.amount, bank.debit, bank.credit, bank.amount_mode |
| variant_08_azur | 82.6% | False | 4 | 0.09 | invoices.resident_id, invoices.original_invoice, payers.resident_id, payers.share_pct, payers.resident_exit_date, payers.date_format, payers.payer_type=guardian, remittances.remittance_id |
| variant_09_maple | 82.6% | False | 5 | 0.1 | invoices.resident_name, invoices.period, invoices.original_invoice, payers.resident_name, payers.resident_exit_date, payers.date_format, payers.payer_type=health_funder, remittances.amount |
| variant_10_azur | 29.8% | False | 7 | 0.11 | invoices.role, invoices.invoice_id, invoices.issue_date, invoices.due_date, invoices.resident_id, invoices.resident_name, invoices.facility, invoices.period, invoices.doc_type, invoices.amount, invoices.original_invoice, invoices.doc_type=invoice, invoices.doc_type=credit, payers.resident_id, payers.resident_name, payers.payer_name, payers.share_pct, payers.mandate_ref, payers.resident_exit_date, payers.date_format, payers.payer_type=guardian, remittances.role, remittances.remittance_id, remittances.date, remittances.payer_name, remittances.facility, remittances.beneficiary_name, remittances.period, remittances.amount, bank.debit, bank.credit, bank.decimal, bank.amount_mode |
| variant_11_maple | 84.4% | True | 5 | 0.11 | invoices.issue_date, invoices.resident_id, invoices.period, invoices.original_invoice, payers.resident_name, payers.resident_exit_date, remittances.remittance_id |

## Most frequent mistakes

- resident_id: 18 schema(s)
- resident_name: 13 schema(s)
- original_invoice: 12 schema(s)
- period: 10 schema(s)
- resident_exit_date: 9 schema(s)
- remittance_id: 8 schema(s)
- amount: 7 schema(s)
- payer_type=guardian: 6 schema(s)
- payer_name: 6 schema(s)
- payer_type=public_aid: 5 schema(s)
- payer_type=health_funder: 5 schema(s)
- date_format: 5 schema(s)
- facility: 5 schema(s)
- share_pct: 4 schema(s)
- payer_type: 4 schema(s)
- payer_type=resident: 4 schema(s)
- payer_type=family: 4 schema(s)
- issue_date: 4 schema(s)
- mandate_ref: 4 schema(s)
- role: 4 schema(s)
- date: 3 schema(s)
- due_date: 3 schema(s)
- debit: 3 schema(s)
- credit: 3 schema(s)
- amount_mode: 3 schema(s)
- invoice_id: 2 schema(s)
- doc_type: 2 schema(s)
- doc_type=invoice: 2 schema(s)
- doc_type=credit: 2 schema(s)
- beneficiary_name: 2 schema(s)
- decimal: 1 schema(s)
