"""
Temporal knowledge system for the credit-union website snapshots.

Pure standard library: no API keys, no network, deterministic answers.

Pipeline
--------
ingest(snapshot)
    Every page is parsed (parse results are cached by content hash, because most pages
    do not change between crawls) into:
      * RateRow  - one row of a rate table, with its caption, column headers, cleaned cell
                   values and the table's "Rates are effective as of ..." date
      * Block    - a heading-scoped text chunk (paragraphs / short-line groups)
    Each snapshot gets its own BM25 index over its blocks. A snapshot is the whole site on
    that date, so "as of D" simply means "the newest snapshot taken on or before D".

answer(question, as_of)
    1. choose the snapshot for as_of
    2. route: hours / branch address / credit-card list / rate question / link question /
       general question
    3. rate questions are answered from table ROWS: qualifiers in the question (term,
       model year, balance or loan amount, jumbo, IRA, business ...) are matched against
       the row label, rows carrying extra qualifiers are penalised, and among equally good
       rows the one from the table with the NEWEST effective date wins, then the most
       authoritative page (rates page / product page > landing page > calculator > blog).
    4. if the question names a product qualifier that no rate table mentions
       (e.g. "7/1 ARM", "construction loan") the system says it is not listed.
    5. general questions use BM25 with page-authority weighting and a recency bonus for
       dated news/blog pages, then return the best sentences of the best block.

conflicts()
    Facts (rate-table cells keyed by product + tier + column, plus a few boilerplate
    facts such as the branch count) are tracked across every page of every snapshot.
    Any fact seen with more than one value is a conflict; the resolution is the value
    from the newest snapshot, newest effective/publish date and most authoritative page.
"""


import hashlib
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                            if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

# --------------------------------------------------------------------------------------
# text utilities
# --------------------------------------------------------------------------------------
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
MONTH_ABBR = {k[:3]: v for k, v in MONTHS.items()}

STOP = set("""a an the of to in on for and or is are was were be been being do does did i me my
we our you your it its this that these those what which who whom how when where why can could
would should will shall may might must there their they them at by with from as about into
than then so if any all some much many please tell know want like get give show provide current
currently right now today dupaco dupacos dupaco's s us also just need find let have has had
whats what's i'm im up""".split())

# words that describe *what kind of number* is wanted, not *which product*
GENERIC = set("""rate rates apr apy interest dividend yield annual percentage earn pay pays paid
charge charges cost costs fee price offer offers program account accounts loan loans product
products option options standard regular normal typical general credit union member members
union's high highest low lowest best tier tiers balance much new open opening get current
latest number value amount percent month months year years term terms certificate cd""".split())

SYN = {
    "vehicle": ["auto"], "car": ["auto"], "truck": ["auto"], "cars": ["auto"],
    "automobile": ["auto"], "motorcycle": ["auto", "motorcycle"],
    "cd": ["certificate"], "cds": ["certificate"], "certificates": ["certificate"],
    "mortgage": ["home", "conventional"], "mortgages": ["home", "conventional"],
    "heloc": ["home", "equity", "line"], "boat": ["boat"], "rv": ["rv"],
    "visa": ["visa"], "card": ["card", "visa"], "cards": ["card", "visa"],
    "savings": ["saving"], "hsa": ["health"], "ira": ["ira"],
}

# row-label words that are not tier qualifiers (do not penalise a row for carrying them)
ROW_NEUTRAL = set("""rate rates apr apy month months year years term less more account type loan loans
program annual percentage low as and or the of up to plus standard""".split())

# nouns that close a product phrase: "<qualifiers> loan", "<qualifiers> certificate" ...
PRODUCT_HEAD = r"(?:loan|loans|mortgage|mortgages|certificate|certificates|cd|cds|account|card|cards|line of credit|savings|checking|ira|heloc|arm)"

# product nouns in a question -> words one of which must appear around the matching row
PRODUCT_FAMILY = {
    "mortgage": {"home", "mortgage", "conventional"}, "heloc": {"heloc", "equity"},
    "home equity": {"equity", "heloc"}, "certificate": {"certificate"}, "cd": {"certificate"},
    "ira": {"ira"}, "checking": {"checking"}, "money market": {"market"}, "credit card": {"visa", "card"},
    "visa": {"visa"}, "auto": {"auto"}, "car loan": {"auto"}, "personal loan": {"personal"},
    "boat": {"boat"}, "rv": {"rv"}, "student": {"student"}, "health savings": {"health"}, "hsa": {"health"},
    "home improvement": {"improvement"},
}

WORD_RE = re.compile(r"\d+/\d+|[a-z0-9]+")


def stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"(?<=[a-z])\$(?=[a-z])", "s", re.sub(r"'s\b|'", "", t))      # "dupaco's" -> "dupaco", "home$tart" -> "homestart"
    return [stem(w) for w in WORD_RE.findall(t)]


def index_tokens(text: str) -> list[str]:
    """tokens plus 6-char prefixes of long words (recognition ~ recognized)."""
    out = tokens(text)
    return out + [w[:6] + "~" for w in out if len(w) >= 7 and w.isalpha()]


STOP |= {stem(w) for w in list(STOP)}        # "does" -> "doe", "has" -> "ha" ...


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOP]


IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
DATA_URI_RE = re.compile(r"data:[a-z/+]+;?[^\s)]*", re.I)
FOOTNOTE_TAIL = re.compile(r"(\d(?:\.\d+)?%|APR|APY|\.\d\d)\s*[¹²³\d]{1,2}\b")


def clean_inline(s: str) -> str:
    s = IMG_RE.sub("", s)
    s = DATA_URI_RE.sub("", s)
    s = LINK_RE.sub(lambda m: m.group(1) + (f" <{m.group(2)}>" if m.group(2).startswith("http") else ""), s)
    s = s.replace("**", "").replace("\\*", "*").replace("\\#", "#").replace("\\\\r\\\\n", " ")
    s = s.replace("\\r\\n", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def clean_cell(s: str) -> str:
    s = clean_inline(s)
    # "0.15%1" -> "0.15%", "5.39% APR1" -> "5.39% APR", "4.10%2" -> "4.10%"
    s = re.sub(r"(%|APR|APY)\s*[¹²³]|(%|APR|APY)(\d)\b", lambda m: (m.group(1) or m.group(2)), s)
    s = re.sub(r"%\s+\d\b(?!\s*(?:%|mo|month|year))", "%", s)
    return s.strip()


DATE_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
DATE_TXT_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)


def parse_num_date(s: str) -> Optional[date]:
    m = DATE_NUM_RE.search(s or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_txt_date(s: str) -> Optional[date]:
    m = DATE_TXT_RE.search(s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTH_ABBR[m.group(1).lower()[:3]], int(m.group(2)))
    except (ValueError, KeyError):
        return None


def iso(d: Optional[date]) -> str:
    return d.isoformat() if d else ""


MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s?(?:k|thousand|million|m)\b)?", re.I)


def money_values(text: str) -> list[float]:
    out = []
    for m in MONEY_RE.finditer(text or ""):
        v = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        suf = (m.group(3) or "").strip().lower()
        if suf in ("k", "thousand"):
            v *= 1000
        elif suf in ("m", "million"):
            v *= 1_000_000
        out.append(v)
    return out


TERM_RE = re.compile(r"\b(\d{1,3})\s*[- ]?\s*(month|mo|year|yr)s?\b", re.I)


def terms_in(text: str) -> set[tuple[int, str]]:
    out = set()
    for m in TERM_RE.finditer(text or ""):
        unit = "m" if m.group(2).lower().startswith("mo") else "y"
        out.add((int(m.group(1)), unit))
    return out


MODEL_RANGE_RE = re.compile(r"\b((?:19|20)\d{2})\s*[-–]\s*(\d{2,4})\s+model year", re.I)
MODEL_OLDER_RE = re.compile(r"\b((?:19|20)\d{2})\s+model year\s*\(and older\)", re.I)
MODEL_SINGLE_RE = re.compile(r"\b((?:19|20)\d{2})\s+model year", re.I)


def model_years(text: str) -> Optional[tuple[int, int]]:
    m = MODEL_RANGE_RE.search(text or "")
    if m:
        a, b = int(m.group(1)), m.group(2)
        b = int(b) if len(b) == 4 else int(str(a)[:2] + b)
        return a, b
    m = MODEL_OLDER_RE.search(text or "")
    if m:
        return 1900, int(m.group(1))
    m = MODEL_SINGLE_RE.search(text or "")
    if m:
        return int(m.group(1)), int(m.group(1))
    return None


