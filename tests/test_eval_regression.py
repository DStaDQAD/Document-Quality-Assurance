"""Deterministic tests for the regression runner's manifest, label scoring and per-claim diff.

No LLM and no documents: results are the dicts /api/verify-paired returns, built by hand.
"""

from pathlib import Path

import pytest

from eval.regression import diff_runs, load_manifest, score_label, Label


def _r(quote, verdict, operation="is_increasing", value=None, metric="Penjualan", reasoning=""):
    return {"context_quote": quote, "verdict": verdict, "operation": operation,
            "claimed_value": value, "metric_label": metric, "reasoning": reasoning}


# --- manifest ---------------------------------------------------------------------------------

def test_load_manifest_reads_documents_sources_and_labels(tmp_path: Path):
    m = tmp_path / "m.yaml"
    m.write_text(
        "- id: toko-mei\n"
        "  publication: sk\n"
        "  doc: data/laporan.docx\n"
        "  sources:\n"
        "    - {file: data/tabel.xlsx, sheets: ['Tabel 1', 'Tabel 2']}\n"
        "    - {file: data/semua.xlsx}\n"
        "  labels:\n"
        "    - {quote: 'naik di seluruh toko', expected: Entailed, note: 'cek manual'}\n",
        encoding="utf-8",
    )
    [doc] = load_manifest([m])
    assert doc.id == "toko-mei" and doc.publication == "sk" and doc.mode == "excel"
    assert doc.sources[0].sheets == ["Tabel 1", "Tabel 2"]
    assert doc.sources[1].sheets == "*"          # no list = every sheet, like ticking them all
    assert doc.labels == [Label(quote="naik di seluruh toko", expected="Entailed", note="cek manual")]


def test_load_manifest_rejects_an_unknown_expected_verdict(tmp_path: Path):
    m = tmp_path / "m.yaml"
    m.write_text("- {id: a, publication: sk, doc: x.docx, sources: [{file: t.xlsx}],"
                 " labels: [{quote: q, expected: Benar}]}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Benar"):
        load_manifest([m])


