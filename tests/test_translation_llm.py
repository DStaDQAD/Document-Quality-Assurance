"""Cek Terjemahan, the plain flow: the model pairs the lines and compares them; code checks the numbers.
The model here is a fake that pairs lines carrying the same <tag> and answers the comparing calls
from a list."""
import asyncio
import re
from unittest.mock import Mock

import pytest
from langchain_core.runnables import RunnableLambda

import translation_llm as tl
from translation_check import MeaningFinding, MeaningFindings
from translation_llm import Group, WindowPairs, check_translation_llm

ID_DOC = "\n".join([
    "DAFTAR ISI <toc>",
    "Uang Beredar <h1>",
    "M2 tumbuh 8,2% pada Agustus 2026. <p1>",
    "Grafik 1 <toc>",
    "Uang kuasi tumbuh 6,5% secara tahunan. <p2>",
    "Kredit tumbuh positif pada bulan ini. <p3>",
    "Hasil lengkap survei dapat dilihat di website. <only>",
])
EN_DOC = "\n".join([
    "CONTENTS <toc>",
    "Money Supply <h1>",
    "Quasi-money grew 6.5% (yoy). <p2>",          # before p1: the versions may order around a chart
    "M2 grew 8.3% in August 2026. <p1>",
    "Chart 1 <toc>",
    "Loans grew positively this month. <p3>",
])
TAG = re.compile(r"<(\w+)>")


def _pairing(prompt_value) -> WindowPairs:
    """Pairs I and E lines with the same tag; <toc> lines skipped, an unpartnered tag one-sided."""
    text = prompt_value.to_messages()[1].content

    def tagged(side):
        out = {}
        for n, line in re.findall(rf"^{side}(\d+): (.*)$", text, re.M):
            out.setdefault(TAG.search(line).group(1), []).append(int(n))
        return out
    ids, ens = tagged("I"), tagged("E")
    pairs = [Group(id_lines=[i], en_lines=[e], kind="heading" if t.startswith("h") else "paragraph")
             for t in ids if t in ens and t != "toc" for i, e in zip(ids[t], ens[t])]
    return WindowPairs(pairs=pairs, id_only=[n for t in ids if t not in ens and t != "toc" for n in ids[t]],
                       en_only=[n for t in ens if t not in ids and t != "toc" for n in ens[t]],
                       skipped=ids.get("toc", []))


def _llm(findings=(), seen=None, pairing=_pairing, methods=None):
    """A model: pairing by `pairing`, every comparing call answered with `findings`."""
    def _structured(schema, **kw):
        if methods is not None:
            methods.append(kw.get("method"))

        def _respond(prompt_value):
            if seen is not None:
                seen.append((schema, prompt_value.to_messages()))
            if schema is WindowPairs:
                return pairing(prompt_value)
            return MeaningFindings(findings=list(findings))
        return RunnableLambda(_respond)
    llm = Mock()
    llm.with_structured_output = Mock(side_effect=_structured)
    return llm


def _check(llm, id_doc=ID_DOC, en_doc=EN_DOC, **kw):
    return asyncio.run(check_translation_llm(id_doc, en_doc, llm, document="laporan", id_filename="id.docx",
                                             en_filename="en.docx", id_is_word=True, en_is_word=True, **kw))


def _texts(report):
    return [(p.id_text, p.en_text) for p in report.pairs]


def test_the_model_pairs_the_text_and_leaves_contents_lines_out():
    report = _check(_llm())
    texts = _texts(report)
    assert ("Uang Beredar <h1>", "Money Supply <h1>") in texts
    assert ("M2 tumbuh 8,2% pada Agustus 2026. <p1>", "M2 grew 8.3% in August 2026. <p1>") in texts
    # A paragraph on the other side of a chart is still paired (NPI Tw II-2026 lost two that way).
    assert ("Uang kuasi tumbuh 6,5% secara tahunan. <p2>", "Quasi-money grew 6.5% (yoy). <p2>") in texts
    assert not any("<toc>" in (i or "") + (e or "") for i, e in texts)
    assert ("Hasil lengkap survei dapat dilihat di website. <only>", None) in texts


def test_numbers_are_still_checked_by_code_on_each_pair():
    report = _check(_llm())
    m2 = next(p for p in report.pairs if p.id_text and "<p1>" in p.id_text)
    assert [f.kind for f in m2.findings] == ["angka"] and m2.findings[0].only_id == ["8,2"]
    assert report.n_number == 1


def test_titles_are_marked_and_give_the_section_of_the_paragraphs_under_them():
    report = _check(_llm())
    head = next(p for p in report.pairs if p.id_text == "Uang Beredar <h1>")
    para = next(p for p in report.pairs if p.id_text and "<p3>" in p.id_text)
    assert head.id_section == "Judul" and para.id_section == "Uang Beredar <h1>"


