#!/usr/bin/env python3
"""Look a publication up in the local patent corpus.

    python3 tools/patent_lookup.py US-9108319-B2            full record
    python3 tools/patent_lookup.py US-9108319-B2 --claims    claims only
    python3 tools/patent_lookup.py --check US-9108319-B2 EP-1234567-A1 ...   do these exist?

Prints plain text.  Anything not in the corpus is reported as NOT FOUND rather than guessed at.
"""
import sys, json

sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    try:
        import draft_cite
    except Exception as exc:                                    # noqa: BLE001
        print("lookup unavailable: %s: %s" % (type(exc).__name__, exc))
        return 1
    if argv[0] in ("--check", "-c"):
        for pub in argv[1:][:40]:
            found = draft_cite.resolve(pub)
            print("%-24s %s  %s" % (pub, "FOUND    " if found.get("found") else "NOT FOUND",
                                    found.get("title") or found.get("reason") or ""))
        return 0
    pub = argv[0]
    want_claims = "--claims" in argv
    record = draft_cite.resolve(pub, with_text=True)
    if not record.get("found"):
        print("NOT FOUND: %s (%s)" % (pub, record.get("reason") or "not in corpus"))
        return 0
    print("PUBLICATION %s" % record.get("publication_number"))
    for key in ("title", "publication_date", "filing_date", "priority_date", "assignee", "url"):
        if record.get(key):
            print("%-18s %s" % (key + ":", record[key]))
    if record.get("abstract") and not want_claims:
        print("\nABSTRACT\n%s" % record["abstract"])
    if record.get("claims"):
        print("\nCLAIMS\n%s" % record["claims"][:60000])
    elif want_claims:
        print("\n(no claim text held for this publication)")
    if record.get("description") and not want_claims:
        print("\nDESCRIPTION (first part)\n%s" % record["description"][:40000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