def balance_range(label: str) -> Optional[tuple[float, float]]:
    vals = money_values(label)
    if not vals:
        return None
    low = label.lower()
    if len(vals) >= 2:
        return vals[0], vals[1]
    if "+" in label or "and up" in low or "or more" in low or "and over" in low:
        return vals[0], float("inf")
    if "or less" in low or "up to" in low or "under" in low:
        return 0.0, vals[0]
    return vals[0], vals[0]


PCT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")


# --------------------------------------------------------------------------------------
# page model
# --------------------------------------------------------------------------------------
def page_kind(url: str) -> str:
    p = url.lower().split("dupaco.com", 1)[-1]
    if p in ("/rates", "/business-rates") or p.startswith("/rates") or p.startswith("/business-rates"):
        return "rates"
    if "/newsroom" in p:
        return "news"
    if "/learn/" in p or "/dupacodaily/" in p or "/resources/" in p or p.startswith("/resources") or re.match(r"^/20\d\d/", p) or "/blog" in p:
        return "blog"
    if "/calculators/" in p or "calculator" in p:
        return "calculator"
    if "/hubfs/" in p:
        return "file"
    if p.endswith("-lp") or "-lp?" in p or p in ("/switch-and-earn", "/earn-more", "/mortgage-refi",
                                                  "/homebuying-guidance", "/money-makeover",
                                                  "/offer-expired", "/makethemost-lp"):
        return "landing"
    if p.startswith("/personal") or p.startswith("/business") or p.startswith("/about") \
            or p in ("/contact", "/membership", "/checking", "/loans", "/personal-loans", "") \
            or p.startswith("/savings-account") or p.startswith("/online-and-mobile-banking") \
            or p.startswith("/connect") or p.startswith("/shine") or p.startswith("/careers"):
        return "product"
    return "other"


AUTHORITY = {"rates": 1.0, "product": 0.92, "other": 0.75, "landing": 0.5, "news": 0.55,
             "blog": 0.45, "file": 0.4, "calculator": 0.3}


def skip_url(url: str) -> bool:
    u = url.lower()
    return ("privacy-policy?" in u and "campaignid" in u) or "subscription-preferences" in u


@dataclass
class RateRow:
    url: str
    kind: str
    caption: str            # e.g. "Certificates / Special Certificates"
    headers: list
    cells: list
    label: str              # the non-numeric descriptive cells joined
    eff: Optional[date]
    snapshot_id: str = ""
    toks: set = field(default_factory=set)
    ltoks: set = field(default_factory=set)
    ctoks: set = field(default_factory=set)

    def rate_cells(self):
        return [(h, c) for h, c in zip(self.headers, self.cells) if PCT_RE.search(c)]

    def render(self) -> str:
        parts = []
        for h, c in zip(self.headers, self.cells):
            if c and c != self.label and c not in self.label and c not in ("—", "-"):
                hh = re.sub(r"\(As Low As\)", "", h).strip()
                hh = re.sub(r"(?<=[\)A-Za-z])[\s\d*¹²³]+$", "", hh).strip()
                hh = {"Annual Percentage Yield (APY)": "APY", "Annual Percentage Rate": "APR",
                      "*Payment Example": "payment example"}.get(hh, hh)
                parts.append(f"{hh} {c}")
        return f"{self.head()}: " + ", ".join(parts)

    def head(self) -> str:
        cap = self.caption.split(" / ")[-1] if self.caption else ""
        return self.label if cap and cap.lower() in self.label.lower() else (f"{cap} – {self.label}" if cap else self.label)


@dataclass
class Block:
    url: str
    kind: str
    heading: str
    text: str
    pub: Optional[date]
    title: str
    snapshot_id: str = ""


@dataclass
class ParsedPage:
    url: str
    title: str
    kind: str
    pub: Optional[date]
    blocks: list
    rows: list
    hours: list             # list of (text, pub)
    facts: list             # list of (key, topic, value)
    fees: list = field(default_factory=list)     # fee-schedule line items: (line, effective date)
    labels: list = field(default_factory=list)   # "Phone: 800-..." style facts: (label, value)


LOBBY_RE = re.compile(r"\blobby\b", re.I)
DAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def parse_page(url: str, title: str, md: str) -> ParsedPage:
    kind = page_kind(url)
    lines = md.split("\n")
    pub = None
    if kind in ("news", "blog"):
        head = "\n".join(lines[:12])
        pub = parse_txt_date(head)
        if not pub:
            m = re.search(r"/(20\d\d)/(\d\d)/", url)
            if m:
                pub = date(int(m.group(1)), int(m.group(2)), 1)

    blocks, rows = [], []
    heading_stack: list[str] = []
    buf: list[str] = []
    recent_short: list[str] = []

    def flush():
        if not buf:
            return
        chunk, size = [], 0
        for para in buf:
            if size + len(para) > 700 and chunk:
                blocks.append(Block(url, kind, " > ".join(heading_stack[-2:]), "\n".join(chunk), pub, title))
                chunk, size = [], 0
            chunk.append(para)
            size += len(para)
        if chunk:
            blocks.append(Block(url, kind, " > ".join(heading_stack[-2:]), "\n".join(chunk), pub, title))
        buf.clear()

    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue
        if line.startswith("|") and i + 1 < n and re.match(r"^\|\s*-{3}", lines[i + 1].strip()):
            flush()
            headers = [clean_cell(c) for c in line.strip("|").split("|")]
            j = i + 2
            body = []
            while j < n and lines[j].strip().startswith("|"):
                body.append([clean_cell(c) for c in lines[j].strip().strip("|").split("|")])
                j += 1
            # effective date: first lines after the table
            eff = None
            k, seen = j, 0
            while k < n and seen < 4:
                t = lines[k].strip()
                if t:
                    seen += 1
                    if "effective" in t.lower():
                        d = parse_num_date(t) or parse_txt_date(t)
                        if d and (eff is None or d > eff):
                            eff = d
                k += 1
            caption = " / ".join(recent_short[-2:])
            is_rate_table = any(PCT_RE.search(" ".join(r)) for r in body) and \
                not any(h.lower() in ("category",) for h in headers[:1])
            for r in body:
                if len(r) < len(headers):
                    r = r + [""] * (len(headers) - len(r))
                label = " | ".join(c for c in r if c and not PCT_RE.search(c)
                                   and not re.fullmatch(r"[\$\d.,\s—–-]+", c))
                # keep balance / amount tiers in the label (they qualify the row)
                tier = [c for c in r if re.search(r"\$[\d,]+", c) and ("–" in c or "-" in c or "+" in c
                                                                      or "less" in c.lower())]
                if tier and not any(t in label for t in tier):
                    label = (label + " | " + " | ".join(tier)).strip(" |")
                h0 = headers[0] if headers else ""
                if h0 and not re.search(r"(?i)type|term|program|balance|product|amount|certificate|category|tier|^loan|^card|^account|^rate", h0) \
                        and h0.lower() not in label.lower():
                    label = f"{h0} | {label}"
                row_line = " | ".join(r)
                if is_rate_table and PCT_RE.search(row_line) and label:
                    rr = RateRow(url, kind, caption, headers, r[:len(headers)], label, eff)
                    rr.toks = set(tokens(label + " " + caption + " " + " ".join(heading_stack[-2:])))
                    rr.ltoks = set(tokens(label))
                    rr.ctoks = set(tokens(caption.split(" / ")[-1] if caption else "")) - rr.ltoks
                    rows.append(rr)
                    text = rr.render()
                else:
                    text = " | ".join(f"{h}: {c}" for h, c in zip(headers, r) if c)
                if not re.match(r"^\d+$", r[0] or "") and "data points" not in text:
                    blocks.append(Block(url, kind, " > ".join(heading_stack[-2:] + [caption]), text, pub, title))
            recent_short = []
            i = j
            continue
        if line.startswith("#"):
            flush()
            h = clean_inline(line.lstrip("#").strip())
            if kind in ("news", "blog") and re.match(r"(?i)handpicked for you|related (articles|posts|stories)|you might also like", h):
                break
            level = len(line) - len(line.lstrip("#"))
            heading_stack = heading_stack[:max(0, level - 1)] + [h]
            recent_short.append(h)
            i += 1
            continue
        faq = re.match(r"^[-*]\s*\[([^\]]{8,200}\?)\]\(", line)
        if faq:
            flush()
            base = [h for h in heading_stack if not h.endswith("?")]
            heading_stack = base + [clean_inline(faq.group(1))]
            buf.append(clean_inline(faq.group(1)))
            i += 1
            continue
        txt = clean_inline(line)
        if not txt or "data points:" in txt or txt.startswith("Point ") or len(re.sub(r"[^a-zA-Z]", "", txt)) < 2:
            i += 1
            continue
        if len(txt) < 60 and not txt.startswith(("[", "<", "_", "*", "-")) and "effective" not in txt.lower() \
                and not txt.endswith((".", ":")):
            recent_short.append(txt)
            recent_short = recent_short[-4:]
        buf.append(txt)
        i += 1
    flush()

    # ---- branch hours blocks (press releases list "Lobby ... Drive-up ...")
    hours = []
    full = "\n".join(clean_inline(l) for l in lines)
    for m in re.finditer(r"(?:branch hours are:?|hours are:?)\s*(.{20,400}?)(?:ABOUT DUPACO|\n#|$)", full, re.I | re.S):
        seg = re.sub(r"\s*\n\s*", "\n", m.group(1)).strip()
        if LOBBY_RE.search(seg) and DAY_RE.search(seg):
            pre = full[max(0, m.start() - 600):m.start()]
            hours.append((seg, pre))

    # ---- boilerplate facts (for conflicts + news-style questions)
    facts = []
    for m in re.finditer(r"(?<![\d,.$])\b(\d{1,2})\s+(?:branch office locations|branch offices|branches|branch locations)\b", full, re.I):
        facts.append(("branch_count", "number of Dupaco branch office locations", m.group(1)))
    for m in re.finditer(r"\bThis was the (\w+) time Forbes", full):
        facts.append(("forbes_count", "number of times Forbes recognized Dupaco (Best-in-State credit union)", m.group(1)))
    fees, labels = [], []
    if "fee" in url.lower() and kind == "file":
        eff = parse_txt_date("\n".join(lines[:15]))
        for l in lines:
            t = clean_inline(l)
            if 8 < len(t) < 200 and re.search(r"\$\s?\d|\bfree\b|\d%", t, re.I) and not t.startswith("*"):
                fees.append((t, eff))
    for l in lines:
        t = clean_inline(l)
        m = re.match(r"^([A-Z][A-Za-z .]{2,30}):\s*(\S.{1,80})$", t)
        if m and re.search(r"\d", m.group(2)):
            labels.append((m.group(1).strip(), m.group(2).strip()))
    return ParsedPage(url, title, kind, pub, blocks, rows, hours, facts, fees, labels)


