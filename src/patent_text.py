"""Parse claims/description blobs into structured, coordinate-preserving units (spec §3, §4)."""
from __future__ import annotations
import re

CLAIM_START = re.compile(r"(?m)^\s{0,8}(\d{1,3})\s*\.\s")
# references to other claims: "claim 1", "claims 1 to 5", "claims 1-3", "claim 1 or 2", "any of claims 1 and 2"
CLAIM_REF = re.compile(r"\bclaims?\s+((?:\d+\s*(?:to|through|and|or|[-–,])?\s*)+)", re.I)
PREAMBLE = re.compile(r"^\s*(what\s+is\s+claimed\s+is\s*:?|we\s+claim\s*:?|i\s+claim\s*:?|claims?\s*:?)",
                      re.I)


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def split_claims(blob: str):
    """-> list of dicts {claim_no, text}. Splits on leading 'N.' claim numbers."""
    if not blob:
        return []
    blob = PREAMBLE.sub("", blob)
    starts = list(CLAIM_START.finditer(blob))
    out = []
    for i, m in enumerate(starts):
        no = int(m.group(1))
        s = m.end()
        e = starts[i + 1].start() if i + 1 < len(starts) else len(blob)
        text = re.sub(r"\s+", " ", blob[s:e]).strip()
        if not text:
            continue
        # claim numbers should be monotonic-ish; skip stray matches that jump backwards hard
        if out and no <= out[-1]["claim_no"] and no != out[-1]["claim_no"] + 1:
            # likely a false split (e.g. an enumerated sub-item) -> append to previous
            out[-1]["text"] += " " + text
            continue
        out.append({"claim_no": no, "text": text})
    return out


def _expand_refs(fragment: str):
    nums = set()
    # ranges first
    for a, sep, b in re.findall(r"(\d+)\s*(to|through|[-–])\s*(\d+)", fragment, re.I):
        try:
            lo, hi = int(a), int(b)
            if 0 < lo <= hi <= 200:
                nums.update(range(lo, hi + 1))
        except ValueError:
            pass
    for n in re.findall(r"\d+", fragment):
        nums.add(int(n))
    return {n for n in nums if 0 < n <= 200}


def dependencies(text: str):
    """-> (is_independent, sorted list of parent claim numbers)."""
    refs = set()
    for m in CLAIM_REF.finditer(text):
        refs |= _expand_refs(m.group(1))
    return (len(refs) == 0), sorted(refs)


def resolve_claims(claims):
    """Attach is_independent, parents, and resolved_text (own text WITH inherited limitations).
    claims: list of {claim_no, text}. Returns same list with fields added."""
    by_no = {c["claim_no"]: c for c in claims}
    for c in claims:
        indep, parents = dependencies(c["text"])
        # a claim can't depend on itself or a later claim
        parents = [p for p in parents if p in by_no and p < c["claim_no"]]
        c["is_independent"] = indep or not parents
        c["parents"] = parents

    memo = {}

    def resolved(no, seen):
        if no in memo:
            return memo[no]
        c = by_no.get(no)
        if not c:
            return ""
        if c["is_independent"] or not c["parents"] or no in seen:
            memo[no] = c["text"]
            return c["text"]
        # resolve against the lowest-numbered parent chain (standard convention)
        parent = min(c["parents"])
        pref = resolved(parent, seen | {no})
        full = (pref + "\n" + c["text"]).strip()
        memo[no] = full
        return full

    for c in claims:
        c["resolved_text"] = resolved(c["claim_no"], set())
        c["token_count"] = _est_tokens(c["resolved_text"])
    return claims


HEADING = re.compile(r"^[A-Z][A-Z0-9 ,/&\-()]{2,58}$")
FIG_LINE = re.compile(r"\bFIG(?:URE)?S?\.?\s*\d", re.I)
REFNUM = re.compile(r"\b(\d{1,3})\b")


def split_paragraphs(blob: str, target_chars=1000, max_chars=1600):
    """Group description lines into paragraph chunks, tracking the current section heading and
    a running paragraph number. -> list {para_no, heading, text}."""
    if not blob:
        return []
    lines = [ln.strip() for ln in re.split(r"\n+", blob) if ln.strip()]
    out, cur, heading, pno = [], [], None, 0

    def flush():
        nonlocal cur, pno
        if cur:
            pno += 1
            txt = re.sub(r"\s+", " ", " ".join(cur)).strip()
            if txt:
                out.append({"para_no": f"p{pno:04d}", "heading": heading, "text": txt})
            cur = []

    for ln in lines:
        if HEADING.match(ln) and len(ln) < 60:
            flush()
            heading = ln.title()
            continue
        cur.append(ln)
        if sum(len(x) for x in cur) >= target_chars:
            flush()
    flush()
    # hard-cap overly long paragraphs
    capped = []
    for p in out:
        t = p["text"]
        if len(t) <= max_chars:
            capped.append(p)
        else:
            for i in range(0, len(t), max_chars):
                capped.append({**p, "text": t[i:i + max_chars]})
    return capped


def figure_captions(blob: str):
    """Best-effort figure captions from the 'BRIEF DESCRIPTION OF THE DRAWINGS' region."""
    if not blob:
        return []
    caps = []
    for ln in re.split(r"\n+", blob):
        ln = re.sub(r"\s+", " ", ln).strip()
        if FIG_LINE.search(ln) and 8 < len(ln) < 400:
            refs = sorted(set(REFNUM.findall(ln)))
            m = re.search(r"FIG(?:URE)?S?\.?\s*(\d+[A-Z]?)", ln, re.I)
            caps.append({"figure_no": m.group(1) if m else None, "caption": ln,
                         "reference_numbers": refs})
    # dedupe by caption
    seen, uniq = set(), []
    for c in caps:
        if c["caption"] not in seen:
            seen.add(c["caption"]); uniq.append(c)
    return uniq[:60]
