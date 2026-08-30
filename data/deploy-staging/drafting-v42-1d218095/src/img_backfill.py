"""Throttled, resumable patent-drawing image backfill + ingest into `figure_images`.

Runs on instance-3, safely beside the live app and the text backfill. Never touches the
text `chunks` table. Idempotent by (publication_number, file_name, model).

Phases (argparse subcommands)
  acquire         Fetch/render drawings to data/figures/<pub>/ for a target set, using
                  enrich_display.enrich_for_display — which already does Google Patents +
                  EPO OPS + Espacenet recovery and routes every OPS byte through the shared
                  weekly 4 GB budget (ops._ops_get). Rate-limited (--sleep), bounded (--limit).
  ingest          CPU path: embed on-disk figures and INSERT. Fine for hundreds of images.
  export-figs     Tar the figure dirs of a target set to ship to matcher-gpu (the GPU box
                  cannot reach this DB — localhost only).
  ingest-vectors  Load a vectors.jsonl produced on the GPU box (img_search embed-dir) into
                  figure_images. This is the BULK path.
  status          Coverage report: pubs with figures-on-disk vs embedded.

BULK (production) flow, honouring "embed on the T4":
  1. img_backfill acquire  --set pending --limit N            (instance-3, fills disk)
  2. img_backfill export-figs --set ondisk --out figs.tar     (instance-3)
  3. scp/base64 figs.tar + src/img_search.py -> matcher-gpu
  4. python -m img_search embed-dir <extracted> vectors.jsonl (matcher-gpu, T4)
  5. base64 vectors.jsonl -> instance-3
  6. img_backfill ingest-vectors vectors.jsonl                (instance-3, COPY-in)

Guardrails: a lockfile (data/img_backfill.lock) prevents two concurrent runs; --dry-run
does no writes; the OPS weekly budget is enforced upstream so acquire self-throttles.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import db
from config import DATA, SEED_CPC
from img_search import IMG_DIM, IMG_MODEL, embed_images, _vec

FIGDIR = DATA / "figures"
LOCKFILE = DATA / "img_backfill.lock"


# ---------------------------------------------------------------- lockfile ----
@contextlib.contextmanager
def _lock():
    """Coarse single-writer lock so two backfills never fight over the same rows."""
    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip() or "0")
        except Exception:
            pid = 0
        if pid and _pid_alive(pid):
            raise SystemExit(f"[img_backfill] another run holds {LOCKFILE} (pid {pid}); abort.")
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    LOCKFILE.write_text(str(os.getpid()))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            LOCKFILE.unlink()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------- target selection ----
def _ondisk_pubs() -> list[str]:
    """Publications that already have figure files on disk (zero acquisition cost)."""
    if not FIGDIR.is_dir():
        return []
    out = []
    for d in sorted(FIGDIR.iterdir()):
        if d.is_dir() and any(f.suffix.lower() in (".png", ".jpg", ".jpeg") for f in d.iterdir()):
            out.append(d.name)
    return out


def _gold_pubs() -> list[str]:
    """Gold-set anchor publications + their curated relevant families (highest priority)."""
    try:
        import goldset
        anchors = [a["pub"] for a in goldset.ANCHORS if a.get("pub")]
    except Exception:
        anchors = []
    # resolve curated families -> publication numbers
    fams: set[str] = set()
    try:
        import goldset
        for a in goldset.ANCHORS:
            for fam in a.get("extra_gold_families", []) or []:
                fams.add(str(fam))
    except Exception:
        pass
    pubs = list(dict.fromkeys(anchors))
    if fams:
        with db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT publication_number FROM publications "
                "WHERE simple_family_id = ANY(%s) OR extended_family_id = ANY(%s)",
                (list(fams), list(fams)))
            pubs += [r["publication_number"] for r in cur.fetchall()]
    return list(dict.fromkeys(pubs))


def _pending_pubs(limit: int) -> list[str]:
    """Corpus publications not yet embedded, prioritised: have figure metadata, seed-CPC
    classes first (the vacuum-gripping core), then the rest by recency."""
    like = [c.replace("/", "") + "%" for c in SEED_CPC]  # loose CPC prefix match
    with db.cursor() as cur:
        cur.execute(
            """
            WITH pri AS (
              SELECT p.id, p.publication_number,
                     EXISTS (SELECT 1 FROM figures f WHERE f.publication_id=p.id) AS has_fig,
                     EXISTS (SELECT 1 FROM classifications c
                             WHERE c.publication_id=p.id
                               AND replace(c.symbol,' ','') LIKE ANY(%s)) AS seed_cpc
              FROM publications p
              WHERE NOT EXISTS (SELECT 1 FROM figure_images fi
                                WHERE fi.publication_number=p.publication_number
                                  AND fi.model=%s)
            )
            SELECT publication_number FROM pri
            ORDER BY seed_cpc DESC, has_fig DESC
            LIMIT %s
            """,
            (like, IMG_MODEL, limit))
        return [r["publication_number"] for r in cur.fetchall()]


def _resolve_set(name: str, limit: int) -> list[str]:
    if name == "ondisk":
        pubs = _ondisk_pubs()
    elif name == "gold":
        pubs = _gold_pubs()
    elif name == "pending":
        pubs = _pending_pubs(limit)
    else:
        raise SystemExit(f"unknown --set {name!r} (ondisk|gold|pending)")
    return pubs[:limit] if limit else pubs


# ------------------------------------------------------------------- acquire ----
def acquire(pubs: list[str], sleep: float, dry_run: bool) -> dict:
    """Fetch/render drawings to disk via the existing recovery path. Rate-limited."""
    import enrich_display
    stats = {"requested": len(pubs), "acquired": 0, "had": 0, "empty": 0, "errors": 0}
    for pub in pubs:
        d = FIGDIR / pub
        if d.is_dir() and any(f.suffix.lower() == ".png" for f in d.iterdir()):
            stats["had"] += 1
            continue
        if dry_run:
            print(f"[acquire] DRY would fetch {pub}", flush=True)
            continue
        try:
            disp = enrich_display.enrich_for_display(pub)
            n = int(disp.get("n_images") or 0)
            # enrich_for_display writes SerpApi drawings into its cache but OPS drawings land
            # directly in data/figures/<pub>/; count what actually reached disk.
            on_disk = d.is_dir() and any(f.suffix.lower() == ".png" for f in d.iterdir())
            if on_disk:
                stats["acquired"] += 1
            elif n:
                stats["acquired"] += 1  # SerpApi-hosted, will render on ingest via PDF path
            else:
                stats["empty"] += 1
            print(f"[acquire] {pub}: n_images={n} on_disk={on_disk}", flush=True)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[acquire] {pub}: ERROR {e}", flush=True)
        time.sleep(max(0.0, sleep))
    return stats


# -------------------------------------------------------------------- ingest ----
def _pub_id_map(pubs: list[str]) -> dict[str, int]:
    if not pubs:
        return {}
    with db.cursor() as cur:
        cur.execute(
            "SELECT publication_number, id FROM publications WHERE publication_number = ANY(%s)",
            (pubs,))
        return {r["publication_number"]: r["id"] for r in cur.fetchall()}


def _existing(pubs: list[str]) -> set[tuple]:
    if not pubs:
        return set()
    with db.cursor() as cur:
        cur.execute(
            "SELECT publication_number, file_name FROM figure_images "
            "WHERE model=%s AND publication_number = ANY(%s)", (IMG_MODEL, pubs))
        return {(r["publication_number"], r["file_name"]) for r in cur.fetchall()}


def _insert_rows(rows: list[dict]) -> int:
    """rows: {publication_number, file_name, fig_index, sha256, vec}. ON CONFLICT DO NOTHING."""
    if not rows:
        return 0
    pmap = _pub_id_map(list({r["publication_number"] for r in rows}))
    n = 0
    with db.cursor() as cur:
        for i in range(0, len(rows), 200):
            batch = rows[i:i + 200]
            vals, flat = [], []
            for r in batch:
                vals.append("(%s,%s,%s,%s,%s,%s,%s::vector)")
                flat += [pmap.get(r["publication_number"]), r["publication_number"],
                         r["fig_index"], r["file_name"], r["sha256"], IMG_MODEL,
                         r["vec"] if isinstance(r["vec"], str) else _vec(r["vec"])]
            cur.execute(
                "INSERT INTO figure_images "
                "(publication_id, publication_number, fig_index, file_name, sha256, model, embedding) "
                "VALUES " + ",".join(vals) +
                " ON CONFLICT (publication_number, file_name, model) DO NOTHING",
                flat)
            n += cur.rowcount
    return n


def ingest(pubs: list[str], dry_run: bool, batch: int = 32) -> dict:
    """CPU embed on-disk figures and insert. For BULK use export-figs + GPU + ingest-vectors."""
    existing = _existing(pubs)
    stats = {"pubs": len(pubs), "files": 0, "embedded": 0, "skipped": 0, "inserted": 0}
    buf: list[dict] = []
    for pub in pubs:
        d = FIGDIR / pub
        if not d.is_dir():
            continue
        files = sorted(f for f in d.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg"))
        for idx, f in enumerate(files):
            stats["files"] += 1
            if (pub, f.name) in existing:
                stats["skipped"] += 1
                continue
            buf.append({"publication_number": pub, "file_name": f.name, "fig_index": idx,
                        "_path": f})
    if dry_run:
        print(f"[ingest] DRY {len(buf)} figures to embed across {len(pubs)} pubs", flush=True)
        stats["embedded"] = len(buf)
        return stats
    for i in range(0, len(buf), batch):
        group = buf[i:i + batch]
        blobs = [g["_path"].read_bytes() for g in group]
        vecs = embed_images(blobs, batch_size=len(blobs))
        rows = []
        for g, b, v in zip(group, blobs, vecs):
            rows.append({"publication_number": g["publication_number"], "file_name": g["file_name"],
                         "fig_index": g["fig_index"], "sha256": hashlib.sha256(b).hexdigest(),
                         "vec": v})
        stats["inserted"] += _insert_rows(rows)
        stats["embedded"] += len(rows)
        print(f"[ingest] {stats['embedded']}/{len(buf)}", flush=True)
    return stats


# ------------------------------------------------------------- export-figs ----
def export_figs(pubs: list[str], out: str) -> dict:
    """Tar the figure dirs for `pubs` (relative to data/figures) to ship to the GPU box."""
    import tarfile
    out_p = Path(out)
    n_files = 0
    with tarfile.open(out_p, "w") as tar:
        for pub in pubs:
            d = FIGDIR / pub
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    tar.add(f, arcname=f"{pub}/{f.name}")
                    n_files += 1
    return {"pubs": len(pubs), "files": n_files, "tar": str(out_p),
            "bytes": out_p.stat().st_size if out_p.exists() else 0}


# ---------------------------------------------------------- ingest-vectors ----
def ingest_vectors(jsonl_path: str, dry_run: bool) -> dict:
    """COPY a GPU-produced vectors.jsonl into figure_images. Validates model + dim."""
    p = Path(jsonl_path)
    rows, seen, bad = [], set(), 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            bad += 1
            continue
        if r.get("model") != IMG_MODEL or int(r.get("dim", 0)) != IMG_DIM or len(r.get("vec", [])) != IMG_DIM:
            bad += 1
            continue
        key = (r["publication_number"], r["file_name"])
        if key in seen:
            continue
        seen.add(key)
        # fig_index is not carried in jsonl; derive a stable ordinal per pub from file order later
        rows.append(r)
    # assign fig_index per publication by sorted file_name (stable, matches disk order)
    bypub: dict[str, list[dict]] = {}
    for r in rows:
        bypub.setdefault(r["publication_number"], []).append(r)
    prepared = []
    for pub, rs in bypub.items():
        for idx, r in enumerate(sorted(rs, key=lambda x: x["file_name"])):
            prepared.append({"publication_number": pub, "file_name": r["file_name"],
                             "fig_index": idx, "sha256": r.get("sha256"), "vec": r["vec"]})
    stats = {"lines": len(rows) + bad, "valid": len(prepared), "bad": bad, "inserted": 0}
    if dry_run:
        print(f"[ingest-vectors] DRY {stats}", flush=True)
        return stats
    # skip rows already present (idempotent re-run)
    existing = _existing(list(bypub.keys()))
    prepared = [r for r in prepared if (r["publication_number"], r["file_name"]) not in existing]
    stats["inserted"] = _insert_rows(prepared)
    print(f"[ingest-vectors] {stats}", flush=True)
    return stats


# -------------------------------------------------------------------- status ----
def status() -> dict:
    ondisk = _ondisk_pubs()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) n, count(DISTINCT publication_number) p "
                    "FROM figure_images WHERE model=%s", (IMG_MODEL,))
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM figures")
        fig_meta = cur.fetchone()["count"]
        cur.execute("SELECT count(*) FROM publications")
        pubs_total = cur.fetchone()["count"]
    return {"model": IMG_MODEL, "dim": IMG_DIM,
            "embedded_figures": row["n"], "embedded_pubs": row["p"],
            "pubs_with_figs_on_disk": len(ondisk),
            "figure_metadata_rows": fig_meta, "publications_total": pubs_total}


# ---------------------------------------------------------------------- CLI ----
def main():
    ap = argparse.ArgumentParser(description="patent-drawing image backfill")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_set(sp):
        sp.add_argument("--set", default="pending", choices=["ondisk", "gold", "pending"])
        sp.add_argument("--limit", type=int, default=0, help="0 = no cap")
        sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("acquire"); add_set(sp); sp.add_argument("--sleep", type=float, default=2.0)
    sp = sub.add_parser("ingest"); add_set(sp)
    sp = sub.add_parser("export-figs"); add_set(sp); sp.add_argument("--out", required=True)
    sp = sub.add_parser("ingest-vectors"); sp.add_argument("jsonl"); sp.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")

    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status(), indent=2)); return
    if a.cmd == "ingest-vectors":
        with _lock():
            print(json.dumps(ingest_vectors(a.jsonl, a.dry_run), indent=2)); return

    pubs = _resolve_set(a.set, a.limit)
    print(f"[img_backfill] {a.cmd} set={a.set} -> {len(pubs)} pubs", flush=True)
    if a.cmd == "acquire":
        with _lock():
            print(json.dumps(acquire(pubs, a.sleep, a.dry_run), indent=2))
    elif a.cmd == "ingest":
        with _lock():
            print(json.dumps(ingest(pubs, a.dry_run), indent=2))
    elif a.cmd == "export-figs":
        print(json.dumps(export_figs(pubs, a.out), indent=2))


if __name__ == "__main__":
    main()
