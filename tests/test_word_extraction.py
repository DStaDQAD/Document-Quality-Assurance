"""word_extraction: a Word report's narrative and the tables it prints as EMF pictures."""

from word_extraction import EmfText, emf_table_lines, emf_text_runs
from word_fixtures import SNIPPET_RUNS, emf_bytes


# --- EMF text records -----------------------------------------------------------------------

def test_emf_text_runs_reads_every_text_record_with_its_position():
    runs = emf_text_runs(emf_bytes([(10, 20, "Giro"), (90, 20, "3.130,8")]))
    assert runs == [EmfText(10, 20, "Giro"), EmfText(90, 20, "3.130,8")]


def test_emf_text_runs_ignores_what_is_not_an_emf():
    assert emf_text_runs(b"\x89PNG\r\n\x1a\n" + bytes(100)) == []
    assert emf_text_runs(b"") == []


def test_emf_text_runs_survives_a_truncated_file():
    data = emf_bytes([(10, 20, "Giro"), (90, 20, "3.130,8")])
    assert emf_text_runs(data[:-30]) == [EmfText(10, 20, "Giro")]


# --- rows -----------------------------------------------------------------------------------

def _texts(lines):
    return [text for _, text in lines]


def test_lines_run_top_to_bottom_with_the_label_before_its_values():
    lines = emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS)))
    assert lines[0][0] > lines[-1][0]          # PDF convention: larger y is higher
    assert "Uang Beredar Luas (M2) 10.355,7 10.253,7 9,7 9,2" in _texts(lines)


def test_a_label_that_wraps_just_above_its_values_joins_them():
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS))))
    assert "Uang Kartal di Luar Bank Umum dan BPR 1.206,1 1.186,3 10,8 15,7" in texts


def test_a_label_split_around_its_values_reads_in_order():
    # Tabel 9's last row: "Surat Berharga … Sektor" / values / "Swasta**", 13 units apart
    # against a row pitch of ~37.
    runs = [(100, 34, "Mar"), (160, 34, "Apr*"), (240, 34, "Mar'26"),
            (0, 77, "Giro"), (100, 77, "5,5"), (160, 77, "7,6"), (240, 77, "39,2"),
            (0, 114, "Uang Kartal"), (100, 114, "1,0"), (160, 114, "2,0"), (240, 114, "3,0"),
            (0, 138, "Surat Berharga Sektor"), (100, 151, "26,7"), (160, 151, "36,1"),
            (240, 151, "35,0"), (0, 164, "Swasta**")]
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(runs))))
    assert "Surat Berharga Sektor Swasta** 26,7 36,1 35,0" in texts


def test_header_lines_are_never_merged_into_a_data_row():
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS))))
    assert "2026 % (yoy)" in texts and "Komponen Uang Beredar" in texts
    assert "Mar Apr* Mar'26 Apr'26*" in texts


def test_without_a_period_header_rows_are_only_grouped_by_height():
    runs = [(0, 10, "a"), (50, 10, "1,0"), (0, 11, "b")]
    assert _texts(emf_table_lines(emf_text_runs(emf_bytes(runs)))) == ["a 1,0", "b"]