def test_load_manifest_rejects_duplicate_ids(tmp_path: Path):
    m = tmp_path / "m.yaml"
    m.write_text("- {id: a, publication: sk, doc: x.docx, sources: [{file: t.xlsx}]}\n"
                 "- {id: a, publication: sk, doc: y.docx, sources: [{file: t.xlsx}]}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="a"):
        load_manifest([m])


# --- scoring a label --------------------------------------------------------------------------

def test_score_label_is_missing_when_no_result_quotes_the_sentence():
    label = Label(quote="toko C turun", expected="Entailed")
    assert score_label(label, [_r("Penjualan toko A naik.", "Entailed")]).status == "hilang"


def test_score_label_matches_the_quote_ignoring_case_and_spacing():
    label = Label(quote="Toko A  naik", expected="Entailed")
    assert score_label(label, [_r("Penjualan toko a naik.", "Entailed")]).status == "benar"


def test_a_true_claim_split_into_several_facts_is_right_only_when_every_fact_is():
    """A multi-group sentence may come back as one fact per group. Calling the claim right
    when one of its parts is still Tidak Cukup Data would hide exactly the gap being fixed."""
    label = Label(quote="naik di seluruh toko", expected="Entailed")
    results = [_r("Penjualan naik di seluruh toko.", "Entailed", metric="toko A"),
               _r("Penjualan naik di seluruh toko.", "Inconclusive", metric="toko B")]
    assert score_label(label, results).status == "salah"


def test_a_seeded_error_counts_as_caught_when_any_part_is_refuted():
    """'naik di seluruh toko' with one shop falling: the shops that did rise are rightly
    Sesuai; the claim is caught as soon as the shop that fell is Tidak Sesuai."""
    label = Label(quote="naik di seluruh toko", expected="Refuted")
    results = [_r("Penjualan naik di seluruh toko.", "Entailed", metric="toko A"),
               _r("Penjualan naik di seluruh toko.", "Refuted", metric="toko B")]
    assert score_label(label, results).status == "benar"


def test_score_label_narrows_by_operation_and_value_when_given():
    label = Label(quote="toko A", expected="Entailed", operation="yoy_growth", claimed_value=-0.3)
    results = [_r("Penjualan toko A turun 0,3% (yoy) menjadi 12,5.", "Inconclusive",
                  operation="value", value=12.5),
               _r("Penjualan toko A turun 0,3% (yoy) menjadi 12,5.", "Entailed",
                  operation="yoy_growth", value=-0.3)]
    assert score_label(label, results).status == "benar"


# --- per-claim diff ---------------------------------------------------------------------------

def test_diff_pairs_the_same_claim_despite_a_different_metric_label():
    """The extractor names the same row differently from run to run ('Jasa / TOTAL' vs
    'Jasa / TOTAL > Pemerintah'); the quote, operation and number identify the claim."""
    base = [_r("Pangsa jasa 16,2%.", "Entailed", "ratio", 16.2, "Jasa / TOTAL")]
    new = [_r("Pangsa jasa 16,2%.", "Entailed", "ratio", 16.2, "Jasa / TOTAL > Pemerintah")]
    [change] = diff_runs(base, new)
    assert change.kind == "sama"


def test_diff_reports_a_changed_verdict_and_flags_a_lost_entailed():
    base = [_r("Toko A naik.", "Entailed")]
    new = [_r("Toko A naik.", "Inconclusive")]
    [change] = diff_runs(base, new)
    assert change.kind == "berubah" and change.needs_review


def test_diff_does_not_flag_inconclusive_becoming_entailed():
    [change] = diff_runs([_r("Toko A naik.", "Inconclusive")], [_r("Toko A naik.", "Entailed")])
    assert change.kind == "berubah" and not change.needs_review


def test_diff_flags_a_new_refuted_claim_and_a_vanished_entailed_one():
    base = [_r("Toko A naik.", "Entailed")]
    new = [_r("Toko B turun 5,0%.", "Refuted", "yoy_growth", -5.0)]
    changes = {c.kind: c for c in diff_runs(base, new)}
    assert changes["hilang"].needs_review          # a Sesuai claim no longer checked at all
    assert changes["baru"].needs_review            # a new Tidak Sesuai must be explained


def test_diff_pairs_duplicate_keys_by_metric_label():
    """One sentence, one operation, no number: 'naik di toko A dan toko B' gives two facts that
    differ only in their metric. Each must be compared with its own counterpart."""
    base = [_r("Naik di toko A dan B.", "Entailed", metric="toko A"),
            _r("Naik di toko A dan B.", "Inconclusive", metric="toko B")]
    new = [_r("Naik di toko A dan B.", "Inconclusive", metric="toko B"),
           _r("Naik di toko A dan B.", "Entailed", metric="toko A")]
    assert [c.kind for c in diff_runs(base, new)] == ["sama", "sama"]


def test_diff_marks_a_change_the_ai_review_made():
    base = [_r("Toko A naik.", "Inconclusive")]
    new = [_r("Toko A naik.", "Entailed", reasoning="x | Ditinjau AI: sel ...")]
    [change] = diff_runs(base, new)
    assert change.ai_review


def test_diff_pairs_an_anchor_only_quote_with_the_sentence_that_contains_it():
    """When the model's anchor is not found, the quote IS the anchor ('sebesar 109,2'); in the
    next run it may be located and become the whole sentence. Same claim."""
    base = [_r("sebesar 109,2", "Entailed", "value", 109.2, "Indeks > Usia 31-40")]
    new = [_r("Indeks tertinggi pada usia 20-30 dan 31-40, masing-masing 116,1 dan 109,2.",
              "Entailed", "value", 109.2, "Indeks > Usia 31-40")]
    [change] = diff_runs(base, new)
    assert change.kind == "sama"


def test_a_claim_restructured_into_other_facts_of_the_same_sentence_is_not_flagged():
    """Five per-group facts became one 'seluruh kelompok' fact for the same sentence, with the
    same verdict. That is a different shape of the same check, not a lost Sesuai."""
    q = "Indeks berada pada level optimis pada seluruh kelompok pengeluaran."
    base = [_r(q, "Entailed", "above_threshold", 100.0, f"Indeks > Kelompok {i}") for i in range(3)]
    new = [_r(q, "Entailed", "above_threshold", 100.0, "Indeks (seluruh kelompok)")]
    changes = diff_runs(base, new)
    assert {c.kind for c in changes} <= {"sama", "dibentuk_ulang"}
    assert not any(c.needs_review for c in changes)


def test_a_restructured_sentence_with_a_different_verdict_is_still_flagged():
    q = "Indeks berada pada level optimis pada seluruh kelompok pengeluaran."
    base = [_r(q, "Entailed", "above_threshold", 100.0, "Indeks > Kelompok A")]
    new = [_r(q, "Refuted", "above_threshold", 100.0, "Indeks (seluruh kelompok)")]
    assert any(c.needs_review for c in diff_runs(base, new))


# --- replay: re-judging saved results without calling the model ------------------------------

def test_a_saved_result_is_rebuilt_into_the_fact_it_judged():
    from eval.regression import fact_from_result
    r = {"operation": "yoy_growth", "metric_label": "Toko A", "claimed_value": -0.3,
         "claimed_unit": "persen_yoy", "context_quote": "Toko A turun 0,3% (yoy).", "verdict": "Entailed",
         "reasoning": "", "periods": [{"metric_label": "Penjualan > Toko A", "year": 2025, "month": "Jul"},
                                      {"metric_label": "Penjualan > Toko A", "year": 2026, "month": "Jul"}]}
    fact = fact_from_result(r)
    assert fact.operation == "yoy_growth" and fact.claimed_value == -0.3
    assert [(p.metric_label, p.year, p.month) for p in fact.periods] == [("Penjualan > Toko A", 2026, "Jul")]


def test_an_unresolved_result_is_rebuilt_from_its_reason():
    from eval.regression import fact_from_result
    r = {"operation": "value", "metric_label": "pangsa toko", "claimed_value": 80.6, "claimed_unit": "persen",
         "context_quote": "q", "verdict": "Inconclusive", "periods": [],
         "reasoning": "Data tidak ditemukan di sumber Excel manapun untuk: pangsa toko Jul 2026."}
    fact = fact_from_result(r)
    assert [(p.metric_label, p.year, p.month) for p in fact.periods] == [("pangsa toko", 2026, "Jul")]


def test_a_quantified_result_is_rebuilt_on_its_series():
    from eval.regression import fact_from_result
    r = {"operation": "is_increasing", "metric_label": "Penjualan (seluruh kelompok)", "group_scope": "all",
         "context_quote": "q", "verdict": "Entailed", "reasoning": "", "claimed_value": None,
         "periods": [{"metric_label": "Penjualan > Utara", "year": 2026, "month": "Aug"},
                     {"metric_label": "Penjualan > Utara", "year": 2026, "month": "Sep"}]}
    fact = fact_from_result(r)
    assert fact.group_scope == "all"
    assert [(p.metric_label, p.month) for p in fact.periods] == [("Penjualan", "Sep")]


def test_a_chart_label_result_is_not_replayed():
    from eval.regression import fact_from_result
    assert fact_from_result({"checked_item": "chart", "operation": "value", "periods": []}) is None


# --- replay of a strict run ---------------------------------------------------------------------

def test_replay_rejudges_strict_claims(tmp_path: Path, monkeypatch):
    import json

    from eval import run_regression
    from excel_parser_bi import BITableData
    from paired_verifier import _ExcelSource
    from strict.claims import Cell, Claim, claim_to_dict

    manifest = tmp_path / "m.yaml"
    manifest.write_text("- id: pii-sp\n  publication: pii\n  doc: d.docx\n  sources:\n    - {file: t.xls}\n",
                        encoding="utf-8")
    table = BITableData(title="PII", unit="Juta USD", row_labels=["Aset"])
    table._data[("Aset", 2025, "Q4")] = 558496.4949
    monkeypatch.setattr(run_regression, "_parsed_sources",
                        lambda doc, root: [_ExcelSource(table=table, filename="t.xls", sheet="5.39")])
    monkeypatch.setattr(run_regression, "_narrative_of", lambda doc, root: None)
    claim = Claim(operation="value", subject="Posisi AFLN",
                  cells=[Cell("S1", "Aset", "Q4 2025", "Aset", (2025, "Q4"))],
                  context_quote="Posisi AFLN tercatat 558,5 miliar dolar AS.", claimed_value=558.5,
                  unit="miliar USD")
    run_dir, out = tmp_path / "run", tmp_path / "out"
    run_dir.mkdir()
    (run_dir / "pii-sp.json").write_text(json.dumps({"results": [], "strict_claims": [claim_to_dict(claim)]}),
                                         encoding="utf-8")

    run_regression.main(["replay", str(run_dir), "--manifest", str(manifest), "--out", str(out)])

    [result] = json.loads((out / "pii-sp.json").read_text(encoding="utf-8"))["results"]
    assert (result["verdict"], result["resolved_via"]) == ("Entailed", "strict")


def test_run_has_a_strict_switch():
    from eval import run_regression
    import argparse

    seen = {}
    real = argparse.ArgumentParser.parse_args

    def _capture(self, argv=None, namespace=None):
        seen["args"] = real(self, argv, namespace)
        raise SystemExit(0)

    import unittest.mock as um
    with um.patch.object(argparse.ArgumentParser, "parse_args", _capture), pytest.raises(SystemExit):
        run_regression.main(["run", "--manifest", "m.yaml", "--out", "o", "--strict"])
    assert seen["args"].strict is True


def test_a_document_can_name_reference_documents(tmp_path: Path):
    # The team's usual check: press release and report together, each the other's reference.
    from eval.run_regression import _request_files
    (tmp_path / "laporan.docx").write_bytes(b"L")
    (tmp_path / "sp.docx").write_bytes(b"S")
    (tmp_path / "t.xlsx").write_bytes(b"X")
    m = tmp_path / "m.yaml"
    m.write_text("- id: ub-laporan\n  publication: uang-beredar\n  doc: laporan.docx\n  mode: both\n"
                 "  references: [sp.docx]\n  sources:\n    - {file: t.xlsx, sheets: ['I.1']}\n", encoding="utf-8")

    [doc] = load_manifest([m])
    files, sheets = _request_files(doc, tmp_path)

    assert doc.references == ["sp.docx"] and doc.mode == "both"
    assert [(field, name) for field, (name, _data, _type) in files] == [
        ("pdf_file", "laporan.docx"), ("excel_file", "t.xlsx"), ("reference_pdf", "sp.docx")]
    assert sheets == ["I.1"]
