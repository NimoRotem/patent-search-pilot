"""Checkpoint artifacts: written atomically, recorded with a digest, refused when they disagree.

WHY A DIGEST AND NOT JUST A PATH
--------------------------------
A checkpoint that names a file is worth exactly what the file is worth. The failure this exists to
catch is not a MISSING artifact, which announces itself, it is a PRESENT one that is half a file: a
worker SIGKILLed between the write and the close, a disk that filled during the write, a page cache
that was never flushed before the box lost power. Reloading a fragment is worse than having no
checkpoint at all, because the run then finishes and publishes a report assembled from it, and
nothing in the output says so.

So every artifact goes through `write()`: serialise, write to a temp file IN THE SAME DIRECTORY,
`flush` + `fsync` the file, `os.replace` it into place, then `fsync` the directory so the rename
itself is durable. `write()` returns the sha256 of the bytes that landed, and the caller records
that digest WITH the checkpoint.

On resume `read(path, sha256)` hashes the bytes back and returns None on any disagreement. None
means "there is no checkpoint here": the caller redoes the work. It never means "close enough".

EVERY FUNCTION HERE IS SAFE TO CALL WITH NO DURABLE RUN. `write` is just an atomic write and
`read` of an unknown digest is just a read, so the gold-set runner and the test suite behave as
they always did.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path


def digest_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def serialise(payload, *, indent=None):
    """The exact bytes an artifact is made of. One place, so the digest a writer records and the
    digest a reader recomputes can never be produced by two different serialisations."""
    return json.dumps(payload, default=str, indent=indent).encode("utf-8")


def write_bytes(path, blob):
    """Put `blob` at `path` atomically and durably. -> its sha256.

    The temp file is a sibling, so `os.replace` is a rename inside one directory and therefore
    atomic. The two fsyncs are the difference between "the rename is in the page cache" and "the
    rename survives the power going out", which is the case this whole module is about.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            #  A filesystem that will not let us fsync a directory still gave us the atomic
            #  rename, which is the ordering guarantee the digest depends on.
            pass
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return digest_bytes(blob)


def write(path, payload, *, indent=None):
    """Serialise `payload` and publish it atomically. -> the sha256 to record with the checkpoint."""
    return write_bytes(path, serialise(payload, indent=indent))


def digest_of(path):
    """The sha256 of what is on disk now, or None when there is nothing readable there."""
    try:
        return digest_bytes(Path(path).read_bytes())
    except OSError:
        return None


def read(path, sha256=None):
    """The artifact at `path`, or None when it is absent, unreadable or not what was recorded.

    A digest of None means the caller has no recorded digest to check against, so the content is
    returned if it parses. That is the weaker contract and it is only for artifacts written before
    digests existed; anything this module writes records one.
    """
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    if sha256 and digest_bytes(blob) != str(sha256):
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:                                                # noqa: BLE001
        #  Unparseable is the same answer as absent: there is no checkpoint here.
        return None


def verify(path, sha256):
    """Whether the artifact at `path` is exactly the one whose digest was recorded."""
    if not sha256:
        return False
    return digest_of(path) == str(sha256)
