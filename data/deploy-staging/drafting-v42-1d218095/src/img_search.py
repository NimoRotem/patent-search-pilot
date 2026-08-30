"""Patent-drawing image-similarity search — shared embedding model + query path.

MODEL (pinned): facebook/dinov2-base — ViT-B/14, 768-d CLS embedding (IMG_MODEL/IMG_DIM).

Why DINOv2 over CLIP for this job
  Patent figures are abstract LINE DRAWINGS, not natural photos. CLIP is trained on
  image<->caption pairs and leans on texture/semantics that line art largely lacks;
  DINOv2 is self-supervised and captures STRUCTURE / SHAPE, which is exactly the signal
  that makes two vacuum-gripper cross-sections "look alike". matcher-gpu's own stack pairs
  DINOv2 with CLIP for the same reason and DINOv2 carries the structural load.

Why the -base (768-d) variant, not the -large one matcher-gpu already has
  The QUERY path must embed ONE drawing on instance-3's CPU under a tight RAM budget.
  dinov2-base is ~86M params (~350 MB resident, ~200-400 ms/img on CPU); dinov2-large is
  ~300M (~1.2 GB, seconds/img) — too heavy to keep resident in a gunicorn worker on a
  15 GB box already running a 6 GB text HNSW. base is trivially installable on both boxes
  via HuggingFace transformers (already present on instance-3 AND matcher-gpu).

The SAME IMG_MODEL id embeds the corpus (GPU bulk) and the query (CPU) so the vectors are
comparable — the id is pinned here so corpus and query can never drift. Change it and the
whole figure_images table must be re-embedded.

Failure is LOUD, never silent: an empty index or a model that will not load RAISES
(ImageIndexEmpty / ImageModelError) so the search channel can never return [] that a caller
misreads as "no similar drawings" — the dark-failure this codebase has hit repeatedly.

Standalone on the GPU box: the module-level imports are stdlib only; torch/transformers/
PIL and `db` are imported lazily inside functions, so `embed_dir_to_jsonl` (the bulk GPU
entry point) runs on matcher-gpu with no patents-app DB/config present.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading

IMG_MODEL = "facebook/dinov2-base"   # pinned — corpus & query MUST share this
IMG_DIM = 768                        # dinov2-base hidden size

_MODEL = None
_PROC = None
_DEVICE = None
_LOCK = threading.Lock()

_QCACHE: dict[str, list[float]] = {}   # sha256(image) -> vector; query drawings repeat across a report
_QCACHE_LOCK = threading.Lock()
_QCACHE_MAX = 512


class ImageSearchError(RuntimeError):
    """Base for any visible image-search failure."""


class ImageIndexEmpty(ImageSearchError):
    """The figure_images index has no rows for IMG_MODEL — the channel is dark."""


class ImageModelError(ImageSearchError):
    """The embedding model could not be loaded or run."""


def _load():
    """Lazy, thread-safe, process-wide model load. Raises ImageModelError on failure."""
    global _MODEL, _PROC, _DEVICE
    if _MODEL is not None:
        return
    with _LOCK:
        if _MODEL is not None:
            return
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModel
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            proc = AutoImageProcessor.from_pretrained(IMG_MODEL)
            model = AutoModel.from_pretrained(IMG_MODEL).to(dev).eval()
            # keep CPU inference from oversubscribing the RAM-constrained box
            if dev == "cpu":
                try:
                    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
                except Exception:
                    pass
            _MODEL, _PROC, _DEVICE = model, proc, dev
        except Exception as e:  # noqa: BLE001 — surfaced as a typed, visible error
            raise ImageModelError(f"failed to load image model {IMG_MODEL}: {e}") from e


def device() -> str:
    _load()
    return _DEVICE or "cpu"


def _prep(blob: bytes):
    """Bytes -> square, white-padded RGB PIL image.

    Patent sheets are tall/narrow or wide; a plain resize+centre-crop (the processor's
    default) would distort or clip the figure. Padding to a square on white preserves the
    whole drawing and its aspect ratio, applied identically to corpus and query.
    """
    from PIL import Image
    im = Image.open(io.BytesIO(blob)).convert("RGB")
    w, h = im.size
    s = max(w, h)
    if w == h:
        return im
    canvas = Image.new("RGB", (s, s), (255, 255, 255))
    canvas.paste(im, ((s - w) // 2, (s - h) // 2))
    return canvas


def embed_images(blobs, batch_size: int = 32) -> list[list[float]]:
    """Embed a list of PNG/JPG byte blobs -> list of L2-normalised 768-d vectors.

    Batched; used for GPU bulk and for multi-sketch queries. Raises ImageModelError if the
    model will not load. Individual undecodable images raise (callers filter upstream).
    """
    if not blobs:
        return []
    _load()
    import torch
    out: list[list[float]] = []
    for i in range(0, len(blobs), batch_size):
        chunk = blobs[i:i + batch_size]
        imgs = [_prep(b) for b in chunk]
        inputs = _PROC(images=imgs, return_tensors="pt").to(_DEVICE)
        with torch.no_grad():
            o = _MODEL(**inputs)
        pooled = getattr(o, "pooler_output", None)
        cls = pooled if pooled is not None else o.last_hidden_state[:, 0]
        cls = torch.nn.functional.normalize(cls, dim=-1)
        out.extend(cls.cpu().tolist())
    return out


def embed_image(blob: bytes) -> list[float]:
    """Embed one drawing, memoised by image sha256 (query drawings repeat within a report)."""
    key = hashlib.sha256(blob).hexdigest()
    with _QCACHE_LOCK:
        v = _QCACHE.get(key)
    if v is not None:
        return v
    v = embed_images([blob])[0]
    with _QCACHE_LOCK:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()
        _QCACHE[key] = v
    return v


def _vec(e) -> str:
    """pgvector literal, same format the text pipeline uses."""
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


def index_count() -> int:
    """Rows embedded with the CURRENT model. 0 => the channel is dark."""
    import db
    return db.scalar("SELECT count(*) FROM figure_images WHERE model=%s", (IMG_MODEL,)) or 0


def _rows_to_pubs(rows, k):
    """Collapse figure hits to distinct publications, keeping each pub's best figure."""
    best: dict[str, dict] = {}
    for r in rows:
        p = r["publication_number"]
        if p is None:
            continue
        if p not in best or r["score"] > best[p]["score"]:
            best[p] = r
    ranked = sorted(best.values(), key=lambda r: -r["score"])[:k]
    return [{
        "publication_number": r["publication_number"],
        "publication_id": r["publication_id"],
        "score": round(float(r["score"]), 4),
        "fig_file": r["file_name"],
        "fig_index": r["fig_index"],
        "source": "image",           # results-page source tag
    } for r in ranked]


