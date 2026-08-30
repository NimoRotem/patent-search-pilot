import sys
sys.path.insert(0, "/home/nimrod_rotem/patent-search-pilot/src")
import grounding, deep_analysis
ft = deep_analysis.full_text("DE-3724659-A1")
txt = "\n\n".join(p["text"] for p in ft["passages"])
QUOTES = [
 ("the decisive bracing/spacer disclosure (German)",
  "Distanzstück (5) vorgesehen ist, das durch die Anlage gegen das zu hebende Material die "
  "Zusammenpressung der Dichtlippen begrenzt"),
 ("the compressible foam seal (German)",
  "Dichtlippe (4) aus elastischem Material mit großer kompressibler Verformbarkeit, z.B. Schaumstoff"),
 ("the same passage, English machine translation as stored",
  "a spacer ( 5 ) which comes into contact with the material to be lifted and which extends all "
  "around as far as the sealing lips"),
]
print("raw corpus text contains a SOFT HYPHEN:", "­" in txt,
      "| contains 'kom­ pressibler':", "kom­ pressibler" in txt)
for name, q in QUOTES:
    print(f"\n{name}")
    print(f"   grounded()   = {grounding.grounded(q, txt)}")
    print(f"   span_ratio   = {grounding.span_ratio(q, txt):.2f}  (needs >= {grounding.MIN_SPAN})")
    print(f"   bigram_ratio = {grounding.bigram_ratio(q, txt):.2f}  (needs >= {grounding.MIN_BIGRAM})")
print("\ncontent_words() of a German phrase:",
      grounding.content_words("Dichtlippe aus elastischem Material mit großer kompressibler Verformbarkeit"))
print("content_words() of the SAME phrase as stored (soft-hyphenated):",
      grounding.content_words("Dichtlippe aus elastischem Material mit großer kom­ pressibler Verformbarkeit"))
