"""Bilingual (source + machine-English) claim fields — src/translate.py split_bilingual().

The corpus stores many non-English claims already bilingual: the original immediately followed by
its English machine translation, concatenated with NO separator. Measured on the live DB, the
blob reads as ~EN 0.230 / FOREIGN 0.142, so looks_nonenglish() answered False, translate()
short-circuited on its "already English" pre-check and returned GERMAN text labelled
lang:"English" in 0.13-0.25 s having made no LLM call at all.
"""
import translate as T

# Real shapes taken from the corpus (DE-19601222-C2, DE-102009047815-B4, DE-4303944-A1).
DE_EN_NO_SPACE = (
    "Vakuum-Hebevorrichtung mit einer Vakuum-Saugeinrichtung (2) zum Ansaugen von Gegenstaenden, "
    "wobei die Saugeinrichtung (2) mit einem mit einer Vakuumquelle verbindbaren Hebeschlauch (1) "
    "verbunden und um eine Schwenkachse (4) zwischen zwei Positionen verschwenkbar ist, dadurch "
    "gekennzeichnet, dass die Saugeinrichtung (2) in der Ablegeposition automatisch wieder in die "
    "Aufnahmeposition verschwenkt."
    "Vacuum lifting device with a vacuum suction device ( 2 ) for sucking objects, wherein the "
    "suction device ( 2 ) is connected to a lifting hose ( 1 ) connectable to a vacuum source and "
    "is pivotable about a pivot axis ( 4 ) between two positions, characterized in that the "
    "suction device ( 2 ) in the storage position automatically pivots back into the receiving "
    "position."
)

DE_EN_NUMBERED = (
    "Hubeinrichtung mit einem an einem Grundkoerper (9) befestigten Mast, an dessen freien Ende "
    "sich ein Ausleger (6) befindet, an dem eine Hubeinrichtung (1) angeordnet ist, dadurch "
    "gekennzeichnet, dass der Mast zweiteilig ausgebildet ist und das obere Mastteil durch ein "
    "Stellglied um eine Achse verschwenkbar ist."
    "1. Lifting device with a mast attached to a base body ( 9 ), at the free end of which a boom "
    "( 6 ) is attached, on which a lifting device ( 1 ) is arranged, characterized in that the "
    "mast is in two parts and the upper mast part can be pivoted about an axis by an actuator."
)

ENGLISH_ONLY = (
    "A vacuum gripping apparatus comprising a gripper body defining a negative pressure chamber, "
    "a suction plate coupled to the gripper body, a first port and a second port formed in a wall "
    "of the chamber, wherein the first port is connected to a vacuum source and the second port is "
    "connected to a valve, and a plurality of suction openings formed in a surface of the suction "
    "plate such that the suction openings communicate with the negative pressure chamber."
)

GERMAN_ONLY = (
    "Sauggreifer nach Anspruch 1, dadurch gekennzeichnet, dass der Haltegriff (1) ueber die beiden "
    "Holme (2) mit dem Grundkoerper verbunden ist und dass die Saugplatte (5) an dem Grundkoerper "
    "elastisch gelagert ist, wobei zwischen der Saugplatte und dem Grundkoerper ein Dichtelement "
    "angeordnet ist, welches den Unterdruckraum nach aussen hin abdichtet."
)


def _de_words(s):
    en, fr = T._lang_votes(T._WORD_RE.findall(s.lower()))
    return fr > en


# ---- the split -------------------------------------------------------------------------------
def test_splits_when_english_follows_with_no_separator():
    pair = T.split_bilingual(DE_EN_NO_SPACE)
    assert pair is not None, "bilingual claim was not recognised"
    src, eng = pair
    assert _de_words(src) and not _de_words(eng)
    assert src.endswith("verschwenkt.")
    assert eng.startswith("Vacuum lifting device")


def test_splits_when_the_english_half_is_claim_numbered():
    pair = T.split_bilingual(DE_EN_NUMBERED)
    assert pair is not None
    src, eng = pair
    assert eng.startswith("Lifting device with a mast")
    # the "1." that introduced the English half must not be left dangling on the German half
    assert not src.rstrip().endswith("1.")


def test_no_german_leaks_into_the_english_half():
    for blob in (DE_EN_NO_SPACE, DE_EN_NUMBERED):
        _, eng = T.split_bilingual(blob)
        assert not _de_words(eng[:160]), f"German leaked into the English head: {eng[:80]!r}"


# ---- must NOT split --------------------------------------------------------------------------
def test_monolingual_english_is_not_split():
    assert T.split_bilingual(ENGLISH_ONLY) is None


def test_monolingual_german_is_not_split():
    assert T.split_bilingual(GERMAN_ONLY) is None


def test_short_text_is_not_split():
    assert T.split_bilingual("Sauggreifer nach Anspruch 1.") is None
    assert T.split_bilingual("") is None
    assert T.split_bilingual(None) is None


def test_abbreviation_is_not_a_boundary():
    """German 'z.B.' must not be read as a sentence end (it split DE-2536829-A1 wrongly)."""
    txt = ("Vorrichtung zum Ansetzen und Transportieren von verschiedenen glatten Flachteilen, "
           "z.B. Glasscheiben, Fliesen, geschliffene Marmorteile und Tafelware in verschiedenen "
           "Groessen und Staerken, dadurch gekennzeichnet, dass die Vorrichtung einen Grundkoerper "
           "mit mehreren Saugern aufweist, welche ueber Leitungen mit einer Vakuumquelle verbunden "
           "sind und einzeln absperrbar ausgebildet sind.")
    pair = T.split_bilingual(txt)
    # monolingual German: either no split at all, or certainly not one that calls German "English"
    assert pair is None


# ---- translate() end to end ------------------------------------------------------------------
def test_translate_returns_the_english_half_labelled_german(monkeypatch):
    """The whole point: no LLM call, correct label, English text out."""
    import llm
    calls = []
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: calls.append(1) or {})
    res = T.translate(DE_EN_NO_SPACE, use_cache=False)
    assert calls == [], "a bilingual field must not cost an LLM call"
    assert res["lang"] == "German"
    assert res["translated"] is True
    assert res["bilingual"] is True
    assert res["text"].startswith("Vacuum lifting device")
    assert not _de_words(res["text"])


def test_translate_no_longer_labels_german_as_english(monkeypatch):
    """THE REPRO: this returned {'lang': 'English'} with German text."""
    import llm
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    res = T.translate(DE_EN_NO_SPACE, use_cache=False)
    assert res["lang"] != "English"


def test_unsplittable_bilingual_text_still_never_claims_to_be_english():
    """Backstop: mixed-language text we cannot cleanly split must reach the translator."""
    mixed = GERMAN_ONLY + " " + ENGLISH_ONLY
    assert T.looks_nonenglish(mixed) or T._looks_mixed(mixed)


def test_plain_english_still_short_circuits(monkeypatch):
    import llm
    calls = []
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: calls.append(1) or {})
    res = T.translate(ENGLISH_ONLY, use_cache=False)
    assert calls == []
    assert res["lang"] == "English" and res["translated"] is False


def test_guess_source_language():
    assert T.guess_source_language(GERMAN_ONLY) == "German"
    assert T.guess_source_language("") == ""
