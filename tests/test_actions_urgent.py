"""The strip at the top of the actions page: what shuts soon on EVERY target and tab."""
import observations as obs


def _row(pub, days, instrument="Third-party observations", office="EPO", **kw):
    act = {"status": "open", "instrument": instrument, "days_left": days, "fee": "EUR 0"}
    row = {"publication": pub, "office": office, "days_left": days, "deadline": "2026-10-0%d" % min(days or 1, 9),
           "actions": [act] if days is not None else [], "action_headline": {"label": instrument},
           "title": "t " + pub, "state": "open"}
    row.update(kw)
    return row


def test_every_target_and_tab_is_read_and_only_what_shuts_in_time_is_listed(monkeypatch):
    targets = [{"id": 1, "name": "Schmalz"}, {"id": 2, "name": "Piab"}]
    data = {(1, "patent"): [_row("EP1", 3), _row("EP2", 40)],
            (1, "trademark"): [_row("EM9", 12, instrument="Opposition", office="EUIPO")],
            (2, "design"): [_row("D1", 0, instrument="Invalidity", office="EUIPO"),
                            _row("D2", None, closing_soon=True)],
            (2, "patent"): [_row("US1", 16)]}
    read = []

    def fake(user_id, target_id, today=None, kind="patent"):
        read.append((target_id, kind))
        return [dict(r) for r in data.get((target_id, kind), [])]
    monkeypatch.setattr(obs, "cases_for", fake)
    current = (1, "patent", [dict(r) for r in data[(1, "patent")]])
    u = obs.urgent_items(4, targets, current=current, filings=[], have=())
    assert [(i["pub"], i["days"], i["kind"], i["company"]) for i in u["items"]] == [
        ("D1", 0, "design", "Piab"), ("EP1", 3, "patent", "Schmalz"),
        ("EM9", 12, "trademark", "Schmalz")]
    assert [i["pub"] for i in u["anyday"]] == ["D2"]
    assert u["items"][1]["here"] and not u["items"][0]["here"]
    assert (1, "patent") not in read                        # the page's own rows are reused
    assert u["companies"] == 2


def test_an_action_list_date_sooner_than_the_window_gets_its_own_card(monkeypatch):
    row = _row("DE1", None, office="DPMA")
    row["boards"] = [{"n": "1", "title": "Confirm the fee", "days_left": 5, "deadline": "2026-10-01",
                      "due_note": "or the opposition is deemed not filed"}]
    monkeypatch.setattr(obs, "cases_for", lambda *a, **k: [])
    u = obs.urgent_items(4, [{"id": 1, "name": "Schmalz"}], current=(1, "patent", [row]), filings=[])
    [card] = u["items"]
    assert card["kind"] == "action" and card["days"] == 5 and card["tab"] == "patent"
    assert card["what"].startswith("Action 1") and "deemed not filed" in card["context"]


def test_an_action_naming_two_patents_is_one_card(monkeypatch):
    rows = []
    for pub in ("EP3995267B1", "DE102020129586B4"):
        r = _row(pub, None)
        r["boards"] = [{"n": "11", "title": "Re-read the two lost registers", "days_left": 4,
                        "deadline": "2026-09-30"}]
        rows.append(r)
    monkeypatch.setattr(obs, "cases_for", lambda *a, **k: [])
    u = obs.urgent_items(4, [{"id": 1, "name": "Schmalz"}], current=(1, "patent", rows), filings=[])
    [card] = u["items"]
    assert card["more"] == 1 and card["pub"] == "EP3995267B1"
