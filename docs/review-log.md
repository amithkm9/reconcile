# Review log

Before publishing, the repository went through four independent read-only reviews:
- correctness;
- security;
- dead code and code quality;
- documentation, read as a founder would.

Each finding below was either fixed, with a test where it can be tested, or is listed under "Not fixed".

## Correctness

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | After save and load, the new-format guard counted *every* posted line as "trusted layout", so `make run` on saved state auto-posted lines the in-memory run would hold. | `Ledger.record` is the single booking path for both live posts and `load_state`, so derived state is rebuilt exactly. | `test_saved_state_reloads_identically` |
| 2 | `apply_decision` did not check whether a line was already resolved. A second click, or a stale browser tab, posted the same bank line twice and broke the money balance. | It refuses resolved lines. The app identifies queue items by bank hash and checks the balance before saving. | `test_review_actions_post_once_and_refuse_conflicts` |
| 3 | A credit note issued *after* the invoice was paid made every later run fail the balance check. | The payer is in credit: reported as `refund_due_cents`, not an error. | `test_credit_note_after_payment_is_a_refund_not_a_failure` |
| 4 | A remittance candidate could allocate more than the bank line (a short-paid statement). | Allocation is capped at the line amount; `Ledger.post` rejects any posting larger than its line. | `test_remittance_never_allocates_more_than_the_line` |
| 5 | The app kept a stale copy of the state, so a click after a terminal `make run` overwrote that week. `save_state` deleted the file before rewriting it. | The app reloads when the file changes; saves are written to a temporary file and swapped in atomically. | |
| 6 | Payer payment history was rebuilt with one entry per allocation row instead of one per payment, so features differed after a reload. | Same fix as 1. | `test_saved_state_reloads_identically` |
| 7 | Approving or rejecting a reminder draft let it be redrafted a week later. | Approved and rejected drafts count as drafted. | `test_an_approved_reminder_is_not_redrafted_a_week_later` |
| 8 | A bank row with an unparseable date or amount crashed hashing, or produced a float hash that defeats idempotent import. | Such rows (at most 2%, or loading fails) are dropped before hashing; amounts are integers. | |
| 9 | Reminders went only to the first payer of an invoice, so a family's share was never chased when the département's was also open. | One reminder per payer share; holds and redraft timing per share. The evaluation now scores reminders per share. | |
| 10 | If the model file was missing, `make run` silently switched to the rules scorer but kept thresholds certified for the model. | It fails instead. | |
| 11 | A December payment marked "JANVIER" was matched to January of the same year. | The nearest such month. | `test_month_name_in_december_means_next_january` |
| 12 | Duplicate lines inside one export were counted on raw text but hashed on normalised text. | Both use normalised text. | |

**Found while verifying the fixes:**
- **Missed reminders.** All 6 missed reminders (Maple 4, Client 3 2) had one cause: UK schemes cut references at 18 characters, so `UNPAID DD MCH00001` fits ten mandates. The returned direct debit was never posted, the ledger thought the invoice was paid, and no reminder went out. An ambiguous return is now offered to a reviewer with every payer it could be, and is never auto-posted (`test_truncated_dd_return_is_queued_with_every_payer_it_could_be`). Missed reminders went to 0.
- **Certification population.** Threshold certification now uses only lines the decision rule could actually auto-post, not ambiguous lines that are always reviewed.

## Security

The scope is a local tool on synthetic data. Severity is rated for the day it runs on real client data.

| # | Finding | Fix |
|---|---|---|
| 1 | **Name masking leaked names.** A surname column with few distinct values looked like a category and was sent to the LLM unmasked; names inside codes (`DUPONT01`) and identifier samples leaked too. | Every LLM-bound sample is masked: any letter run of 4+ that is not vocabulary becomes `<A>`; constant columns send only their shape. `test_llm_profiles_carry_no_person_names` checks all three clients. Residual risk: names of 3 letters or fewer. |
| 2 | **LLM output was saved unvalidated**, and the human confirmed the deterministic mapper's proposal while the LLM's was saved. A hallucinated path (`../other_client/…`) or value map would be used. | Proposals are restricted to discovered files, existing columns and allowed values. The review shown is built from the final template, with disagreements flagged. Template paths must stay inside the client folder. Category values are checked on every row. `test_llm_proposal_is_validated_before_it_is_saved` |
| 3 | The review app listened on all network interfaces, with no login. | `.streamlit/config.toml` binds it to `127.0.0.1` and turns off usage statistics. A login is required before anyone but the operator uses it. |
| 4 | Outputs from a real client (audit log, LLM cache) could be committed by accident. | Both are gitignored. Committed reports are synthetic only. |
| 5 | The model file is a pickle (unpickling runs code). | Its sha256 is stored in the state at save time; `load` refuses a mismatching file. `test_model_file_is_only_unpickled_when_its_hash_matches` |
| 6 | Bank labels were rendered as Markdown in the app (links, images). | Rendered as plain text. |
| 7 | Exported CSV cells could start with `=`, `+`, `-` or `@` (formula injection in Excel). | Text cells are prefixed with `'`. `test_export_writes_posted_allocations_and_defuses_formulas` |
| 8 | `--client` was used unvalidated in file paths. | `paths.client_paths` accepts `[a-z0-9_-]` only and anchors paths on the repository root. |

**Checked and clean:** all SQL uses placeholders; YAML is always `safe_load`; no `eval`/`exec`/`shell=True`; no secrets in the tree or the history; the API key is read from the environment only and never logged.

## Code quality

- **Removed dead code:** unused attributes, functions, entity models, candidate signals, dataclass fields and parameters.
- **One place for each thing:**
  - client paths (`paths.py`);
  - charts (`plots.py`), which also broke an `evaluate` ↔ `experiments` import cycle;
  - money formatting;
  - month-end;
  - saving learned state (`save_state`).
- **Typed:** a `Scorer` protocol, a generic `structured_call`, and generic allocation helpers. `ty check` passes and is part of `make lint`, alongside a wider ruff rule set (`SIM`, `C4`, `ARG`, `RUF`, `PERF`, `T20`).
- **Tests:** 62 → 75, covering every fix above plus the export and the model file.
- **Generated data and pipeline outputs:** the refactor regenerated byte-identical data (357 files compared). The other reports and pipeline outputs changed only where noted above.

## Not fixed

- The LLM mapping path has not been run against the API (no key while building). Its validation is tested with fabricated proposals.
- The review app has no authentication. It is a single-operator localhost tool.
- `build_shares` nets a credit note into its invoice regardless of the credit's date. The synthetic data issues credits with their invoice, so the evaluation cannot see a future credit. Real data would need credits applied on their issue date.
- Five copies of the entity field list remain (entity model, loader, mapper, LLM prompt, mapping eval). Changing the prompt changes the LLM cache key, so this was left until the LLM path is run.
