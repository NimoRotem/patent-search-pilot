"""The release manifest: what is in a release, what it was built from, and its checksums.

A RELEASE IS IDENTIFIED, CONTENT-ADDRESSED AND NEVER EDITED. `release_id` is the human name
(`domain_01_v1`); `content_hash` is a sha256 over the canonical manifest body and is the identity
that matters, because it is the one a shard can recompute for itself.

WHAT THE HASH DELIBERATELY DOES NOT COVER. `built_at`, `builder`, `timings` and `host` are in the
manifest and are excluded from the hash. Content addressing whose value changes when the clock
changes is not content addressing: two builders handed the same staged input have to produce the
same hash, otherwise the hash proves nothing about what a shard is serving and only proves that
somebody ran a build. `HASH_EXCLUDES` is part of the format and is written into the manifest, so a
reader can see what was covered rather than having to trust this docstring.

WHAT A SHARD CHECKS. `verify()` answers one question, "am I serving the release I think I am", with
four independent checks: the manifest hash recomputes; every artifact on disk has the sha256 the
manifest recorded; the row counts in the database match the counts the manifest recorded; and the
index parameters the database reports match the ones the manifest recorded. Any one of those
failing is a shard that must not join the fan-out.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time

SCHEMA = "corpus-release/1"

#  Excluded from the hash. Everything else in the manifest is covered.
HASH_EXCLUDES = ("content_hash", "built_at", "builder", "timings", "hash_excludes")


def canonical_bytes(manifest):
    """The exact bytes that get hashed. Sorted keys, no incidental whitespace, ASCII escaped.

    ensure_ascii is on deliberately: a manifest that hashes differently depending on the writer's
    locale or Python version is worse than no hash at all.
    """
    body = {k: v for k, v in (manifest or {}).items() if k not in HASH_EXCLUDES}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str).encode("utf-8")


def content_hash(manifest):
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def artifact(path, name=None):
    """A file the release ships, as a manifest entry. The name is relative, so a snapshot that is
    restored under a different root still verifies."""
    return {"name": name or os.path.basename(path), "bytes": os.path.getsize(path),
            "sha256": sha256_file(path)}


def _git_rev(root):
    try:
        return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()[:40]
    except Exception:
        return ""


def builder_identity(root="."):
    return {"host": socket.gethostname(), "user": os.environ.get("USER", ""),
            "git": _git_rev(root), "pid": os.getpid()}


def build(*, release_id, shard_key, version, kind, domains, counts, built_from, index_params,
          artifacts=(), stats=None, timings=None, root=".", note=""):
    """Assemble a manifest and stamp its content hash.

    The hash is computed last and over everything except `HASH_EXCLUDES`, so adding a field to the
    manifest changes the hash, which is the intended behaviour: a release built by a different
    version of this builder is a different release.
    """
    m = {
        "schema": SCHEMA,
        "release_id": str(release_id),
        "shard_key": str(shard_key),
        "version": int(version),
        "kind": str(kind),
        "note": str(note or ""),
        "domains": sorted(str(d) for d in (domains or [])),
        "counts": dict(counts or {}),
        "built_from": dict(built_from or {}),
        "index_params": dict(index_params or {}),
        "artifacts": sorted((dict(a) for a in (artifacts or [])), key=lambda a: a["name"]),
        "stats": dict(stats or {}),
        "hash_excludes": list(HASH_EXCLUDES),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "builder": builder_identity(root),
        "timings": dict(timings or {}),
    }
    m["content_hash"] = content_hash(m)
    return m


class Verification:
    """A verdict with its reasons. `ok` is the only thing a caller should branch on; `checks`
    is what a human reads when it is False."""

    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail=""):
        #  The detail is the reason a check FAILED. Callers build it eagerly, so a passing check
        #  would otherwise read "ok: true, detail: observed 768 != manifest 768", which is a
        #  contradiction an operator has to stop and parse in the middle of a cutover.
        self.checks.append({"check": name, "ok": bool(ok),
                            "detail": "" if ok else str(detail)})
        return self

    @property
    def ok(self):
        return all(c["ok"] for c in self.checks)

    @property
    def failures(self):
        return [c for c in self.checks if not c["ok"]]

    def to_dict(self):
        return {"ok": self.ok, "checks": self.checks}

    def __repr__(self):
        return f"<Verification ok={self.ok} failures={len(self.failures)}>"


def verify(manifest, *, artifact_root=None, observed_counts=None, observed_index_params=None):
    """Is this release what its manifest says it is?

    `artifact_root`         directory the manifest's artifacts were restored into. None skips the
                            file checks, which is what a caller that only has the database does.
    `observed_counts`       what the database actually holds, e.g. {"chunks": n, "families": n}.
    `observed_index_params` what the database actually reports for the dense index.
    """
    v = Verification()
    if not manifest:
        return v.add("manifest_present", False, "no manifest")
    v.add("schema", manifest.get("schema") == SCHEMA,
          f"{manifest.get('schema')!r} != {SCHEMA!r}")
    recomputed = content_hash(manifest)
    v.add("content_hash", recomputed == manifest.get("content_hash"),
          f"recomputed {recomputed} != recorded {manifest.get('content_hash')}")

    if artifact_root is not None:
        for a in manifest.get("artifacts") or []:
            p = os.path.join(artifact_root, a["name"])
            if not os.path.exists(p):
                v.add(f"artifact:{a['name']}", False, "missing")
                continue
            size = os.path.getsize(p)
            if size != a.get("bytes"):
                v.add(f"artifact:{a['name']}", False, f"{size} bytes != {a.get('bytes')}")
                continue
            got = sha256_file(p)
            v.add(f"artifact:{a['name']}", got == a.get("sha256"),
                  f"{got} != {a.get('sha256')}")

    if observed_counts is not None:
        want = manifest.get("counts") or {}
        for key in ("chunks", "families", "publications"):
            if key in want:
                got = observed_counts.get(key)
                v.add(f"count:{key}", got == want[key], f"observed {got} != manifest {want[key]}")

    if observed_index_params is not None:
        want = (manifest.get("index_params") or {}).get("dense") or {}
        got = dict(observed_index_params or {})
        for key in ("type", "m", "ef_construction", "opclass", "dim"):
            if key in want:
                v.add(f"index_param:{key}", got.get(key) == want[key],
                      f"observed {got.get(key)!r} != manifest {want[key]!r}")
    return v


def write(manifest, path):
    """Write the manifest beside its snapshot. Atomic: a half-written manifest is a release nobody
    can verify, and a truncated file is exactly what a crash mid-write leaves."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
