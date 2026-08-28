#!/usr/bin/env bash
# Milestone 6 §3 — end-to-end regression / release-candidate check. All green = shippable.
set -uo pipefail
cd "$(dirname "$0")"
BASE=http://127.0.0.1:8631
PY="$(cd "$(dirname "$0")" && pwd)/.venv/bin/python"
pass=0; fail=0
ok(){ echo "  ✓ $*"; pass=$((pass+1)); }
bad(){ echo "  ✗ $*"; fail=$((fail+1)); }
code(){ curl -s -o /dev/null -w "%{http_code}" "$@"; }

echo "== health =="
[ "$(code $BASE/healthz)" = 200 ] && ok "healthz 200" || bad "healthz"

echo "== all 11 gold reports load + have claim chart + 25 cards =="
GOLD="grabo_gripper_novelty grabo_gripper_inventive grabo_extended_frame grabo_de_utility_xling schmalz_sauggreifsystem schmalz_vacuum_clamp probst_stone_lifter_xling probst_kerb_lifter nl_handheld_vacuum_seal_sensor nl_porous_surface_gripper nl_robot_eoat_vacuum"
for g in $GOLD; do
  html=$(curl -s $BASE/report/$g)
  c=$(echo "$html" | grep -oc 'class="refcard"')
  # The heading is "Element × reference grid" — it was renamed away from "claim chart" by the
  # disclosure work (a retrieval map is not a claim chart), and this gate went on matching the old
  # string, so all 11 gold reports had been failing here on a wording change, not a defect.
  chart=$(echo "$html" | grep -oc 'Element × reference grid')
  if [ "$c" -ge 10 ] && [ "$chart" -ge 1 ]; then ok "$g ($c cards, chart)"; else bad "$g (cards=$c chart=$chart)"; fi
done

echo "== reference enrichment (drawings + PDF + sections) for a US ref =="
ref=$(curl -s "$BASE/api/ref/US-11207792-B2?slug=grabo_gripper_novelty")
echo "$ref" | grep -q '"n_images"' && ok "ref api returns display" || bad "ref api"
imgs=$(echo "$ref" | $PY -c "import sys,json;print(json.load(sys.stdin)['display'].get('n_images',0))" 2>/dev/null)
[ "${imgs:-0}" -ge 1 ] && ok "US ref has $imgs drawings" || bad "US ref drawings"
[ "$(code $BASE/pdf/US-11207792-B2)" = 200 ] && ok "PDF facsimile serves" || bad "PDF facsimile"

echo "== citation graph + more-like-this + compare + print =="
echo "$(curl -s $BASE/api/graph/US-11207792-B2)" | grep -q '"backward"' && ok "citation graph" || bad "citation graph"
echo "$(curl -s $BASE/api/morelike/US-11207792-B2)" | grep -q '"results"' && ok "more-like-this" || bad "more-like-this"
[ "$(code "$BASE/compare?slug=grabo_gripper_novelty&pubs=US-3005652-A,US-11207792-B2")" = 200 ] && ok "compare" || bad "compare"
[ "$(code $BASE/print/grabo_gripper_novelty)" = 200 ] && ok "print view" || bad "print view"

echo "== triage flags persist =="
curl -s -X POST $BASE/api/flags/grabo_gripper_novelty -H 'Content-Type: application/json' -d '{"pub":"US-3005652-A","flag":"relevant","note":"regtest"}' >/dev/null
curl -s $BASE/api/flags/grabo_gripper_novelty | grep -q '"regtest"' && ok "flag persisted" || bad "flag persist"
curl -s -X POST $BASE/api/flags/grabo_gripper_novelty -H 'Content-Type: application/json' -d '{"pub":"US-3005652-A","flag":"","note":""}' >/dev/null  # reset

echo "== export PDF + DOCX + XLSX + MD (gold AND free-text) =="
for fmt in pdf docx xlsx md; do
  sz=$(curl -s -X POST $BASE/export -d "slug=grabo_gripper_novelty" -d "pubs=US-3005652-A,US-11207792-B2,US-9457478-B2" -d "format=$fmt" -o /tmp/reg_gold.$fmt -w "%{size_download}")
  [ "${sz:-0}" -gt 20000 ] && ok "gold export $fmt ($sz b)" || bad "gold export $fmt"
