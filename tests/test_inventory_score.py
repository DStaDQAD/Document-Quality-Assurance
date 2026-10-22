"""score.py --run: a new run scored against the labels by sentence + printed number, because the
labels point at claims by their index in the run they were made on (from_system)."""
import json

from tools.inventory import score

SENTENCE = {
    "sid": "s002",
    "text": "Uang Primer (M0) Adjusted pada Juli 2026 tumbuh 17,1% (yoy), lebih tinggi dibandingkan "
            "pertumbuhan bulan Juni 2026 sebesar 13,8% (yoy) sehingga tercatat sebesar Rp2.254,5 triliun.",
    "numbers": [{"text": t, "year_like": False, "reference": False} for t in ("17,1", "13,8", "2.254,5")],
}


def _gold(number, measure="growth_yoy", verdict="Sesuai", row="Uang Primer Adjusted 1)", direction="none",
          period="Juli 2026"):
    return {"number": number, "measure": measure, "expected_verdict": verdict, "direction": direction,
            "maps_to": {"kind": "row", "source": "I.2", "row": row}, "period": period}


def _sys(value, operation="yoy_growth", verdict="Entailed", row="Uang Primer Adjusted 1)", month="Jul"):
    return {"operation": operation, "metric_label": row, "claimed_value": value, "verdict": verdict,
            "matched_source": "TABEL1_2.xls / I.2",
            "periods": [{"metric_label": row, "year": 2026, "month": month, "col_label": None}]}


def test_claims_pair_up_by_their_printed_number_whatever_the_order():
    gold = [_gold("17,1"), _gold("13,8", period="Juni 2026"), _gold("2.254,5", measure="level")]
    system = [_sys(2254.5, operation="value"), _sys(13.8, month="Jun"), _sys(17.1)]

    pairs, missed, extra = score.match_sentence(SENTENCE, gold, system)

    assert {(g["number"], s["claimed_value"]) for g, s in pairs} == {("17,1", 17.1), ("13,8", 13.8),
                                                                     ("2.254,5", 2254.5)}
    assert missed == [] and extra == []


def test_a_threshold_pairs_with_the_threshold_label_not_with_the_value():
    sentence = {"sid": "s001", "text": "PMI-BI sebesar 51,43%, berada pada fase ekspansi.",
                "numbers": [{"text": "51,43", "year_like": False, "reference": False}]}
    gold = [_gold("51,43", measure="level", row="PMI - BI"), _gold(None, measure="threshold", row="PMI - BI")]
    system = [_sys(50.0, operation="above_threshold", row="PMI - BI"), _sys(51.43, operation="value", row="PMI - BI")]

    pairs, missed, extra = score.match_sentence(sentence, gold, system)

    assert {(g["measure"], s["operation"]) for g, s in pairs} == {("level", "value"),
                                                                  ("threshold", "above_threshold")}


def test_a_trend_pairs_with_a_direction_label_and_leftovers_are_missed_or_extra():
    gold = [_gold(None, measure="direction", direction="up"), _gold("17,1")]
    system = [_sys(None, operation="is_increasing"), _sys(99.9, operation="value")]

    pairs, missed, extra = score.match_sentence(SENTENCE, gold, system)

    assert [(g["measure"], s["operation"]) for g, s in pairs] == [("direction", "is_increasing")]
    assert [g["number"] for g in missed] == ["17,1"]
    assert [s["claimed_value"] for s in extra] == [99.9]


def test_tally_counts_right_luck_wrong_missed_and_extra():
    pairs = [(_gold("17,1"), _sys(17.1)),
             (_gold("13,8"), _sys(13.8, row="Uang Primer")),                  # right verdict, wrong row
             (_gold("2.254,5", verdict="Tidak Sesuai"), _sys(2254.5, operation="value"))]
    tally = score.tally(pairs, missed=[_gold("15,1")], extra=[_sys(1.0, verdict="Refuted")])

    assert tally["system_claims"] == 4 and tally["verdict_right"] == 2
    assert tally["right_verdict_wrong_reading"] == 1
    assert tally["false_Sesuai_should_be_Tidak Sesuai"] == 1
    assert tally["missed_by_system"] == 1
    assert tally["not_a_claim"] == 1 and tally["not_a_claim_judged_Tidak Sesuai"] == 1


def test_score_run_reads_the_named_run_of_every_labelled_document(tmp_path):
    base, cases = tmp_path / "inventory", tmp_path / "cases"
    (base / "labels").mkdir(parents=True)
    (base / "data").mkdir()
    (cases / "m0_jul2026" / "notes1").mkdir(parents=True)
    name = "m0_jul2026__m0-sp.json"
    (base / "data" / name).write_text(json.dumps({"sentences": [dict(SENTENCE, system_claims=[])]}),
                                      encoding="utf-8")
    labels = {"case": "m0_jul2026", "doc_id": "m0-sp", "publication": "uang-primer-m0", "labels": {
        "s002": {"status": "done", "sentence_kind": "data",
                 "claims": [dict(_gold("17,1"), from_system=None), dict(_gold("13,8"), from_system=None)]}}}
    (base / "labels" / name).write_text(json.dumps(labels), encoding="utf-8")
    run = {"results": [dict(_sys(17.1), context_quote=SENTENCE["text"])]}
    (cases / "m0_jul2026" / "notes1" / "m0-sp.json").write_text(json.dumps(run), encoding="utf-8")

    rows = score.score_run(base, "notes1", cases_root=cases)

    r = rows["uang-primer-m0"]
    assert (r["documents"], r["system_claims"], r["verdict_right"], r["missed_by_system"]) == (1, 1, 1, 1)


def test_a_labelled_claim_without_a_printed_number_pairs_on_its_measure():
    # PII Tw IV s024: the sentence runs across a page break, its numbers are in the next half, so the
    # label has no printed number; the system's diff claims still belong to it.
    gold = [_gold(None, measure="change"), _gold(None, measure="change", row="Kewajiban")]
    system = [_sys(15.3, operation="diff"), _sys(8.4, operation="diff", row="Kewajiban"),
              _sys(1.0, operation="value")]

    pairs, missed, extra = score.match_sentence(SENTENCE, gold, system)

    assert len(pairs) == 2 and missed == []
    assert [s["operation"] for s in extra] == ["value"]
