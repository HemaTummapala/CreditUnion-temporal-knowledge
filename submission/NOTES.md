# NOTES — how this knowledge system works

**No API keys. No network. Python standard library only** (`requirements.txt` is intentionally empty).
Deterministic: the same question always gets the same answer. Ingest takes about 10 s for all five snapshots, and answers take about 10 ms each.

## 1. Keeping history

Each snapshot is the whole website on its date. Every page is parsed once (parses are cached by content
hash, because most pages don't change between crawls) into:

* **Rate rows**: every row of every rate table, with its caption ("Special Certificates"), column
  headers, cleaned cell values and the table's own *"Rates are effective as of MM/DD/YYYY"* date.
* **Text blocks**: heading-scoped chunks. FAQ items ("- [Question?](…)" followed by the answer) become their own blocks.
  "Handpicked for You" teaser sections on news pages are dropped.
* Extras: publish dates of news and blog posts, branch-hours blocks, fee-schedule line items, and "Label: value" facts
  (Phone, Routing Number).

Each snapshot gets its own BM25 index. `answer(q, as_of=D)` uses **the newest snapshot taken on or before D**.
With no `as_of`, it uses the latest snapshot (2026-09-28).

## 2. What "current" means (the rules)

1. **Newest snapshot first.** Older snapshots are only used for `as_of` questions and change history.
2. **Same fact, several pages → newest effective date wins**, then page authority:
   `/rates` and product pages > other site pages > news > blog > landing and promo pages > PDFs > calculators.
   Example: `/calculators/vehicle-affordability` and `/switch-and-earn` still show 5.34% (effective 1/1/2026),
   while `/rates` and `/personal/loans/auto-motorcycle-loans` show **5.39%** (effective 08/31/2026).
3. **Sanity rule:** a row whose dividend rate is higher than its own APY is internally inconsistent, so it loses ties.
4. **A product that only appears on landing, calculator or blog pages** ranks below one that is on the official rate sheet.
5. **News facts:** when near-duplicate press releases disagree (e.g. "23 branch office locations" vs "22"), the
   most recently *published* one wins.

## 3. Respecting qualifiers (tiers are not conflicts)

Rate questions are answered from **one table row**. The row is scored on:
term ("60-month"), model-year range (2025 is inside "2024-27"), balance or loan amount inside the tier range,
"Min. to Earn" thresholds (a $100,000 deposit gets the *jumbo* special), product words in the label or caption,
and personal vs business. Rows that carry qualifiers the question didn't ask for (jumbo, Roth, Boat & RV…) are penalised.
Copies of the same fact on several pages are grouped first, so rule 2 picks among them. When the question doesn't
choose a tier ("30-year mortgage rate"), every tied tier from the same page is listed.

## 4. Being honest

The system says it doesn't know (`confident=False`, no numbers in the text) when:
* the question names a product qualifier that no rate table mentions ("7/1 ARM", "construction loan");
* the amount asked about is outside every published tier ("jumbo mortgage over $1 million");
* a named branch has no published hours ("Mullan Avenue branch");
* a fee isn't in the fee schedule;
* retrieval finds nothing that mentions the question's key terms.

## 5. conflicts()

Every rate-table fact (canonical product + tier key, all columns merged) and a few boilerplate facts
(branch count, Forbes count) are tracked across **every page of every snapshot**. Any fact that has been shown
with more than one value becomes one `Conflict`. Each one lists the competing values, their sources, the
resolution (what `answer()` would say today) and a reason: the stale page that still shows the old value,
and/or the earlier snapshots. Formatting-only differences ("12.50% - 19.50%" vs "12.50% – 19.50%") are ignored.
Model-year tiers are only compared when their year ranges overlap.

## 6. Routing inside answer()

hours → branch address → credit-card list → fee schedule → link/URL → rate question (rows) → general retrieval
(BM25 + coverage + authority, near-duplicate collapse with newest-wins, best sentence across the top blocks).
Questions about change ("has the rate changed?") get the value history across snapshots.

## Known limitations

* Free-text answers are extractive (the best sentences), not generated, so a few open-ended questions get a
  partial answer.
* Practice item dup-088 expects 294 VTO users (from the 2024 article). The system answers 256, because the newer
  2025 article reports 256 for 2024 and "newest source wins".