done
# The Markdown export exists to carry the reference text the other three drop, so "it downloaded"
# is not enough of a check: assert it actually contains claim text and no images/links.
grep -q '#### Claims (' /tmp/reg_gold.md && ok "md carries full claim text" || bad "md has no claims"
! grep -q '](http' /tmp/reg_gold.md && ok "md is link-free" || bad "md leaked hyperlinks"
# Every real ad-hoc report has a `.meta.json` companion. Discover from that positive marker rather
# than trying to maintain a growing denylist of sidecars (`view`, `archive`, `claim-grid`, …).
FT=$(ls -t data/reports/adhoc-*.meta.json 2>/dev/null | head -1 | xargs -n1 basename 2>/dev/null | sed 's/\.meta\.json$//')
if [ -n "$FT" ]; then
  pubs=$($PY -c "import sys;sys.path.insert(0,'src');import json,webview;v=webview.build_view(json.load(open('data/reports/$FT.json')),top_n=4);print(','.join(c['pub'] for c in v['cards'][:4]))" 2>/dev/null | grep -v FutureWarning)
  sz=$(curl -s -X POST $BASE/export -d "slug=$FT" -d "pubs=$pubs" -d "format=pdf" -o /tmp/reg_ft.pdf -w "%{size_download}")
  [ "${sz:-0}" -gt 20000 ] && ok "free-text export pdf ($sz b)" || bad "free-text export pdf"
else echo "  (no free-text report cached to export-test)"; fi

echo "== edge cases: no 500s =="
[ "$(code -X POST $BASE/run -d 'query=')" = 302 ] && ok "empty query -> 302" || bad "empty query"
[ "$(code $BASE/report/does-not-exist-zzz)" = 404 ] && ok "bad slug -> 404" || bad "bad slug"
[ "$(code "$BASE/api/ref/JUNK-9?slug=grabo_gripper_novelty")" = 200 ] && ok "junk ref -> 200 graceful" || bad "junk ref"
[ "$(code $BASE/api/graph/JUNK-9)" = 200 ] && ok "junk graph -> 200" || bad "junk graph"
[ "$(code -X POST $BASE/export -d 'slug=x' -d 'pubs=' -d 'format=pdf')" = 400 ] && ok "empty export -> 400" || bad "empty export"
[ "$(code $BASE/pdf/JUNK-9)" = 404 ] && ok "missing pdf -> 404" || bad "missing pdf"

echo "== drafting studio: routes exist, are account-gated, and the agent is configured =="
# These are unauthenticated calls on purpose. A drafting route that answers 200 to a signed-out
# request is the failure worth catching here; 403 IS the pass.
[ "$(code $BASE/drafts/start)" = 403 ] && ok "intake is account-gated" || bad "intake gate"
[ "$(code $BASE/drafts/1/studio)" = 403 ] && ok "studio is account-gated" || bad "studio gate"
[ "$(code $BASE/api/drafts/1/studio/poll)" = 403 ] && ok "studio poll is account-gated" || bad "poll gate"
[ "$(code -X POST $BASE/drafts/1/studio/message)" = 400 ] && ok "message POST needs CSRF" || bad "message CSRF"
health=$(curl -s $BASE/healthz)
echo "$health" | grep -q '"draft_turn_worker"' && ok "turn worker reports health" || bad "turn worker health"
echo "$health" | $PY -c "import sys,json;d=json.load(sys.stdin)['draft_turn_worker'];sys.exit(0 if d['thread_alive'] and d['configured'] else 1)" \
  && ok "turn worker thread alive" || bad "turn worker thread"
echo "$health" | $PY -c "import sys,json;a=json.load(sys.stdin)['draft_turn_worker']['agent'];print('   ',a.get('binary'),a.get('version'));sys.exit(0 if a['ok'] else 1)" \
  && ok "drafting agent available" || bad "drafting agent NOT available"
( cd src && $PY -c "
import sys; sys.path.insert(0,'.')
import draft_qa, draft_workspace, draft_uspto, draft_studio
s = {k: 'x' for k,_n,_h in draft_workspace.SECTION_FILES}
s['claims'] = '1. A tool comprising a body.'
s['abstract'] = 'A tool.'
assert draft_qa.run_checks(sections=s, numerals=[], figures=[], allow_remote=False)
assert draft_uspto.fee_profile(s['claims'])['independent'] == 1
assert draft_studio.render_markdown(s)
print('drafting checks importable and runnable')
" ) >/dev/null 2>&1 && ok "consistency checks run" || bad "consistency checks"

echo "== OPS parser (dry-run, no creds) =="
( cd src && PATENT_SKIP_DOTENV=1 $PY test_ops.py ) 2>&1 | grep -q 'PASS' && ok "ops parser test PASS" || bad "ops parser"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ] && echo "ALL GREEN — release candidate" || echo "FAILURES present"
