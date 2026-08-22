"""The niche corpus: what the boundary is, what a manifest record is, how to read one.

The niche is the retrieval universe for vacuum generation, vacuum gripping, suction handling,
lifting and hoisting, material handling, conveyors and robot grippers. `docs/niche_boundary.md`
records how the boundary was measured; `docs/niche_manifest_contract.md` is the reader's contract
and `docs/corpus_completeness.md` carries the numbers. This module is the executable copy of all
three: the boundary predicate, the record builder and the manifest reader/writer live here so that
`ops/niche_*.py`, the tests and workstream C all agree by construction rather than by convention.

WHY THE BOUNDARY IS NOT A LIST OF CPC BRANCHES ALONE. MEASURED on the live corpus, 2026-08-22:
of the 148,942 examiner-cited documents reachable from the seeded field, 28.5% carry a symbol in
the six subclasses the corpus holds completely, 35.5% are classified somewhere else entirely, and
35.9% carry no classification at all. No CPC set can reach the last group, and reaching the middle
group by classification costs about 6.9M publications for roughly 22 points of examiner reach. The
boundary therefore has a classification part AND two closures, and the closures are where the reach
actually comes from.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

#  The floors `src/sources/fulltext.py` already uses to decide whether a document has been read in
#  full. Imported rather than restated so the manifest and the fetcher cannot disagree about what
#  "held" means. Kept as a soft import: the tests must run without httpx.
try:  # pragma: no cover - exercised by the real package, stubbed in tests
    from sources.fulltext import MIN_CLAIMS_CHARS, MIN_DESC_CHARS
except Exception:  # pragma: no cover
    MIN_CLAIMS_CHARS = int(os.environ.get("PATENTS_MIN_CLAIMS_CHARS", "200"))
    MIN_DESC_CHARS = int(os.environ.get("PATENTS_MIN_DESC_CHARS", "800"))

MANIFEST_VERSION = 1

#  THE UNCLASSIFIED SHARD HAS ONE NAME. `retrieval.shard_router` emits the literal string
#  "unclassified" as a route and can never return a list without it, because 1,024,320
#  publications, 20.6% of the corpus, carry no classification and they skew old and foreign, which
#  is the population the gold citation lists are drawn from. A manifest that named the same shard
#  `""` would register a shard `hot_domains` never matches, and that whole population would go
#  quietly unreachable in the tier built to reach it. See docs/shard_and_global_seams.md rule 5.6.
try:  # pragma: no cover - the real package; the constant is duplicated for a bare test import
    from retrieval.shard_router import UNCLASSIFIED
except Exception:  # pragma: no cover
    UNCLASSIFIED = "unclassified"

#  The rungs of the ladder in `src/sources/fulltext.py`, in the order that module tries them, and
#  the jurisdictions each one actually serves. `best_source` names the cheapest rung that can
#  serve a family; C may fall further down the ladder but should never fall up it.
SOURCE_LADDER = (
    ("pqai", ("US",)),
    ("epo_ops", ("EP", "WO")),
    ("himmpat", ("CN", "JP", "KR")),
    ("gpatents_direct", ()),          # every jurisdiction, the catch-all rung
)
LOCAL_MEMBER = "local:family_member"

RECORD_FIELDS = ("family_id", "publications", "cpc", "title", "abstract", "has_claims",
                 "has_description", "has_complete_text", "best_source", "missing_fields")


# ---------------------------------------------------------------- CPC symbol handling
def normalise_symbol(symbol):
    """'B25J 15/0616' -> 'B25J15/0616'. '' for anything too short to be a symbol."""
    s = str(symbol or "").strip().upper().replace(" ", "")
    return s if len(s) >= 4 else ""


def subclass_of(symbol):
    """CPC subclass, four characters, or `UNCLASSIFIED` when the symbol is unusable.

    This is `retrieval.shard_router.domain_of` under another name, on purpose and to the letter,
    including the unclassified case: every niche family rolls up to exactly one shard and nobody
    re-derives the mapping. Returning `""` here instead would name the unclassified shard twice.
    """
    s = normalise_symbol(symbol)
    return s[:4] if s else UNCLASSIFIED


def shard_domains_of(symbols):
    """The shard domains one family belongs to, best used on a manifest record's `cpc`.

    A family with no usable symbol belongs to the unclassified shard, not to no shard. MEASURED on
    release niche-2026-08-22: 294,327 of 1,607,502 niche families carry `cpc == []`, and they are
    there because the family and citation closures put them there, so they are exactly the families
    a classification-only shard map would lose.
    """
    doms = {subclass_of(s) for s in (symbols or ())}
    doms.discard("")
    return sorted(doms) if doms else [UNCLASSIFIED]


def main_group_of(symbol):
    """CPC main group: 'B65G47/91' -> 'B65G47'. '' when the symbol is unusable."""
    s = normalise_symbol(symbol)
    return s.split("/")[0] if s else ""


def is_indexing_code(symbol):
    """True for a symbol that tags a document rather than classifying it.

    Two families of them, and both would wreck a boundary rule that counted them as fields:
    CPC section Y (Y02, Y10S, Y10T ... cross-sectional tagging, up to 5.2M publications behind one
    code) and the 2000-series orthogonal indexing subgroups (B65G2201/..., F16B2200/...). They stay
    in a manifest record's `cpc` list, because they are true of the document; they are excluded from
    the boundary, because they are not a technical field anyone can acquire.
    """
    s = normalise_symbol(symbol)
    if not s:
        return True
    if s[0] == "Y":
        return True
    group = main_group_of(s)[4:]
    return group.isdigit() and int(group) >= 2000


# ---------------------------------------------------------------- family identity
#  DOCDB writes -1 for "this publication has no simple family", and the ingest stored it verbatim.
#  MEASURED on the live corpus 2026-08-22: 21,862 publications carry `simple_family_id = '-1'`.
#  The obvious key, COALESCE(NULLIF(simple_family_id,''), publication_number), passes '-1' straight
#  through, so all 21,862 unrelated documents become ONE family. Found because the first manifest
#  record came out with a family holding every CPC symbol from ploughs to harvesters.
NO_FAMILY = frozenset({"", "-1", "0", "null", "none", "n/a", "\\n"})


def family_key(simple_family_id, publication_number):
    """The family a publication belongs to. A publication with no family is its own family."""
    s = str(simple_family_id or "").strip()
    return publication_number if s.lower() in NO_FAMILY else s


# ---------------------------------------------------------------- the boundary
class Boundary:
    """The checked-in niche definition. Constructed from `config/niche_boundary.json`."""

    def __init__(self, spec):
        self.spec = spec
        self.release_prefix = spec.get("release_prefix", "niche")
        self.core_subclasses = frozenset(spec.get("core_subclasses") or ())
        self.adjacent_groups = frozenset(spec.get("adjacent_groups") or ())
        rule = spec.get("selection_rule") or {}
        self.min_support = int(rule.get("min_support", 100))
        self.min_density = float(rule.get("min_density", 0.03))
        cl = spec.get("closures") or {}
        self.family_closure = bool(cl.get("family", True))
        cit = cl.get("citation") or {}
        self.citation_closure = bool(cit.get("enabled", True))
        self.citation_categories = tuple(cit.get("categories") or ("SEA", "EXA", "ISR"))
        self.citation_origins = tuple(cit.get("origins") or ())
        self.citation_hops = int(cit.get("hops", 1))
        self.citation_direction = cit.get("direction", "both")

    @classmethod
    def load(cls, path):
        with open(path) as fh:
            return cls(json.load(fh))

    def sha256(self):
        blob = json.dumps(self.spec, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()

    def symbol_tier(self, symbol):
        """'core' | 'adjacent' | None for one classification symbol."""
        if is_indexing_code(symbol):
            return None
        if subclass_of(symbol) in self.core_subclasses:
            return "core"
        if main_group_of(symbol) in self.adjacent_groups:
            return "adjacent"
        return None

    def tier_of_symbols(self, symbols):
        """The strongest tier any of these symbols earns. None when nothing does."""
        tier = None
        for s in symbols or ():
            t = self.symbol_tier(s)
            if t == "core":
                return "core"
            if t == "adjacent":
                tier = "adjacent"
        return tier

    def shard_domains(self, include_unclassified=True):
        """Every shard domain the niche touches, in `shard_router.domain_of` spelling.

        `UNCLASSIFIED` is in the list by default because the niche really does contain unclassified
        families: the family and citation closures put 294,327 of them there, and a shard map built
        from the CPC nodes alone would silently drop every one.
        """
        doms = set(self.core_subclasses) | {subclass_of(g) for g in self.adjacent_groups}
        doms.discard(UNCLASSIFIED)
        if include_unclassified:
            doms.add(UNCLASSIFIED)
        return sorted(doms)

    def citation_admitted(self, category, origin):
        """Does this citation edge extend the niche?

        `category` is who supplied the citation (SEA / EXA / ISR from a search report, APP from the
        applicant's IDS). `origin` carries the relevance code (X, Y, A). The applicant's IDS is not
        a search result: one US patent in this corpus carries 5,771 APP citations against 11 from
        the search report. X and Y are the codes that threaten novelty and inventive step, and
        restricting to them is what keeps the closure a closure: MEASURED 2026-08-22, all examiner
        categories take the niche to 94.6% of the corpus and X/Y takes it to 62.2%.
        """
        c = str(category or "").upper()
        if not any(k in c for k in self.citation_categories):
            return False
        if not self.citation_origins:
            return True
        o = str(origin or "").upper()
        return any(k in o for k in self.citation_origins)


# ---------------------------------------------------------------- text state
def text_state(claims_chars, desc_chars):
    """(has_claims, has_description) for ONE publication, at the fetcher's own floors."""
    return (int(claims_chars or 0) >= MIN_CLAIMS_CHARS,
            int(desc_chars or 0) >= MIN_DESC_CHARS)


def best_source(countries, complete_member_exists):
    """The cheapest rung of the acquisition ladder that can serve this family.

    `countries` are the two-letter offices of the family's held publications. A family whose text
    already sits under a sibling publication number is a JOIN, not an acquisition, and saying so is
    the difference between a 2,000-document fetch and a 200,000-document one.
    """
    if complete_member_exists:
        return LOCAL_MEMBER
    have = {str(c or "").upper()[:2] for c in countries if c}
    for name, offices in SOURCE_LADDER:
        if not offices or have & set(offices):
            return name
    return SOURCE_LADDER[-1][0]


def build_record(family_id, members, symbols, rep_number=None):
    """One manifest record. `members` is a list of dicts, one per held publication:

        {"publication_number", "country", "title", "abstract", "claims_chars", "desc_chars"}

    Optional per member: `has_abstract`, for a caller that knows an abstract exists but has chosen
    not to carry its text (the enumeration holds only the representative's, which is what keeps a
    3.1M publication run inside memory). It defaults to whether `abstract` is non-empty.

    `symbols` is every classification symbol across the family. `rep_number` pins the
    representative; without it the most complete member wins, ties broken by publication number so
    a re-run is byte identical.
    """
    pubs = sorted({m["publication_number"] for m in members if m.get("publication_number")})
    has_claims = has_desc = complete = False
    for m in members:
        c, d = text_state(m.get("claims_chars"), m.get("desc_chars"))
        has_claims = has_claims or c
        has_desc = has_desc or d
        complete = complete or (c and d)
    rep = None
    if rep_number:
        rep = next((m for m in members if m.get("publication_number") == rep_number), None)
    if rep is None:
        rep = _representative(members)
    title = (rep.get("title") or "") if rep else ""
    abstract = (rep.get("abstract") or "") if rep else ""
    missing = []
    if not any((m.get("title") or "").strip() for m in members):
        missing.append("title")
    if not any(_has_abstract(m) for m in members):
        missing.append("abstract")
    if not has_claims:
        missing.append("claims")
    if not has_desc:
        missing.append("description")
    src = None
    if missing:
        src = best_source([m.get("country") for m in members], complete)
    return {
        "family_id": family_id,
        "publications": pubs,
        "cpc": sorted({normalise_symbol(s) for s in symbols if normalise_symbol(s)}),
        "title": title,
        "abstract": abstract,
        "has_claims": has_claims,
        "has_description": has_desc,
        "has_complete_text": complete,
        "best_source": src,
        "missing_fields": sorted(missing),
    }


def _has_abstract(m):
    if "has_abstract" in m:
        return bool(m["has_abstract"])
    return bool((m.get("abstract") or "").strip())


def _representative(members):
    """The member whose text is most complete, ties broken by publication number so a re-run of
    the enumeration produces byte-identical records."""
    if not members:
        return None

    def rank(m):
        c, d = text_state(m.get("claims_chars"), m.get("desc_chars"))
        return (int(c and d), int(d), int(c), int(_has_abstract(m)),
                1 if (m.get("title") or "").strip() else 0,
                _neg(m.get("publication_number") or ""))
    return max(members, key=rank)


class _neg:
    """Reverse ordering on a string, so `max` picks the LOWEST publication number on a tie."""
    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s

    def __lt__(self, other):
        return self.s > other.s

    def __eq__(self, other):
        return self.s == other.s


# ---------------------------------------------------------------- manifest writing
class ManifestWriter:
    """Append-only, crash-safe, readable while it is being written.

    A part file is written whole, flushed and checksummed BEFORE its name appears in `index.json`,
    and `index.json` is replaced atomically. A reader that trusts only `index.json` therefore never
    sees a partial part, which is what lets workstream C start against a partial release.
    """

    def __init__(self, directory, release_id, boundary, batch_size=50000):
        self.dir = directory
        self.release_id = release_id
        self.boundary = boundary
        self.batch_size = int(batch_size)
        self.parts = []
        self._buf = []
        self._n = 0
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "boundary.json"), "w") as fh:
            json.dump(boundary.spec, fh, indent=1, sort_keys=True)
        self.started_at = _now()
        self._write_index("in_progress")

    def add(self, record):
        self._buf.append(record)
        if len(self._buf) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self._buf:
            return
        name = f"part-{len(self.parts):05d}.jsonl"
        path = os.path.join(self.dir, name)
        h = hashlib.sha256()
        nbytes = 0
        with open(path, "w") as fh:
            for rec in self._buf:
                line = json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n"
                b = line.encode()
                h.update(b)
                nbytes += len(b)
                fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self.parts.append({
            "name": name, "families": len(self._buf), "bytes": nbytes,
            "sha256": h.hexdigest(),
            "first_family_id": self._buf[0]["family_id"],
            "last_family_id": self._buf[-1]["family_id"],
            "written_at": _now(),
        })
        self._n += len(self._buf)
        self._buf = []
        self._write_index("in_progress")

    def close(self):
        self.flush()
        self._write_index("complete")
        open(os.path.join(self.dir, "COMPLETE"), "w").write(self.release_id + "\n")

    def _write_index(self, state):
        idx = {
            "release_id": self.release_id,
            "manifest_version": MANIFEST_VERSION,
            "state": state,
            "boundary_sha256": self.boundary.sha256(),
            "started_at": self.started_at,
            "updated_at": _now(),
            "parts": self.parts,
            "totals": {"families": self._n},
        }
        tmp = os.path.join(self.dir, "index.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(idx, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(self.dir, "index.json"))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- manifest reading
def latest_release_dir(manifests_root):
    """The newest release directory, from the LATEST pointer, falling back to a name sort."""
    ptr = os.path.join(manifests_root, "LATEST")
    if os.path.exists(ptr):
        name = open(ptr).read().strip()
        path = os.path.join(manifests_root, name)
        if os.path.isdir(path):
            return path
    names = sorted(d for d in os.listdir(manifests_root)
                   if os.path.isdir(os.path.join(manifests_root, d)))
    return os.path.join(manifests_root, names[-1]) if names else None


def read_index(release_dir):
    with open(os.path.join(release_dir, "index.json")) as fh:
        return json.load(fh)


def read_manifest(release_dir, after_part=None):
    """Yield (part_name, record) for every part `index.json` currently names.

    Only parts listed in the index are opened: a part on disk but not in the index is still being
    written. Pass the last part name you consumed to resume.
    """
    idx = read_index(release_dir)
    started = after_part is None
    for p in idx.get("parts") or ():
        if not started:
            started = (p["name"] == after_part)
            continue
        with open(os.path.join(release_dir, p["name"])) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield p["name"], json.loads(line)


def follow(release_dir, poll=30.0, sleep=time.sleep):
    """Every record, including the ones written after this call, until the release closes."""
    last = None
    while True:
        for name, rec in read_manifest(release_dir, after_part=last):
            last = name
            yield rec
        if read_index(release_dir).get("state") == "complete":
            return
        sleep(poll)


def verify_part(release_dir, part):
    """True when the part on disk still hashes to what the index recorded."""
    h = hashlib.sha256()
    with open(os.path.join(release_dir, part["name"]), "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest() == part["sha256"]