def search_by_image(blob: bytes, k: int = 10, pool: int = 200) -> list[dict]:
    """Top-K corpus publications whose drawings are most similar to ONE query drawing.

    Returns [{publication_number, publication_id, score(cosine 0..1), fig_file, fig_index,
    source='image'}] collapsed to distinct publications, best-first.

    RAISES ImageIndexEmpty if nothing is embedded and ImageModelError if the model fails —
    never a silent [].
    """
    n = index_count()
    if n == 0:
        raise ImageIndexEmpty(
            f"figure_images has 0 rows for model {IMG_MODEL}; the image-search channel is "
            "not built yet (run img_backfill). Refusing to return an empty result that "
            "would read as 'no similar drawings'.")
    qv = _vec(embed_image(blob))       # ImageModelError propagates
    k = max(1, min(int(k), 100))
    pool = max(k, min(int(pool), 1000))
    import db
    with db.cursor() as cur:
        cur.execute(
            "SELECT publication_id, publication_number, fig_index, file_name, "
            "       1 - (embedding <=> %s::vector) AS score "
            "FROM figure_images WHERE model=%s AND embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, IMG_MODEL, qv, pool))
        rows = cur.fetchall()
    return _rows_to_pubs(rows, k)


def search_by_images(blobs, k: int = 10, pool: int = 200) -> list[dict]:
    """Multi-sketch query: fan over several query drawings, keep the MAX score per pub.

    Used when an input document carries several figures — a corpus pub that matches ANY of
    them should surface. Raises the same errors as search_by_image.
    """
    blobs = [b for b in (blobs or []) if b]
    if not blobs:
        return []
    if index_count() == 0:
        raise ImageIndexEmpty(
            f"figure_images has 0 rows for model {IMG_MODEL}; image-search channel not built.")
    agg: dict[str, dict] = {}
    for b in blobs:
        for r in search_by_image(b, k=k, pool=pool):
            p = r["publication_number"]
            if p not in agg or r["score"] > agg[p]["score"]:
                agg[p] = r
    return sorted(agg.values(), key=lambda r: -r["score"])[:k]


# --------------------------------------------------------------------------------------
# GPU bulk entry point — NO DB. Walk figure dirs, embed, write vectors.jsonl.
# --------------------------------------------------------------------------------------
_IMG_EXT = (".png", ".jpg", ".jpeg", ".ppm")


def embed_dir_to_jsonl(figures_root, out_path, batch: int = 32, progress_every: int = 200) -> dict:
    """Embed every figure under <figures_root>/<publication_number>/*.png -> JSONL rows.

    Row = {publication_number, file_name, sha256, model, dim, vec}. Resumable: rows already
    present in out_path (by publication_number+file_name) are skipped. Runs on matcher-gpu
    (GPU auto-detected) with only torch/transformers/PIL — no patents DB needed.
    """
    from pathlib import Path
    root = Path(figures_root)
    out_path = Path(out_path)
    done: set[tuple] = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                r = json.loads(line)
                done.add((r["publication_number"], r["file_name"]))
            except Exception:
                continue
    tasks = []
    for pubdir in sorted(root.iterdir()):
        if not pubdir.is_dir():
            continue
        pub = pubdir.name
        for f in sorted(pubdir.iterdir()):
            if f.suffix.lower() in _IMG_EXT and (pub, f.name) not in done:
                tasks.append((pub, f))
    stats = {"total_files": len(tasks) + len(done), "embedded": 0, "skipped": len(done), "errors": 0}
    print(f"[embed-dir] {len(tasks)} to embed, {len(done)} already done, device={device()}",
          flush=True)
    with out_path.open("a") as fh:
        for i in range(0, len(tasks), batch):
            group = tasks[i:i + batch]
            blobs, metas = [], []
            for pub, f in group:
                try:
                    blobs.append(f.read_bytes())
                    metas.append((pub, f.name))
                except Exception:
                    stats["errors"] += 1
            if not blobs:
                continue
            try:
                vecs = embed_images(blobs, batch_size=len(blobs))
            except Exception as e:  # noqa: BLE001
                stats["errors"] += len(blobs)
                print(f"[embed-dir] batch error: {e}", flush=True)
                continue
            for (pub, name), blob, v in zip(metas, blobs, vecs):
                fh.write(json.dumps({
                    "publication_number": pub,
                    "file_name": name,
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "model": IMG_MODEL,
                    "dim": IMG_DIM,
                    "vec": [round(x, 6) for x in v],
                }) + "\n")
                stats["embedded"] += 1
            fh.flush()
            if stats["embedded"] % progress_every < batch:
                print(f"[embed-dir] {stats['embedded']}/{len(tasks)}", flush=True)
    print(f"[embed-dir] done: {stats}", flush=True)
    return stats


def self_test() -> dict:
    """Cheap liveness check: model loads, embeds a synthetic image, index reachable."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (255, 255, 255)).save(buf, "PNG")
    v = embed_image(buf.getvalue())
    ok = len(v) == IMG_DIM and abs(sum(x * x for x in v) - 1.0) < 1e-3
    out = {"model": IMG_MODEL, "dim": len(v), "device": device(), "l2_normalised": ok}
    try:
        out["index_count"] = index_count()
    except Exception as e:  # noqa: BLE001 — DB may be absent on the GPU box
        out["index_count"] = f"unavailable ({e})"
    return out


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args or args[0] == "selftest":
        print(json.dumps(self_test(), indent=2))
    elif args[0] == "embed-dir":
        # img_search embed-dir <figures_root> <out.jsonl> [batch]
        b = int(args[3]) if len(args) > 3 else 32
        print(json.dumps(embed_dir_to_jsonl(args[1], args[2], batch=b), indent=2))
    elif args[0] == "search":
        # img_search search <image_path> [k]
        with open(args[1], "rb") as fh:
            blob = fh.read()
        k = int(args[2]) if len(args) > 2 else 10
        for r in search_by_image(blob, k=k):
            print(f"{r['score']:.4f}  {r['publication_number']:24s}  {r['fig_file']}")
    else:
        print("usage: img_search [selftest | embed-dir <root> <out.jsonl> [batch] | "
              "search <image> [k]]")