# --------------------------------------------------------------------------------------
# BM25 index (one per snapshot)
# --------------------------------------------------------------------------------------
class BM25:
    def __init__(self, docs: list[list[str]], k1=1.4, b=0.6):
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.len = [len(d) for d in docs]
        self.avg = sum(self.len) / max(1, self.N)
        self.post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(docs):
            for t, c in Counter(d).items():
                self.post[t].append((i, c))
        self.idf = {t: math.log(1 + (self.N - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def search(self, q: list[str], weights: Optional[dict] = None) -> dict[int, float]:
        sc: dict[int, float] = defaultdict(float)
        for t in set(q):
            if t not in self.post:
                continue
            w = self.idf[t] * (weights.get(t, 1.0) if weights else 1.0)
            for i, c in self.post[t]:
                dl = self.len[i]
                sc[i] += w * c * (self.k1 + 1) / (c + self.k1 * (1 - self.b + self.b * dl / self.avg))
        return sc


class SnapIndex:
    def __init__(self, snapshot_id: str, taken_on: str, pages: list[ParsedPage]):
        self.snapshot_id, self.taken_on = snapshot_id, taken_on
        self.pages = {p.url: p for p in pages}
        self.blocks: list[Block] = []
        self.rows: list[RateRow] = []
        self.hours: list[tuple[str, str, str, Optional[date]]] = []
        for p in pages:
            for b in p.blocks:
                self.blocks.append(b)
            for r in p.rows:
                self.rows.append(r)
            for seg, pre in p.hours:
                self.hours.append((seg, pre, p.url, p.pub))
        docs = []
        for b in self.blocks:
            t = index_tokens(b.text) + index_tokens(b.heading) * 2 + index_tokens(b.title)
            docs.append(t)
        self.bm25 = BM25(docs)
        self.blocks_by_url = defaultdict(list)
        for b in self.blocks:
            self.blocks_by_url[b.url].append(b)
        self.doc_sets = [set(d) for d in docs]
        self.url_toks: dict = {}
        self.vocab = set(self.bm25.post)
        self.row_vocab = set()
        for r in self.rows:
            self.row_vocab |= r.toks


# --------------------------------------------------------------------------------------
# the knowledge system
# --------------------------------------------------------------------------------------
RATE_WORDS = {"rate", "apr", "apy", "dividend", "interest", "yield", "earn", "pay"}
HOURS_WORDS = {"hour", "open", "close", "closing", "opening"}
LINK_WORDS = {"link", "url", "website", "web", "page", "site", "locator"}
CONTACT_US = "please contact Dupaco (call the member line or visit a branch)"


class TemporalKnowledge:
    def __init__(self):
        self.snaps: list[SnapIndex] = []
        self._parse_cache: dict[str, ParsedPage] = {}
        self._answer_cache: dict[tuple, Answer] = {}
        self._idf_cache: dict = {}

    # ---------------------------------------------------------------- ingest
    def ingest(self, snapshot: Snapshot) -> None:
        pages = []
        for p in snapshot.pages:
            if skip_url(p.url):
                continue
            h = hashlib.md5((p.url + "\0" + p.markdown).encode("utf-8", "ignore")).hexdigest()
            pp = self._parse_cache.get(h)
            if pp is None:
                pp = parse_page(p.url, p.title, p.markdown)
                self._parse_cache[h] = pp
            pages.append(pp)
        idx = SnapIndex(snapshot.snapshot_id, snapshot.taken_on, pages)
        for b in idx.blocks:
            b.snapshot_id = b.snapshot_id or snapshot.snapshot_id
        self.snaps.append(idx)
        self.snaps.sort(key=lambda s: s.taken_on)
        self._idf_cache = self.snaps[-1].bm25.idf
        self._answer_cache.clear()

    def _date_in_question(self, q: str) -> Optional[str]:
        """'... as of May 2026' / 'as of May 19, 2026' / 'as of 2026-05-31' written inside the question.
        Only dates before the newest snapshot change anything (later dates = current)."""
        m = re.search(r"\bas of\s+(\d{4}-\d{2}-\d{2})", q, re.I)
        d = None
        if m:
            d = m.group(1)
        else:
            m = re.search(r"\bas of\s+(?:the end of\s+|early\s+|late\s+|mid[- ])?"
                          r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})", q, re.I)
            if m:
                mo, yr = MONTH_ABBR[m.group(1).lower()[:3]], int(m.group(3))
                if m.group(2):
                    day = int(m.group(2))
                else:   # "as of May 2026" -> how the site stood at the end of that month
                    nxt = date(yr + (mo == 12), mo % 12 + 1, 1)
                    day = (nxt - date.resolution).day
                try:
                    d = date(yr, mo, day).isoformat()
                except ValueError:
                    d = None
        if d and self.snaps and d < self.snaps[-1].taken_on:
            return d
        return None

    def _snap(self, as_of: Optional[str]) -> SnapIndex:
        if not as_of:
            return self.snaps[-1]
        target = str(as_of)[:10]
        chosen = self.snaps[0]
        for s in self.snaps:
            if s.taken_on <= target:
                chosen = s
        return chosen

    # ---------------------------------------------------------------- answer
    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        if not self.snaps:
            return Answer(text="I don't know - no website data has been loaded.", confident=False)
        if not as_of:
            as_of = self._date_in_question(question)
        key = (question.strip().lower(), as_of)
        if key in self._answer_cache:
            return self._answer_cache[key]
        snap = self._snap(as_of)
        try:
            ans = self._route(question, snap, as_of)
        except Exception as e:  # never crash the harness
            ans = Answer(text=f"I'm not able to confirm that from the website; {CONTACT_US}.",
                         confident=False, conflict_note=f"internal error: {type(e).__name__}")
        self._answer_cache[key] = ans
        return ans

    def _route(self, q: str, snap: SnapIndex, as_of: Optional[str]) -> Answer:
        ql = q.lower()
        qt = set(tokens(q))
        if ("hour" in qt or "lobby" in qt or "drive" in qt) and ("hour" in qt or "open" in qt) \
                and "24" not in ql:
            return self._hours(q, snap)
        if "address" in qt or re.search(r"\bwhere is the .* branch\b", ql):
            a = self._address(q, snap)
            if a:
                return a
        if ("credit card" in ql or "visa" in ql) and qt & {"rate", "apr", "offer", "interest"} \
                and not qt & {"platinum", "reward", "business", "intro", "introductory", "transfer"}:
            a = self._all_cards(q, snap)
            if a:
                return a
        if qt & LINK_WORDS or "what link" in ql or "which link" in ql or \
                re.search(r"\b(join|apply|become a member)\b.*\bonline\b|\bonline\b.*\b(join|apply)\b", ql):
            a = self._link(q, snap)
            if a:
                return a
        if re.search(r"\bfees?\b|\bcharge for\b|\bcosts?\b|how much (is|does) (it|a|the)\b", ql) and \
                not re.search(r"\b(apr|apy|interest rate|dividend)\b", ql):
            a = self._fee(q, snap)
            if a:
                return a
        car_year = re.search(r"\b(car|vehicle|auto|truck|motorcycle)\b", ql) and re.search(r"\b(19|20)\d\d\b", ql)
        if qt & RATE_WORDS or re.search(r"\b(apy|apr)\b", ql) or car_year or \
                re.search(r"\bminimum (to open|deposit|balance)|\bmin(\.|imum)? to (open|earn)\b", ql):
            a = self._rate(q, snap, as_of)
            if a:
                return a
        return self._general(q, snap, as_of)

    # ---------------------------------------------------------------- rate questions
    def _row_score(self, r: RateRow, q: str, qtok: set, qterms, qyear, qmoney, business_q: bool) -> float:
        s = 0.0
        ltoks = r.ltoks
        for t in qtok:
            if t in ltoks:
                s += 1.0 if t not in GENERIC else 0.3
            elif t in r.ctoks:
                s += 0.9 if t not in GENERIC else 0.2
            elif t in r.toks:
                s += 0.4 if t not in GENERIC else 0.1
        # specific words in the table caption that the question did not ask for ("Boat & RV")
        cextra = [t for t in r.ctoks if t.isalpha() and t not in qtok and t not in STOP and t not in GENERIC
                  and t not in ROW_NEUTRAL and len(t) > 2 and t not in ltoks]
        s -= 0.3 * len(cextra)
        lab = r.label + " " + r.caption
        # term qualifiers (60-month, 30-year ...)
        rterms = terms_in(r.label)
        if qterms:
            if rterms:
                s += 3.0 if (qterms & rterms) else -5.0
            else:
                s -= 0.5
        # model year
        my = model_years(r.label)
        if qyear and my:
            s += 3.0 if any(my[0] <= y <= my[1] for y in qyear) else -5.0
        # balance / loan amount
        br = balance_range(r.label)
        if qmoney and br:
            inside = [v for v in qmoney if br[0] - 0.5 <= v <= br[1] + 0.5]
            s += 3.0 if inside else -4.0
        # extra qualifiers on the row that the question did not ask for
        extra = [t for t in set(tokens(r.label)) if t.isalpha() and t not in qtok and t not in STOP
                 and t not in ROW_NEUTRAL and len(t) > 2]
        s -= 0.45 * len(extra)
        # "Min. to Earn" thresholds: prefer the highest minimum the amount qualifies for (jumbo vs standard)
        if qmoney and not br:
            mins = [money_values(c) for h, c in zip(r.headers, r.cells) if "min" in h.lower()]
            mins = [m[0] for m in mins if m]
            if mins:
                amt = max(qmoney)
                lo = max(mins)
                s += (1.0 + 1.0 * min(1.0, lo / amt)) if amt >= lo else -3.0
        # personal vs business
        is_bus = "business" in r.url or "business" in lab.lower()
        if is_bus and not business_q:
            s -= 2.0
        if business_q and not is_bus:
            s -= 1.0
        return s

    def _expand(self, q: str) -> set:
        base = set(content_tokens(q))
        out = set(base)
        for t in base:
            for s in SYN.get(t, []):
                out.add(stem(s))
        return out

    def _rate(self, q: str, snap: SnapIndex, as_of) -> Optional[Answer]:
        ql = q.lower()
        qtok = self._expand(q)
        qterms = terms_in(q)
        qyear = [int(y) for y in re.findall(r"\b(19[89]\d|20[0-3]\d)\b", q)
                 if not re.search(rf"{y}\s*[- ]?\s*(month|year)", q)]
        if as_of or re.search(r"\bas of\b|\bin (january|february|march|april|may|june|july|august|september|october|november|december)\b", ql):
            # a year in an "as of" phrase is a date, not a model year
            qyear = [y for y in qyear if "model" in ql or "vehicle" in ql or "car" in ql]
        qmoney = [v for v in money_values(q)]
        business_q = "business" in ql
        if not snap.rows:
            return None
        scored = []
        for r in snap.rows:
            sc = self._row_score(r, q, qtok, qterms, qyear, qmoney, business_q)
            scored.append((sc, r))
        scored.sort(key=lambda x: -x[0])
        top = scored[0][0]
        # does the question name a product qualifier that no rate table mentions?
        # ("7/1 adjustable-rate mortgage", "construction loan") -> honest "not listed"
        phrase_words = []
        for m in re.finditer(r"((?:[\w/$'’-]+\s+){0,3})" + PRODUCT_HEAD + r"\b", ql):
            phrase_words += content_tokens(m.group(1))
        phrase_words += [t for t in tokens(q) if "/" in t]
        unknown = []
        for t in dict.fromkeys(phrase_words):
            if t in GENERIC or t in STOP or t.isdigit() or len(t) < 2:
                continue
            alts = {t} | {stem(x) for x in SYN.get(t, [])}
            if not (alts & snap.row_vocab):
                unknown.append(t)
        if unknown and (top < 4 or any("/" in t for t in unknown)):
            what = " ".join(unknown)
            return Answer(
                text=(f"Dupaco's website doesn't publish a rate for that ({what}) - it isn't listed on the "
                      f"rate pages. For a quote, {CONTACT_US}."),
                sources=[Source(url="https://www.dupaco.com/rates", snapshot_id=snap.snapshot_id)],
                confident=False)
        if top <= 0.0:
            return None
        spec_q = {t for t in qtok if t not in GENERIC}

        def real(r):
            return bool(spec_q & r.toks) or bool(qterms & terms_in(r.label))
        scored = [(sc, r) for sc, r in scored if sc > 0 and real(r)]
        # the row must belong to the product family the question names (mortgage, certificate, card ...)
        fams = [alts for w, alts in PRODUCT_FAMILY.items() if re.search(rf"\b{w}", ql)]
        if fams:
            def fam_ok(r):
                space = r.toks | set(tokens(r.url.split("dupaco.com", 1)[-1]))
                return all(a & space for a in fams)
            filt = [(sc, r) for sc, r in scored if fam_ok(r)]
            if filt:
                scored = filt
        if not scored:
            return None
        top = scored[0][0]
        # group copies of the same fact (same product row on several pages / stale copies)
        groups: dict = defaultdict(list)
        for sc, r in scored:
            groups[self._fact_key(r)].append((sc, r))

        def pick_key(r: RateRow):
            return (not self._inconsistent(r), r.eff or date(1900, 1, 1), AUTHORITY.get(r.kind, 0.5),
                    r.url.endswith("/rates"))
        greps = []
        for k, g in groups.items():
            gmax = max(sc for sc, _ in g)
            members = [r for sc, r in g if sc >= gmax - 1.0]
            # products that only appear on promo/landing/calculator/blog pages rank below the official sheet
            if not any(r.kind in ("rates", "product") for r in members):
                gmax -= 0.6
            greps.append((gmax, max(members, key=pick_key), members))
        greps.sort(key=lambda x: -x[0])
        top = greps[0][0]
        tied = [g for g in greps if g[0] >= top - 0.3]
        best = max(tied[:1] + [g for g in tied if g[0] >= top - 1e-9], key=lambda g: pick_key(g[1]))[1]
        cands = [r for g in tied for r in g[2]]
        if qmoney:
            br = balance_range(best.label)
            fam_tiers = [balance_range(r.label) for r in snap.rows
                         if r.url == best.url and r.caption == best.caption and balance_range(r.label)]
            uncovered = fam_tiers and not any(lo - 0.5 <= v <= hi + 0.5 for lo, hi in fam_tiers for v in qmoney)
            if (br and not any(br[0] - 0.5 <= v <= br[1] + 0.5 for v in qmoney)) or uncovered:
                return Answer(text=("Dupaco's website doesn't list a rate for that amount - the published tiers "
                                    f"don't cover it. For a quote, {CONTACT_US}."),
                              sources=[Source(url=best.url, snapshot_id=snap.snapshot_id)], confident=False)
        # rows of the same page that are tied (the question does not pick a tier)
        same = []
        for g in tied:
            on_page = [r for r in g[2] if r.url == best.url]
            if on_page:
                same.append(on_page[0])
        seen, chosen = set(), []
        for r in [best] + same:
            k = (r.caption, r.label)
            if k in seen:
                continue
            seen.add(k)
            chosen.append(r)
        if re.search(r"\b(highest|best|top|max(imum)?)\b", ql) and len(chosen) > 1:
            chosen = [max(chosen, key=lambda r: max((float(re.sub(r'[^\d.]', '', c) or 0)
                                                    for _, c in r.rate_cells()), default=0))]
        elif re.search(r"\b(lowest|minimum|cheapest)\b", ql) and len(chosen) > 1:
            chosen = [min(chosen, key=lambda r: min((float(re.sub(r'[^\d.]', '', c) or 99)
                                                    for _, c in r.rate_cells()), default=99))]
        chosen = chosen[:12]
        if re.search(r"\b(chang|increas|decreas|go(ne)? up|went (up|down)|rais|lower(ed)?|drop|differ|used to|previous|before|since|histor|over time|trend)", ql) \
                and not as_of:
            hist = self._history(best, pick_key)
            if hist:
                return Answer(text=hist, sources=[Source(url=best.url, snapshot_id=snap.snapshot_id)], confident=True)
        body = "; ".join(r.render() for r in chosen)
        when = f" (rates effective as of {best.eff.strftime('%m/%d/%Y')})" if best.eff else ""
        prefix = f"As of {snap.taken_on}, " if as_of else ""
        text = f"{prefix}{body}{when}."
        note = self._rate_conflict_note(best, cands)
        bvals = {c for _, c in best.rate_cells()}
        twins = {r.url for r in cands if r.url != best.url and r.kind in ("product", "rates")
                 and r.eff == best.eff and {c for _, c in r.rate_cells()} == bvals}
        cap_t = set(tokens(best.caption))

        def page_fit(u):
            p = snap.pages.get(u)
            ut = set(tokens(u.split("dupaco.com", 1)[-1])) | set(tokens(p.title if p else ""))
            return (len(ut & (qtok | cap_t)), page_kind(u) == "product", -len(u))
        order = sorted(twins | {best.url}, key=page_fit, reverse=True)
        srcs = [Source(url=u, snapshot_id=snap.snapshot_id, quote=best.render()[:200] if u == order[0] else None)
                for u in order[:3]]
        return Answer(text=text, sources=srcs, confident=True, conflict_note=note)

    def _history(self, best: "RateRow", pick_key) -> Optional[str]:
        """How one fact moved across the snapshots (official copy per snapshot)."""
        key = self._fact_key(best)
        my = model_years(best.label)
        seq = []
        for sn in self.snaps:
            rows = [r for r in sn.rows if self._fact_key(r) == key and
                    (not my or not model_years(r.label) or
                     min(model_years(r.label)[1], my[1]) - max(model_years(r.label)[0], my[0]) >= 1)]
            if not rows:
                seq.append((sn.taken_on, None))
                continue
            r = max(rows, key=pick_key)
            seq.append((sn.taken_on, ", ".join(f"{c} {self._short_hdr(h)}".strip() for h, c in r.rate_cells()
                                               if "point" not in h.lower() and "payment" not in h.lower())))
        runs = []
        for d, v in seq:
            if runs and runs[-1][2] == v:
                runs[-1][1] = d
            else:
                runs.append([d, d, v])
        name = best.head()
        if len(runs) == 1:
            return f"{name}: unchanged at {runs[0][2]} in every snapshot from {runs[0][0]} to {runs[0][1]}."
        parts = []
        for a, b, v in runs:
            span = a if a == b else f"{a} to {b}"
            parts.append(f"{v if v else 'not listed'} ({span})")
        return f"{name} has changed: " + " -> ".join(parts) + f". Current value: {runs[-1][2]}."

    def _rate_conflict_note(self, best: RateRow, cands: list[RateRow]) -> Optional[str]:
        bv = {c for _, c in best.rate_cells()}
        others = []
        for r in cands:
            rv = {c for _, c in r.rate_cells()}
            if rv and rv != bv and r.url != best.url:
                others.append((r.url, r.eff))
        if not others:
            return None
        u, e = others[0]
        return (f"Another page ({u}, effective {iso(e) or 'undated'}) shows a different value; "
                f"used {best.url} (effective {iso(best.eff) or 'undated'}) because it is the newest "
                f"official rate table.")

    def _all_cards(self, q: str, snap: SnapIndex) -> Optional[Answer]:
        ql = q.lower()
        want_bus = "business" in ql
        want_personal = "personal" in ql
        rows = [r for r in snap.rows if "card" in " ".join(r.headers).lower() or "visa" in r.caption.lower()]
        if not rows:
            return None
        best: dict[str, RateRow] = {}
        for r in rows:
            is_bus = "business" in (r.label + r.caption + r.url).lower()
            if (want_bus and not is_bus) or (want_personal and is_bus):
                continue
            if "intro" in r.label.lower() or "transfer" in r.label.lower():
                continue
            name = r.caption.split(" / ")[-1]
            if "visa" not in name.lower():
                name = r.label
            k = name.lower()
            cur = best.get(k)
            if cur is None or (r.eff or date(1900, 1, 1), AUTHORITY.get(r.kind, .5)) > \
                    (cur.eff or date(1900, 1, 1), AUTHORITY.get(cur.kind, .5)):
                best[k] = r
        if not best:
            return None
        parts = []
        for k, r in sorted(best.items(), key=lambda kv: ("business" in kv[0], kv[0])):
            vals = ", ".join(c for _, c in r.rate_cells())
            name = r.caption.split(" / ")[-1] if "visa" in r.caption.lower() else r.label
            parts.append(f"{name}: {vals} APR")
        urls = []
        for r in best.values():
            u = "https://www.dupaco.com/business/credit-cards" if "business" in r.url else \
                "https://www.dupaco.com/personal/credit-cards"
            if u not in urls and (u.split("dupaco.com")[1] in "".join(snap.pages)):
                urls.append(u)
        if not urls:
            urls = sorted({r.url for r in best.values()})
        return Answer(text="Dupaco's credit cards: " + "; ".join(parts) + ".",
                      sources=[Source(url=u, snapshot_id=snap.snapshot_id) for u in urls], confident=True)

    # ---------------------------------------------------------------- fees
    def _fee(self, q: str, snap: SnapIndex) -> Optional[Answer]:
        lines = []
        for p in snap.pages.values():
            for t, eff in p.fees:
                lines.append((t, eff, p.url))
        if not lines:
            return None
        idf = self._idf_cache
        want = [t for t in set(content_tokens(q)) if t not in {"fee", "charge", "cost", "much", "price", "pay"}
                and t not in GENERIC]
        if not want:
            return None
        tot = sum(idf.get(t, 1.0) for t in want)
        best = None
        for t, eff, url in lines:
            lt = set(tokens(t))
            cov = sum(idf.get(w, 1.0) for w in want if w in lt) / tot
            key = (cov, eff or date(1900, 1, 1), -len(t))
            if best is None or key > best[0]:
                best = (key, t, eff, url)
        (cov, _, _), line, eff, url = best
        if cov >= 0.75:
            when = f" (fee schedule effective {eff.strftime('%B %d, %Y')})" if eff else ""
            return Answer(text=f"Per Dupaco's Rate and Fee Schedule: {line}{when}.",
                          sources=[Source(url=url, snapshot_id=snap.snapshot_id, quote=line)], confident=True)
        # maybe a product page states it ("no monthly fee"); otherwise say it isn't listed
        hits = self._search(q, snap, k=6)
        if hits:
            text, b = self._compose(q, hits, snap=snap)
            bt = set(tokens(text))
            if sum(idf.get(w, 1.0) for w in want if w in bt) / tot >= 0.6:
                return Answer(text=text, sources=[Source(url=b.url, snapshot_id=snap.snapshot_id)], confident=True)
        return Answer(text=("I can't find that fee in Dupaco's published Rate and Fee Schedule, so I don't want to "
                            f"guess; {CONTACT_US}."),
                      sources=[Source(url=url, snapshot_id=snap.snapshot_id)], confident=False)

    def _label_fact(self, q: str, snap: SnapIndex) -> Optional[Answer]:
        qt = set(content_tokens(q))
        best = None
        for p in snap.pages.values():
            for lab, val in p.labels:
                lt = set(content_tokens(lab))
                if not lt or not lt <= qt:
                    continue
                rank = (len(lt), p.url.endswith("/contact"), AUTHORITY.get(p.kind, .5), p.url == "https://www.dupaco.com")
                if best is None or rank > best[0]:
                    best = (rank, lab, val, p.url)
        if not best:
            return None
        _, lab, val, url = best
        also = sorted({p.url for p in snap.pages.values() for l2, v2 in p.labels
                       if v2 == val and p.url != url and p.kind in ("product", "other")})
        srcs = [Source(url=url, snapshot_id=snap.snapshot_id)] + [Source(url=u, snapshot_id=snap.snapshot_id) for u in also[:3]]
        return Answer(text=f"Dupaco's {lab.lower()}: {val}.", sources=srcs, confident=True)

    # ---------------------------------------------------------------- hours
    def _hours(self, q: str, snap: SnapIndex) -> Answer:
        ql = q.lower()
        drive = "drive" in ql
        # a specific branch named?  "... for the Mullan Avenue branch"
        m = re.search(r"(?:for|at|of) (?:the |your |dupaco'?s? )?([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*)\s+(?:branch|office|location)", q)
        name = m.group(1).strip() if m and m.group(1).lower() not in ("dupaco", "general", "main") else None
        segs = sorted(snap.hours, key=lambda h: (h[3] or date(1900, 1, 1)), reverse=True)
        if name:
            nl = name.lower()
            hits = [h for h in segs if nl in (h[1] + h[2]).lower()]
            if not hits:
                return Answer(text=(f"I can't find published hours for a {name} branch on Dupaco's website "
                                    f"(it isn't listed in the branch information I have), so I won't guess. Please contact "
                                    f"Dupaco or check the branch locator at dupaco.com/contact."),
                              sources=[Source(url="https://www.dupaco.com/contact", snapshot_id=snap.snapshot_id)],
                              confident=False)
            segs = hits
        if not segs:
            return self._general(q, snap, None)
        lobby, drive_up = [], []
        for seg, pre, url, pub in segs:
            parts = re.split(r"(?i)\bdrive[- ]up\b", seg, maxsplit=1)
            def norm(x):
                x = re.sub(r"\s*\n\s*", "; ", x).strip("; :")
                x = x.replace("–", "-").replace("—", "-")
                return re.sub(r"\s+", " ", x)
            lob = norm(re.sub(r"(?i)^\s*lobby\s*", "", parts[0]))
            if lob and lob.lower() not in [x[0].lower() for x in lobby]:
                lobby.append((lob, url))
            if len(parts) > 1:
                d = norm(parts[1])
                if d and d.lower() not in [x[0].lower() for x in drive_up]:
                    drive_up.append((d, url))
        if drive and drive_up:
            main = drive_up[0]
            text = (f"Drive-up hours at most Dupaco branches: {main[0]}. "
                    + (f"Some branches differ (e.g. {drive_up[1][0]}). " if len(drive_up) > 1 else "")
                    + "Hours vary by branch - which branch would you like? The branch locator at "
                      "https://www.dupaco.com/contact lists every location.")
            urls = [main[1]] + ([drive_up[1][1]] if len(drive_up) > 1 else [])
        else:
            main = lobby[0] if lobby else (segs[0][0], segs[0][2])
            alts = [] if name else [x for x in lobby[1:4]]
            label = f"{name} branch lobby hours" if name else "Typical Dupaco lobby hours"
            text = (f"{label}: {main[0]}. "
                    + ("Other branches use a different schedule: " + " / ".join(a[0] for a in alts) + ". " if alts else "")
                    + "Lobby hours vary by branch - which branch (or all branches) would you like? "
                      "See https://www.dupaco.com/contact for each location.")
            urls = [main[1]] + [a[1] for a in alts]
        urls = list(dict.fromkeys(urls + ["https://www.dupaco.com/contact"]))
        return Answer(text=text, sources=[Source(url=u, snapshot_id=snap.snapshot_id) for u in urls], confident=True)

    # ---------------------------------------------------------------- address
    def _address(self, q: str, snap: SnapIndex) -> Optional[Answer]:
        m = re.search(r"(?:the |of |for )?([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*?)\s+(?:[Bb]ranch|[Oo]ffice|[Ll]ocation)", q)
        name = (m.group(1) if m else "").strip()
        name = re.sub(r"^(What|Where|Is|The)\s+", "", name)
        if not name:
            return None
        nl = name.lower()
        addr_re = re.compile(r"\d{2,6} [\w .'-]+?, [A-Za-z .]+, (?:IA|IL|WI|MN), \d{5}")
        best = None
        # the branch locator lists "Dupaco Branch\n<street>, <city>, <ST>, <zip>"
        branch_addr = re.compile(r"Dupaco Branch\s*\n\s*(\d{2,6} [^\n]+?, [A-Za-z .]+, (?:IA|IL|WI|MN), \d{5})")
        for b in snap.blocks:
            if "Dupaco Branch" not in b.text:
                continue
            for am in branch_addr.finditer(b.text):
                a = am.group(1)
                if nl in a.lower():
                    best = (a, b)
                    break
            if best:
                break
        if not best:
            # fall back to prose: "located at 1100 E. Business Highway 151"
            for b in sorted(snap.blocks, key=lambda b: -(b.pub or date(1900, 1, 1)).toordinal()):
                if nl in b.text.lower():
                    mm = re.search(r"(?i)" + re.escape(name) + r"[^.]{0,60}?located at (\d[^,]+?(?:\. [^,]+?)?)(?:,| and|\.\s)", b.text)
                    if mm:
                        best = (mm.group(1), b)
                        break
        if not best:
            return None
        a, b = best
        return Answer(text=f"The {name} branch is at {a}.",
                      sources=[Source(url=b.url, snapshot_id=snap.snapshot_id, quote=a)], confident=True)

    # ---------------------------------------------------------------- links
    def _link(self, q: str, snap: SnapIndex) -> Optional[Answer]:
        ql = q.lower()
        if not re.search(r"\b(link|url|website|web ?page|page|site|locator|online)\b", ql):
            return None
        if re.search(r"\b(locator|locations?|find a branch|atms?)\b", ql) and "https://www.dupaco.com/contact" in snap.pages:
            u = "https://www.dupaco.com/contact"
            return Answer(text=f"Use Dupaco's branch & ATM locator at {u}.",
                          sources=[Source(url=u, snapshot_id=snap.snapshot_id)], confident=True)
        hits = self._search(q, snap, k=10)
        if not hits:
            return None
        qtok = self._expand(q) - LINK_WORDS - {"see", "use", "go", "online", "dupaco"}
        want_bus = "business" in ql
        top = hits[0][0] or 1.0
        best = None
        for rank, (bsc, b) in enumerate(hits):
            for m in re.finditer(r"([^<>\n]{0,80})<(https?://[^>\s]+)>", b.text):
                anchor, url = m.group(1).strip(" -:>"), m.group(2)
                if "dupaco.com" not in url or "/hubfs/" in url or re.search(r"\.(png|jpe?g|svg|webp|pdf)$", url) \
                        or "/newsroom/tag/" in url or "/author/" in url:
                    continue
                at = set(tokens(anchor)) | set(tokens(url.split("dupaco.com", 1)[-1]))
                sc = len(qtok & at) + 0.6 * len(qtok & set(tokens(b.heading)))
                if ("join" in ql or "become a member" in ql) and "join.dupaco.com" in url:
                    sc += 3
                if "business" in url and not want_bus:
                    sc -= 2
                sc *= 0.5 + 0.5 * bsc / top
                if best is None or sc > best[0]:
                    best = (sc, anchor, url, b)
        if best and best[0] >= 1.0:
            sc, anchor, url, b = best
            if "join.dupaco.com" in url and ("join" in ql or "member" in ql):
                url = re.match(r"https?://[^/]+/?", url).group(0)
                text = f"You can join Dupaco online at {url}"
            else:
                text = f"Use {url}" + (f" ({anchor})" if anchor and len(anchor) < 60 else "")
            return Answer(text=text + ".", sources=[Source(url=url, snapshot_id=snap.snapshot_id),
                                                    Source(url=b.url, snapshot_id=snap.snapshot_id)],
                          confident=True)
        url = hits[0][1].url
        return Answer(text=f"Use {url}.", sources=[Source(url=url, snapshot_id=snap.snapshot_id)], confident=True)

    # ---------------------------------------------------------------- general retrieval
    def _search(self, q: str, snap: SnapIndex, k: int = 10, recency: bool = True):
        qt = list(self._expand(q))
        qt += [w[:6] + "~" for w in qt if len(w) >= 7 and w.isalpha()]
        if not qt:
            return []
        raw = snap.bm25.search(qt)
        if not raw:
            return []
        top_raw = max(raw.values())
        need = [t for t in set(content_tokens(q)) if t not in GENERIC]
        spec_url = set(content_tokens(q)) | {"loan"} & set(tokens(q))
        need_alts = [{t} | {stem(x) for x in SYN.get(t, [])} | ({t[:6] + "~"} if len(t) >= 7 else set()) for t in need]
        cand = []
        for i, sc in raw.items():
            if sc < 0.3 * top_raw:
                continue
            b = snap.blocks[i]
            w = 0.4 + 0.6 * AUTHORITY.get(b.kind, 0.5)
            if need_alts:
                bt = snap.doc_sets[i]
                cov = sum(1 for a in need_alts if a & bt) / len(need_alts)
                w *= 0.35 + 0.65 * cov * cov
            if recency and b.pub:
                w *= 1 + 0.01 * max(0, b.pub.year - 2015)
            if b.kind in ("product", "rates") and spec_url:
                ut = snap.url_toks.get(b.url)
                if ut is None:
                    ut = snap.url_toks[b.url] = set(tokens(b.url.split("dupaco.com", 1)[-1])) - {"personal", "business"}
                w *= 1 + 0.15 * len(spec_url & ut)
            cand.append((sc * w, b))
        cand.sort(key=lambda x: (-x[0], x[1].url, x[1].text[:40]))
        cand = cand[:60]
        # collapse near-duplicate blocks (same boilerplate / re-published story):
        # the NEWEST dated version wins, otherwise the most authoritative page.
        out, groups = [], []
        for sc, b in cand:
            bt = set(tokens(b.text))
            placed = False
            for g in groups:
                gt = g["toks"]
                inter = len(bt & gt)
                same_story = b.title and b.title == g["title"] and b.url != g["url"] and b.kind in ("news", "blog")
                if same_story or (b.url != g["url"] and inter and inter / max(1, min(len(bt), len(gt))) > 0.6
                                  and abs(len(bt) - len(gt)) < 0.5 * max(len(bt), len(gt))):
                    g["members"].append((sc, b))
                    placed = True
                    break
            if not placed:
                groups.append({"toks": bt, "members": [(sc, b)], "title": b.title, "url": b.url})
        for g in groups:
            mem = g["members"]
            top_sc = max(m[0] for m in mem)
            best = max(mem, key=lambda m: ((m[1].pub or date(1900, 1, 1)), AUTHORITY.get(m[1].kind, .5), m[0]))
            out.append((top_sc, best[1]))
        out.sort(key=lambda x: (-x[0], x[1].url, x[1].text[:40]))
        return out[:k]

    def _sentences(self, text: str) -> list[str]:
        parts = []
        for line in text.split("\n"):
            parts += [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])", line) if s.strip()]
        return parts

    def _sent_score(self, q: str, qt: set, sent: str, head: set = frozenset(), idf=None) -> float:
        """0 = unrelated; otherwise idf-weighted share of the question covered (plus small bonuses)."""
        ql = q.lower()
        st = set(index_tokens(sent))
        idf = idf or {}
        w = lambda t: idf.get(t, 1.0) * (0.35 if t in GENERIC else 1.0)
        tot = sum(w(t) for t in qt) or 1.0
        hit = sum(w(t) for t in qt & st) + 0.5 * sum(w(t) for t in (qt & head) - st)
        if hit <= 0:
            return 0.0
        v = hit / tot
        if "dupaco" in ql and "dupaco" in st:
            v += 0.05
        if re.search(r"\b(when|date|still open|close|closing|closed|open)\b", ql) and \
                (DATE_TXT_RE.search(sent) or DATE_NUM_RE.search(sent)) and len(sent) > 25:
            v += 0.3
        if re.search(r"\b(how many|how much|number|amount|count)\b", ql) and re.search(r"\d", sent):
            v += 0.2
        if re.search(r"\d", sent):
            v += 0.03
        if re.search(r"(?i)for more information|click here|learn more|^\W*visit\b|read more", sent):
            v -= 0.25
        if sent.lstrip()[:1] in "“\"":
            v -= 0.15   # quotes are opinions; prefer stated facts
        if len(re.sub(r"\(https?://[^)]*\)|<[^>]*>", "", sent).strip()) < 25:
            v -= 0.3    # link labels / buttons ("Apply today")
        return max(v, 0.001)

    def _compose(self, q: str, hits, max_chars: int = 420, snap=None):
        """Pick the best sentence among the top blocks, then read forward in that block."""
        qt = self._expand(q)
        qt |= {w[:6] + "~" for w in qt if len(w) >= 7 and w.isalpha()}
        if not hits:
            return "", None
        top = hits[0][0] or 1.0
        best = None
        idf = self._idf_cache
        pool = list(hits[:5])
        if snap is not None:
            have = {id(b) for _, b in pool}
            p0 = hits[0][1].url
            pool += [(top, b) for b in snap.blocks_by_url.get(p0, []) if id(b) not in have]
        for rank, (bsc, b) in enumerate(pool):
            sents = self._sentences(b.text)
            head = set(index_tokens(b.heading))
            for j, sent in enumerate(sents):
                v = self._sent_score(q, qt, sent, head, idf)
                if v <= 0:
                    continue
                total = (bsc / top) * (0.55 + 0.45 * min(v, 1.3))
                key = (total, -rank, -j)
                if best is None or key > best[0]:
                    best = (key, b, sents, j)
        if best is None:
            return self._sentences(hits[0][1].text)[0][:max_chars], hits[0][1]
        _, b, sents, i = best
        out = [sents[i]]
        j = i + 1
        # "Both branches are ..." / "This program ..." -> the referent is in the previous sentence
        if i > 0 and re.match(r"(?i)^(both|this|these|they|it|that|those|its|their|the program|the branch)\b", sents[i]):
            out.insert(0, sents[i - 1])
        if sents[i].rstrip().endswith("?") and i + 1 < len(sents):
            out.append(sents[i + 1]); j = i + 2
        while j < len(sents) and len(" ".join(out)) + len(sents[j]) < max_chars and \
                (self._sent_score(q, qt, sents[j], frozenset(), idf) > 0.15 or len(" ".join(out)) < 160 or sents[j].startswith("-")):
            out.append(sents[j])
            j += 1
        if not re.search(r"\d", " ".join(out)) and j < len(sents) and re.search(r"\d", sents[j]) \
                and len(" ".join(out)) + len(sents[j]) < max_chars + 200:
            out.append(sents[j])
        res = " ".join(out)
        res = re.sub(r"\s*<(https?://[^>]+)>", lambda m: (" (" + m.group(1) + ")") if "dupaco.com" in m.group(1)
                     and "hubfs" not in m.group(1) else "", res)
        return res, b

    def _general(self, q: str, snap: SnapIndex, as_of) -> Answer:
        ql = q.lower()
        # count questions that boilerplate answers ("how many branches")
        if re.search(r"how many (branch|location|office)", ql):
            facts = []
            for p in snap.pages.values():
                for k, topic, v in p.facts:
                    if k == "branch_count":
                        facts.append((p.pub or date(1900, 1, 1), AUTHORITY.get(p.kind, .5), v, p.url))
            if facts:
                facts.sort(reverse=True)
                pub, _, v, url = facts[0]
                return Answer(text=f"Dupaco has {v} branch office locations (per its most recent "
                                   f"announcement{', ' + pub.strftime('%B %d, %Y') if pub.year > 1900 else ''}).",
                              sources=[Source(url=url, snapshot_id=snap.snapshot_id)], confident=True,
                              conflict_note="Older press releases give other branch counts; the newest one is used.")
        if re.search(r"\b(phone|telephone|routing|fax|mailing address|p\.?o\.? box|nmls)\b", ql):
            a = self._label_fact(q, snap)
            if a:
                return a
        hits = self._search(q, snap, k=8)
        qcont = [t for t in set(content_tokens(q)) if t not in GENERIC]
        if not hits:
            return Answer(text=f"I don't know - that isn't covered on Dupaco's website; {CONTACT_US}.",
                          confident=False)
        sc, b = hits[0]
        matched = set(tokens(b.text + " " + b.heading + " " + b.title)) & self._expand(q)
        if qcont and len(matched & set(qcont)) == 0:
            return Answer(text=f"I don't know - I couldn't find that on Dupaco's website; {CONTACT_US}.",
                          confident=False)
        text, b = self._compose(q, hits, snap=snap)
        if re.search(r"^\s*how (can|do|should) i\b", ql) and not re.search(r"\d{3}-\d{3}-\d{4}", text):
            text += " You can also call Dupaco at 800-373-7600 or use Shine Online & Mobile Banking."
        prefix = f"As of {snap.taken_on}: " if as_of else ""
        srcs = [Source(url=b.url, snapshot_id=snap.snapshot_id, quote=text[:200])]
        for sc2, b2 in hits[1:3]:
            if b2.url != b.url:
                srcs.append(Source(url=b2.url, snapshot_id=snap.snapshot_id))
        return Answer(text=prefix + text, sources=srcs, confident=True)

    # ---------------------------------------------------------------- conflicts
    @staticmethod
    def _canon_caption(cap: str, label: str, headers) -> str:
        c = (cap.split(" / ")[-1] if cap else "").lower()
        if "model year" in label.lower() or "auto" in c or "motorcycle" in c:
            return "auto"
        c = re.sub(r"\b(rates?|loans?|accounts?|certificates?|the|-)\b", " ", c)
        c = re.sub(r"[^a-z0-9 ]", " ", c)
        return re.sub(r"\s+", " ", c).strip() or (headers[0].lower() if headers else "")

    @staticmethod
    def _canon_label(label: str) -> str:
        l = MODEL_RANGE_RE.sub("modelyear", MODEL_OLDER_RE.sub("oldmodelyear", label))
        l = l.lower().replace("–", "-").replace("—", "-")
        l = re.sub(r"(\$\d[\d,]*)\.00\b", r"\1", l)
        l = re.sub(r"(?<=\d),(?=\d{3})", "", l)
        l = re.sub(r"\bnone\b|\|", " ", l)
        l = re.sub(r"\baccounts?\b", " ", l)
        l = re.sub(r"\s*-\s*", "-", l)
        l = re.sub(r"\bmonths\b", "month", l)
        return re.sub(r"[\s:]+", " ", l).strip()

    def conflicts(self) -> list[Conflict]:
        """One entry per fact that the site has stated with different values (across pages and/or
        across snapshots). Resolution = what answer() would say today: newest snapshot, then the
        table with the newest effective date (or newest publish date), then the most authoritative page."""
        obs: dict[tuple, list] = defaultdict(list)
        topics: dict[tuple, str] = {}
        for si, snap in enumerate(self.snaps):
            for r in snap.rows:
                cells = [(h, c) for h, c in r.rate_cells() if "payment" not in h.lower() and "point" not in h.lower()]
                if not cells:
                    continue
                nums = tuple(sorted({round(float(x), 3) for _, c in cells for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", c)}))
                if not nums:
                    continue
                bus = "business" in r.url
                key = self._fact_key(r)
                shown = " / ".join(f"{re.sub(r'[ (].*', '', c.strip())} {self._short_hdr(h)}".strip() for h, c in cells)
                sane = 0 if self._inconsistent(r) else 1
                obs[key].append((si, (sane, r.eff or date(1900, 1, 1)), AUTHORITY.get(r.kind, .5), nums, shown, r.url,
                                 snap.snapshot_id, model_years(r.label)))
                cap = r.caption.split(" / ")[-1] if r.caption else ""
                topics[key] = (("Business " if bus else "") + (cap + " - " if cap and cap.lower() not in r.label.lower() else "")
                               + r.label).strip()
            for p in snap.pages.values():
                for k, topic, v in p.facts:
                    key = ("fact", k)
                    obs[key].append((si, (1, p.pub or date(1900, 1, 1)), AUTHORITY.get(p.kind, .5), (v,), v, p.url,
                                     snap.snapshot_id, None))
                    topics[key] = topic
        # tier-only rows ("None | $5,000 - $24,999.99" on a landing page) belong to the one named
        # product that has exactly that tier
        named = [k for k in obs if k[0] == "row" and not k[2]]
        for k in [k for k in list(obs) if k[0] == "row" and k[2] and k[2] != "auto"]:
            tier = re.sub(r"^none\s*", "", k[3]).strip()
            if not re.search(r"\$\d", tier):
                continue
            cands = [n for n in named if n[1] == k[1] and n[3].endswith(" " + tier)]
            if len(cands) == 1:
                obs[cands[0]].extend(obs.pop(k))
        out = []
        for key, lst in obs.items():
            groups = [lst]
            if key[0] == "row" and any(o[7] for o in lst):
                # model-year tiers: only compare year ranges that overlap (2023-26 vs 2024-27)
                groups, used = [], set()
                for rg in sorted({o[7] for o in lst if o[7]}, key=lambda x: x[1], reverse=True):
                    if rg in used:
                        continue
                    grp = {x for x in {o[7] for o in lst if o[7]} if x not in used
                           and min(x[1], rg[1]) - max(x[0], rg[0]) >= 1}
                    used |= grp
                    groups.append([o for o in lst if o[7] in grp])
            for g in groups:
                distinct = []
                for o in sorted(g, key=lambda o: o[0]):
                    if o[3] not in [d[3] for d in distinct]:
                        distinct.append(o)
                if len(distinct) < 2 or len({d[4] for d in distinct}) < 2:
                    continue
                latest = max(o[0] for o in g)
                cur = [o for o in g if o[0] == latest]
                win = max(cur, key=lambda o: (o[1], o[2], o[5].endswith("/rates")))
                stale_now = [o for o in cur if o[3] != win[3]]
                older = [o for o in g if o[0] < latest and o[3] != win[3]]
                bits = []
                if stale_now:
                    lo = max(stale_now, key=lambda o: o[2])
                    bits.append(f"in the newest snapshot {lo[5]} still shows {lo[4]}"
                                + (f" (dated {iso(lo[1][1])})" if lo[1][1].year > 1900 else "")
                                + (" which is internally inconsistent (dividend rate above APY)" if not lo[1][0] else "")
                                + f" while {win[5]} shows {win[4]}"
                                + (f" (dated {iso(win[1][1])})" if win[1][1].year > 1900 else "")
                                + "; the newer, more authoritative source wins")
                if older:
                    bits.append("earlier snapshots showed " + ", ".join(dict.fromkeys(o[4] for o in older))
                                + "; the newest snapshot is current")
                values = [d[4] for d in distinct]
                if win[4] not in values:
                    values.append(win[4])
                srcs, seen = [], set()
                for o in [win] + sorted(g, key=lambda o: -o[0]):
                    if (o[5], o[4]) in seen:
                        continue
                    seen.add((o[5], o[4]))
                    srcs.append(Source(url=o[5], snapshot_id=o[6], quote=o[4]))
                    if len(srcs) >= 5:
                        break
                topic = topics[key]
                if key[0] == "row" and g[0][7]:
                    rg = max((o[7] for o in g), key=lambda x: x[1])
                    topic = MODEL_RANGE_RE.sub("", MODEL_OLDER_RE.sub("", topic)).strip(" |-") + \
                        f" auto loan APR ({rg[0] if rg[0] > 1900 else 'up to'}-{rg[1]} model year)"
                out.append(Conflict(topic=re.sub(r"\s+", " ", topic.replace(" | ", " ")),
                                    values=values[:8], sources=srcs, resolution=win[4],
                                    reason="; ".join(bits) or "newest snapshot and source"))
        out.sort(key=lambda c: c.topic)
        return out

    def _fact_key(self, r: "RateRow") -> tuple:
        cc = self._canon_caption(r.caption, r.label, r.headers)
        prod_words = [t for t in tokens(r.label) if t.isalpha() and t not in ROW_NEUTRAL
                      and t not in ("fixed", "none", "variable", "standard", "minimum", "open", "no")]
        fam = "auto" if cc == "auto" else ("" if prod_words else cc)
        return ("row", "business" in r.url, fam, self._canon_label(r.label))

    @staticmethod
    def _inconsistent(r: "RateRow") -> bool:
        """A dividend rate above its own APY is impossible (APY >= rate when compounding)."""
        div = apy = None
        for h, c in r.rate_cells():
            m = re.search(r"(\d+(?:\.\d+)?)\s?%", c)
            if not m:
                continue
            hl = h.lower()
            if "dividend" in hl:
                div = float(m.group(1))
            elif "yield" in hl or "apy" in hl:
                apy = float(m.group(1))
        return div is not None and apy is not None and div > apy + 1e-9

    @staticmethod
    def _short_hdr(h: str) -> str:
        hl = h.lower()
        if "yield" in hl or "apy" in hl:
            return "APY"
        if "apr" in hl or "percentage rate" in hl:
            return "APR"
        if "dividend" in hl:
            return "dividend rate"
        if "rate" in hl:
            return "rate"
        return ""


def build():
    return TemporalKnowledge()
