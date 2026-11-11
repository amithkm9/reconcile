# The first three weeks with a real client

What I would do if this ran on a real EHPAD group's books. Each week ends with a number the finance team can check, and nothing posts or sends without their approval until the numbers say it is safe.

## Week 1: onboard and replay history

**Ask for:**
1. **A year of bank exports and the lettering the team already did over the same period** (the reconciled ledger, or the billing software's lettrage export). This sets the auto threshold and is the only good training signal. Without it, we start on rules and certify on live review.
2. **The invoice and payer exports** as the billing software produces them today, not cleaned up.
3. **The ASH and APA statements:** format (PDF, CSV, paper?) and how late they arrive. Statements are the main path for bulk payments, so a PDF reader may be the first thing to build.
4. **The error tolerance:** how many misposts a month the team catches today, and what one costs to unwind. That sets the 99% target, which is a parameter, not a constant.
5. **The payment-method mix:** the share paying by direct debit, family transfer, département and guardian. This repo's mix is an assumption (`config/*.yaml`). The direct-debit share largely decides the headline automation rate.
6. **The reminder rules:** who approves reminders, and which cases are never chased (deceased residents, disputes, guardianship in progress).
7. **Hosting:** is HDS hosting required? May column profiles (never rows) be sent to an LLM at all, or does the mapping stay deterministic?

**Do:**
- Run `make onboard` on their exports. Every flagged inference gets confirmed with the person who knows the export.
- Replay the year: `evaluate.py` with their lettering as the truth.

**Deliver:** what would have been auto-posted, at what certified precision; the review queue per week; and a list of every line where we disagree with their lettering. Some of those will be their errors, and that list is the most convincing thing to show them.

## Week 2: shadow mode

**Do:**
- Run weekly on live exports with auto-posting off. Everything goes to the review queue with its suggestion, next to the clerks' normal work.
- Diff our suggestions against what they lettered, every day.
- Any new bank label layout raises an alert, and someone teaches the label parser the new layout. That is the failure this repo measured most (`docs/decisions.md#safety`).
- Go through every held and drafted reminder with the person who approves reminders: are the holds the right ones, and what do they cost?

**Deliver:** measured top-1 accuracy of the suggestions on their data, and the time per reviewed line.

## Week 3: switch on the safe part

**Do:**
- Turn on auto-posting for the match types that certified on their history. Expect direct debits by mandate reference and statement-backed bulk payments first.
- Reminders stay approve-before-send.
- Keep the money-balance check and the audit log on every run. Export the lettrage to the billing software, rather than asking the team to work in a second tool.

**Deliver:** clerk minutes per week and misposts, against the week-1 baseline. Then the decision to widen auto-posting (family transfers, partials) belongs to the team, based on their own numbers.
