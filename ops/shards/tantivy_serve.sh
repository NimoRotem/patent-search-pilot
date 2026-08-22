#!/bin/bash
# The Tantivy seam. Workstream C owns what is indexed and what the server is; this owns that
# something is listening on the port, that it answers /health, and that its answer is honest.
#
# Drop the real server at /opt/patents-shard/tantivy/bin/tantivy-serve and it is used from the
# next restart with no unit change. Until then the stub holds the port and reports `absent`, which
# is the truthful state and the one that keeps `lexical.register_backend`'s available() gate
# closed. A closed gate degrades to the Postgres lexical backend, which is the existing fail soft
# contract; a port with nothing on it degrades to a connection error on every query.
set -euo pipefail
REAL=/opt/patents-shard/tantivy/bin/tantivy-serve
if [ -x "$REAL" ]; then
  exec "$REAL" --port "${SHARD_TANTIVY_PORT:-8635}" --index /opt/patents-shard/tantivy/index
fi
exec /usr/bin/python3 /opt/patents-shard/bin/tantivy_stub.py
