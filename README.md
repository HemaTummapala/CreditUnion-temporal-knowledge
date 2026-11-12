# Temporal knowledge system for a credit-union website

Answers member questions from five crawls (snapshots) of a credit union's website, using **the value that is
true now** (or on an `as_of` date). It picks the correct tier, cites its source, says *"I don't know"* when the
site doesn't publish something, and lists every place where the site contradicts itself.

* Solution: [`submission/solution.py`](submission/solution.py) (`build()` → `ingest` / `answer` / `conflicts`)
* How it decides what's current, plus limitations: [`submission/NOTES.md`](submission/NOTES.md)
* Pure Python standard library: **no API keys, no network, no packages**. Ingest takes about 10 s; each answer takes about 10 ms.

## Results on the public practice set (40 questions)

| | Starter | This solution |
|---|---|---|
| Accuracy | 0.09 | **0.91** |
| Old/wrong values repeated | several | **0** |
| Consistency | – | 1.0 |
| Speed (p95) | – | 0.02 s |

## Running it

The website snapshots are **not included** in this repository; they belong to the hackathon kit.
Copy the kit's `snapshots_zip/` folder into the repo root, then:

```bash
# official scorer (unpacks the zips into snapshots/ on first run)
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl

# behaviour tests (23 tests: stale traps, tiers, as_of, refusals, consistency, speed, conflicts)
python -m unittest discover -s tests -v

# ask your own questions
python ask.py
python ask.py "What is the 12-month certificate rate?" "as of 2026-05-31: What was the 10-month jumbo certificate APY?"
```

## Layout

```
submission/   solution.py, NOTES.md, requirements.txt   <- the graded folder
tests/        behaviour tests
ask.py        small CLI for trying questions
harness/      the organisers' scorer (unchanged)
eval/         public practice questions
starter/      the naive starter (unchanged)
```
