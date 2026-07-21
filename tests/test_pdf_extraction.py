import asyncio
import base64
import io
import struct
from unittest.mock import Mock, patch

import pytest

from pdf_extraction import (
    _render_pages_to_b64,
    _strip_tabular_content,
    extract_narrative_text,
    extract_text_from_pdf,
    extract_text_from_pdf_vision_async,
)


# ---------------------------------------------------------------------------
# _strip_tabular_content — wordy-label table rows (trailing numeric run)
# ---------------------------------------------------------------------------

# Verbatim leaked rows measured in the April 2026 test PDF: their long labels (plus
# pypdfium splitting words like 'Sem pit') keep them under the 65% numeric-density rule,
# so before the trailing-run rule they reached the LLM as 'narrative' — 11% of the text.
_LEAKED_TABLE_ROWS = [
    "Uang Beredar Sem pit (M 1) 6.033,8 5.936,1 14,4 13,6",
    "Surat Berharga Selain Saham ** 53,6 64,0 (49,8) (38,2)",
    "Tabungan Rupiah Ditarik Sew aktu-w aktu 2.610,2 2.593,7 7,4 7,1",
    "Kew ajiban kepada Pem erintah Pusat 793,8 903,2 (12,1) (11,7)",
]

# Genuine BI narrative sentences — numbers are always separated by words, so the longest
# trailing numeric run is 1 and none of these may ever be dropped.
_GENUINE_PROSE = [
    "tumbuh sebesar 9,4% (yoy), melanjutkan pertumbuhan pada bulan Maret 2026 sebesar 8,9% (yoy).",
    "M2 pada April 2026 tercatat sebesar Rp10.355,1 triliun atau tumbuh 9,7% (yoy).",
    "pada Maret 2026 sebesar 9,7% (yoy) (Tabel 1 dan Grafik 2)",
    "penurunan suku bunga terjadi pada tahun 2025 dan 2026",
]


@pytest.mark.parametrize("row", _LEAKED_TABLE_ROWS)
def test_strip_tabular_drops_wordy_label_table_rows(row):
    assert _strip_tabular_content(row).strip() == ""


@pytest.mark.parametrize("sentence", _GENUINE_PROSE)
def test_strip_tabular_keeps_genuine_narrative_sentences(sentence):
    assert _strip_tabular_content(sentence).strip() == sentence


def test_strip_tabular_drops_row_of_bare_value_cells():
    # A row that is ONLY value cells (label on a previous line) is also a table row.
    assert _strip_tabular_content("53,6 64,0 (49,8)").strip() == ""


# ---------------------------------------------------------------------------
# _render_pages_to_b64 — real rendering, no mocks
# ---------------------------------------------------------------------------

def _blank_pdf_bytes(page_count: int, width: int = 200, height: int = 100) -> bytes:
    """Build a real in-memory PDF, so the render path runs against genuine bytes."""
    pdfium = pytest.importorskip("pypdfium2")
    doc = pdfium.PdfDocument.new()
    for _ in range(page_count):
        doc.new_page(width, height)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_render_pages_to_b64_produces_one_real_png_per_page():
    """Exercise the actual pypdfium2 → PIL → PNG path instead of mocking it.

    Every other vision test mocks the LLM and never reaches bitmap.to_pil(), which is why
    CI stayed green while production died with ModuleNotFoundError: No module named 'PIL' —
    pypdfium2 imports PIL lazily and does not depend on it, so a clean install had no
    Pillow. This test fails on that install, and on any future lazy-import dependency
    that goes undeclared in requirements.txt.
    """
    rendered = _render_pages_to_b64(_blank_pdf_bytes(2), dpi=144)

    assert len(rendered) == 2
    for b64_page in rendered:
        png = base64.b64decode(b64_page, validate=True)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # IHDR carries the dimensions in the 8 bytes after the chunk header: at 144 DPI a
        # 200x100 pt page (72 pt/inch) must come out exactly 2x, proving dpi is applied.
        width, height = struct.unpack(">II", png[16:24])
        assert (width, height) == (400, 200)


@patch("pdf_extraction._extract_pages_raw")
def test_extract_text_from_pdf_prefixes_each_page_with_a_marker(mock_pages):
    mock_pages.return_value = ["Page one text.", "Page two text."]

    result = extract_text_from_pdf(b"fake-pdf-bytes")

    assert result == "[== Halaman 1 ==]\nPage one text.\n\n[== Halaman 2 ==]\nPage two text."


@patch("pdf_extraction._extract_pages_raw")
def test_extract_text_from_pdf_skips_blank_pages(mock_pages):
    mock_pages.return_value = ["Real content.", "", "   "]

    result = extract_text_from_pdf(b"fake-pdf-bytes")

    assert result == "[== Halaman 1 ==]\nReal content."


@patch("pdf_extraction._extract_pages_raw")
def test_extract_text_from_pdf_returns_empty_string_when_no_text_found(mock_pages):
    # No raise here — paired_verifier.py relies on this returning a short/empty string so it
    # can trigger the vision-OCR fallback rather than catching an exception.
    mock_pages.return_value = ["", "   ", ""]

    assert extract_text_from_pdf(b"fake-pdf-bytes") == ""


@patch("pdf_extraction._extract_pages_raw")
def test_extract_text_from_pdf_returns_empty_string_when_no_pages_at_all(mock_pages):
    mock_pages.return_value = []

    assert extract_text_from_pdf(b"fake-pdf-bytes") == ""


# ---------------------------------------------------------------------------
# extract_narrative_text (pypdf, falling back to vision when too short)
# ---------------------------------------------------------------------------