def test_a_meaning_finding_lands_on_its_pair_and_one_saying_no_difference_is_dropped():
    findings = [MeaningFinding(pair=2, id_quote="a", en_quote="b", explanation="Arah berbeda: naik lawan turun."),
                MeaningFinding(pair=3, id_quote="a", en_quote="b",
                               explanation="Kedua versi menyebut angka yang sama, tidak ada perbedaan.")]
    report = _check(_llm(findings))
    assert any(f.explanation.startswith("Arah berbeda") for f in report.pairs[2].findings)
    assert not any("tidak ada perbedaan" in f.explanation for p in report.pairs for f in p.findings)


def test_every_structured_call_uses_function_calling():
    # The default JSON-schema method held one NPI window for over 240 s on OpenScale (2026-11-05).
    methods = []
    _check(_llm(methods=methods))
    assert methods and set(methods) == {"function_calling"}


def test_the_publications_terms_reach_the_comparing_prompt():
    seen = []
    _check(_llm(seen=seen), publication="npi")
    systems = [msgs[0].content for schema, msgs in seen if schema is MeaningFindings]
    assert systems and all("LLD (Lalu Lintas Devisa) = ITRS" in s for s in systems)
    seen.clear()
    _check(_llm(seen=seen))
    assert not any("TERMS (" in msgs[0].content for schema, msgs in seen if schema is MeaningFindings)


def test_titles_and_paragraphs_go_to_their_own_models():
    main_seen, head_seen = [], []
    _check(_llm(seen=main_seen), heading_llm=_llm(seen=head_seen))
    assert any("compare titles" in msgs[0].content for _, msgs in head_seen if _ is MeaningFindings)
    assert not any("compare titles" in msgs[0].content for _, msgs in main_seen if _ is MeaningFindings)


def test_a_text_printed_twice_is_compared_and_shown_once():
    # A note under two tables, the same on both sides both times (UB Jul: "Sejak 2021, tabungan…").
    twice_id = ID_DOC + "\nCatatan: angka sementara. <n>\nKredit modal kerja tumbuh. <p4>\nCatatan: angka sementara. <n>"
    twice_en = EN_DOC + "\nNote: preliminary. <n>\nWorking capital loans grew. <p4>\nNote: preliminary. <n>"
    seen = []
    report = _check(_llm(seen=seen), id_doc=twice_id, en_doc=twice_en)
    sent = "".join(msgs[1].content for schema, msgs in seen if schema is MeaningFindings)
    assert sent.count("Catatan: angka sementara.") == 1
    assert len([p for p in report.pairs if p.id_text and p.id_text.startswith("Catatan")]) == 2


def test_a_pairing_call_that_fails_once_is_asked_again():
    calls = []

    def flaky(prompt_value):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("provider hiccup")
        return _pairing(prompt_value)
    report = _check(_llm(pairing=flaky))
    assert len(calls) == 2 and report.n_unchecked == 0


def test_an_empty_answer_is_asked_again_and_twice_empty_is_reported_unchecked():
    report = _check(_llm(pairing=lambda prompt_value: None))
    assert report.n_unchecked == 1 and report.pairs[-1].id_text.startswith("1 bagian dokumen")


def test_a_call_that_runs_past_its_cap_is_cut_and_asked_again(monkeypatch):
    monkeypatch.setattr(tl, "CALL_TIMEOUT_S", 0.05)
    calls = []

    async def slow_then_fast(prompt_value):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(1)
        return _pairing(prompt_value)
    llm = Mock()
    llm.with_structured_output = Mock(side_effect=lambda schema, **kw: RunnableLambda(
        slow_then_fast if schema is WindowPairs else (lambda pv: MeaningFindings())))
    report = _check(llm)
    assert len(calls) == 2 and report.n_unchecked == 0


def test_a_group_reusing_a_line_or_numbered_outside_its_window_is_dropped():
    def bad(prompt_value):
        good = _pairing(prompt_value)
        first = good.pairs[0]
        good.pairs += [Group(id_lines=first.id_lines, en_lines=[999], kind="paragraph"),
                       Group(id_lines=first.id_lines, en_lines=first.en_lines, kind="paragraph")]
        return good
    assert _texts(_check(_llm(pairing=bad))) == _texts(_check(_llm()))


def test_a_long_report_is_paired_in_windows_that_cover_every_line():
    id_lines = [f"Kalimat ke-{k} tentang uang beredar bulan ini. <t{k}>" for k in range(200)]
    en_lines = [f"Sentence {k} about money supply this month. <t{k}>" for k in range(200)]
    windows = tl._windows(tl.lines_of("\n".join(id_lines)), tl.lines_of("\n".join(en_lines)))
    assert len(windows) >= 3 and all(len(i) <= tl.CHUNK_LINES for i, _ in windows)
    assert sorted(k for i, _ in windows for k in i) == list(range(200))
    assert all(i.start in e or i.start >= e.start for i, e in windows)
    report = _check(_llm(), id_doc="\n".join(id_lines), en_doc="\n".join(en_lines))
    assert len(report.pairs) == 200 and all(p.id_text and p.en_text for p in report.pairs)


def test_a_document_with_no_text_is_an_error_not_a_clean_result():
    with pytest.raises(ValueError, match="versi Inggris"):
        _check(_llm(), en_doc="[== Halaman 1 ==]\n")
