# Glossary

## French care-home finance

| Term | Meaning |
|---|---|
| **EHPAD** | *Établissement d'hébergement pour personnes âgées dépendantes*: a care home for dependent elderly people. This project models a private non-profit group, which does its own receivables work. Public EHPAD collect through the public accountant (Hélios, *titres de recette*), a different process that is out of scope. |
| **Hébergement / dépendance / soins** | The three parts of an EHPAD price: accommodation (paid by the resident, family or ASH), dependency care (partly APA), and medical care (paid by health insurance directly to the home, not invoiced to residents). |
| **Lettrage** | Matching each incoming payment to the invoice line(s) it settles, so the receivables ledger shows what is still owed. This is the core job automated here. |
| **Relance** | A payment reminder (dunning letter). Here they are only ever drafted, never sent. |
| **ASH** | *Aide sociale à l'hébergement*: the département pays a resident's accommodation when they cannot. It arrives as one bulk transfer for many residents, with a nominative statement (*état nominatif*, *avis de paiement*). |
| **APA** | *Allocation personnalisée d'autonomie*: dependency allowance, often paid as a lump sum to the home (*dotation globale*). It settles no resident invoice, so it is classified as non-receivable income (`non_ar`). |
| **Obligés alimentaires** | Family members legally required to contribute (children, sons- and daughters-in-law). They often pay from their own accounts, sometimes under a married name. |
| **UDAF / mandataire judiciaire** | The guardian association or court-appointed guardian who pays for residents under protection, often several residents in one transfer. |
| **Prélèvement SEPA / RUM** | SEPA direct debit, and its mandate reference (*référence unique de mandat*), printed on the bank line. The easiest match type. |
| **Rejet de prélèvement (REJET PRLV)** | A returned direct debit: the money comes back out, and the invoice is open again. |
| **Compte d'attente** | Suspense account: cash received but not yet attributed (`unapplied` here). |
| **HDS** | *Hébergeur de données de santé*: the French certification required to host health data. Relevant to where a production system could run. |

## UK care-home finance

| Term | Meaning |
|---|---|
| **Self-funder** | A resident paying their own fees, usually by direct debit. |
| **Top-up** | A family payment covering the gap between the council rate and the home's fee. |
| **Local authority / council** | Pays for funded residents by BACS transfer, with a remittance advice listing who is covered. |
| **FNC / CHC** | NHS-Funded Nursing Care and Continuing Healthcare: NHS contributions paid to the home. |
| **BACS / Faster Payments** | UK bank transfer schemes. References are cut at 18 characters, which is why `UNPAID DD MCH00001` cannot name one mandate on its own. |

## Terms used in this project

| Term | Meaning |
|---|---|
| **Share** | One payer's part of one invoice (e.g. département 80%, family 20%). Payments are allocated to shares, not invoices. |
| **Candidate** | One possible allocation of a bank line: a set of edges (share, cents) plus an unapplied residual and the evidence for it. |
| **Exact allocation** | A line is correct only if its whole allocation equals the truth: a 40-invoice transfer with 39 invoices right counts as wrong. |
| **Auto / review / unapplied / non_ar** | The four outcomes for a bank line: posted automatically, queued for a clerk with a suggestion, left in suspense, or not a receivable at all. |
| **Certified threshold** | The lowest score at which the one-sided 95% Clopper–Pearson lower bound on precision, measured on history, is at least 99%. Lines scoring above it are auto-posted. |
| **Clopper–Pearson lower bound** | A conservative confidence bound on a proportion. 297 correct of 300 is "99%", but certifies only about 97.4%. |
| **Automation rate** | Share of receivable bank lines auto-posted. |
| **Review top-1 accuracy** | Within the review queue, how often the system's first suggestion is the right allocation, i.e. how often a clerk only has to click Accept. |
| **Suggestion recall** | Auto-posted correctly *or* right answer on top in review, over all receivable lines. It includes the auto-posts, so it is always higher than review top-1 accuracy. |
| **Shift** | A kind of messiness that never appears before the test months: a bank label format change, family members paying under married names, a new statement layout. |
| **New-format guard** | Lines in a bank label layout never seen in history are not auto-posted until reviewed lines of that layout certify ≥ 99%. |
| **Hold** | A reason not to draft a reminder: a pending review touching the invoice, unexplained recent cash that could be this payer's, a just-returned direct debit, or a deceased resident. |
| **Wrong reminder** | A draft for a payer share that, according to the ground truth, was already paid on the draft date. |
| **Missed reminder** | A payer share overdue and unpaid that was neither drafted nor held during the test quarter. |
