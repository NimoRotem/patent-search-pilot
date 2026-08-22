#!/bin/bash
# The Tantivy seam. Workstream C owns the schema and what is indexed; this owns that a real
# Tantivy is running on the port, that it answers /health, and that its answer is honest.
#
# Drop a compiled server at /opt/patents-shard/tantivy/bin/tantivy-serve and it is used from the
# next restart with no unit change. Otherwise tantivy_server.py runs, which is the Python
# extension module unpacked by bootstrap.sh: a real Tantivy index reader, not a placeholder. When
# no index has been built yet it reports `absent` and `available: false`, which is the truthful
# state and the one that keeps `lexical.register_backend`'s available() gate closed. A closed gate
# degrades to the Postgres lexical backend, which is the existing fail soft contract; a port with
# nothing on it degrades to a connection error on every query.
set -euo pipefail
REAL=/opt/patents-shard/tantivy/bin/tantivy-serve
if [ -x "$REAL" ]; then
  exec "$REAL" --port "${SHARD_TANTIVY_PORT:-8635}" --index /opt/patents-shard/tantivy/index
fi
export PYTHONPATH="${SHARD_TANTIVY_LIB:-/opt/patents-shard/lib}${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 /opt/patents-shard/bin/tantivy_server.py
