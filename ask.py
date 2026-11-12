"""Ask the knowledge system questions.

    python ask.py                                   -> interactive prompt
    python ask.py "question one" "question two"     -> answer these and exit
Prefix a question with 'as of YYYY-MM-DD:' to answer as the website stood on that date.
"""
import sys
from pathlib import Path

sys.path.insert(0, "harness")
sys.path.insert(0, "submission")
from run_eval import load_snapshots  # noqa: E402
import solution  # noqa: E402


def parse(q):
    """Accepts 'as of 2026-05-31: question', stray '>' characters, or a date written in the question."""
    import re
    q = q.strip().lstrip(">").strip()
    m = re.match(r"(?i)as of\s+(\d{4}-\d{2}-\d{2})\s*[:,-]?\s*(.+)", q)
    if m:
        return m.group(2).strip(), m.group(1)
    return q, None   # "... as of May 2026?" inside the question is handled by solution.py itself


def show(ks, raw):
    q, as_of = parse(raw)
    a = ks.answer(q, as_of=as_of)
    print(f"\nQ: {q}" + (f"   [as of {as_of}]" if as_of else ""))
    print(f"A: {a.text}")
    print(f"   source: {a.sources[0].url if a.sources else '-'} | confident: {a.confident}")


ks = solution.build()
for snap in load_snapshots(Path("snapshots")):
    ks.ingest(snap)

if len(sys.argv) > 1:
    for raw in sys.argv[1:]:
        show(ks, raw)
else:
    print("Ready. Type a question (or 'as of 2026-05-31: question'). Empty line to quit.")
    while True:
        raw = input("\n> ").strip()
        if not raw:
            break
        show(ks, raw)