@patch("pdf_extraction._count_total_pages")
@patch("pdf_extraction.extract_text_from_pdf")
def test_extract_narrative_text_skips_vision_when_pypdf_text_is_long_enough(mock_extract_text, mock_count_pages):
    mock_extract_text.return_value = "[== Halaman 1 ==]\n" + "x" * 250
    mock_count_pages.return_value = 1

    result = asyncio.run(extract_narrative_text(b"%PDF-1.4 fake", vision_llm=Mock()))

    assert result == mock_extract_text.return_value


@patch("pdf_extraction.extract_text_from_pdf_vision_async")
@patch("pdf_extraction.extract_text_from_pdf")
def test_extract_narrative_text_falls_back_to_vision_when_pypdf_text_too_short(mock_extract_text, mock_vision):
    mock_extract_text.return_value = "too short"
    mock_vision.return_value = "[== Halaman 1 ==]\n" + "y" * 250

    vision_llm = Mock()
    result = asyncio.run(extract_narrative_text(b"%PDF-1.4 fake", vision_llm=vision_llm))

    mock_vision.assert_called_once_with(b"%PDF-1.4 fake", vision_llm)
    assert result == mock_vision.return_value


@patch("pdf_extraction.extract_text_from_pdf_vision_async")
@patch("pdf_extraction.extract_text_from_pdf")
def test_extract_narrative_text_skips_vision_fallback_when_no_vision_llm_provided(mock_extract_text, mock_vision):
    mock_extract_text.return_value = "too short"

    result = asyncio.run(extract_narrative_text(b"%PDF-1.4 fake", vision_llm=None))

    mock_vision.assert_not_called()
    assert result == "too short"


@patch("pdf_extraction._count_total_pages")
@patch("pdf_extraction.extract_text_from_pdf_vision_async")
@patch("pdf_extraction.extract_text_from_pdf")
def test_extract_narrative_text_falls_back_to_vision_when_most_pages_are_blank(
    mock_extract_text, mock_vision, mock_count_pages
):
    # Chart-only PDF: pypdf clears MIN_USEFUL_CHARS in aggregate from just 2 of 10 pages
    # (stray chart-axis numbers), but 8 of 10 pages produced no text at all.
    mock_extract_text.return_value = (
        "[== Halaman 1 ==]\n" + "a" * 150 + "\n\n[== Halaman 5 ==]\n" + "b" * 150
    )
    mock_count_pages.return_value = 10
    mock_vision.return_value = "[== Halaman 1 ==]\nReal narrative text."

    vision_llm = Mock()
    result = asyncio.run(extract_narrative_text(b"%PDF-1.4 fake", vision_llm=vision_llm))

    mock_vision.assert_called_once_with(b"%PDF-1.4 fake", vision_llm)
    assert result == mock_vision.return_value


# ---------------------------------------------------------------------------
# extract_text_from_pdf_vision_async — page batching
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, content):
        self.content = content


def _count_images(message) -> int:
    return sum(1 for part in message.content if part.get("type") == "image_url")


class _FakeGoogleLLM:
    """Vision LLM stub whose class name contains 'Google' so it takes the Gemini batch path.

    Echoes one [== Halaman j ==] marker per image it receives, numbered 1..k regardless of the
    real page numbers, to prove the code renumbers markers by position (not by the model's N).
    """
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages[0])
        k = _count_images(messages[0])
        return _Resp("\n".join(f"[== Halaman {j} ==]\nNarasi gambar {j}." for j in range(1, k + 1)))


class _FakeLocalLLM:
    """Vision LLM stub with no 'Google'/'Groq' in its name → stays one page per call."""
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages[0])
        return _Resp("teks tanpa penanda")  # no marker → code must prepend it


def test_vision_async_batches_gemini_pages_and_renumbers_markers():
    b64 = [f"img{i}" for i in range(5)]  # 5 pages
    llm = _FakeGoogleLLM()
    with patch("pdf_extraction._render_pages_to_b64", return_value=b64):
        result = asyncio.run(extract_text_from_pdf_vision_async(b"pdf", llm, dpi=100))

    # 5 pages at 3/call → 2 calls (batches [1,2,3] and [4,5]).
    assert len(llm.calls) == 2
    assert _count_images(llm.calls[0]) == 3
    assert _count_images(llm.calls[1]) == 2
    # Every real page marker is present exactly once, renumbered by position even though the
    # model always numbered its output 1..k within each batch.
    for n in range(1, 6):
        assert result.count(f"[== Halaman {n} ==]") == 1
    # Second batch's text was attributed to real pages 4 and 5, not 1 and 2.
    assert "[== Halaman 4 ==]\nNarasi gambar 1." in result
    assert "[== Halaman 5 ==]\nNarasi gambar 2." in result


def test_vision_async_keeps_one_page_per_call_for_non_gemini_and_adds_markers():
    b64 = [f"img{i}" for i in range(3)]
    llm = _FakeLocalLLM()
    with patch("pdf_extraction._render_pages_to_b64", return_value=b64):
        result = asyncio.run(extract_text_from_pdf_vision_async(b"pdf", llm, dpi=100))

    assert len(llm.calls) == 3
    for call in llm.calls:
        assert _count_images(call) == 1
    for n in range(1, 4):
        assert f"[== Halaman {n} ==]\nteks tanpa penanda" in result


def test_vision_pages_per_call_env_override(monkeypatch):
    monkeypatch.setattr("pdf_extraction._VISION_PAGES_PER_CALL_ENV", 2)
    b64 = [f"img{i}" for i in range(5)]
    llm = _FakeLocalLLM()
    with patch("pdf_extraction._render_pages_to_b64", return_value=b64):
        asyncio.run(extract_text_from_pdf_vision_async(b"pdf", llm, dpi=100))

    # Env forces 2 pages/call even for the local provider → 3 calls for 5 pages.
    assert len(llm.calls) == 3
