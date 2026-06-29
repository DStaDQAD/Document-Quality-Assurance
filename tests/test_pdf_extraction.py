from unittest.mock import Mock, patch

import pytest

from pdf_extraction import extract_text_from_pdf


def _reader_with_pages(texts):
    pages = []
    for text in texts:
        page = Mock()
        page.extract_text.return_value = text
        pages.append(page)
    reader = Mock()
    reader.pages = pages
    return reader


@patch("pdf_extraction.PdfReader")
def test_extract_text_from_pdf_joins_pages_with_blank_line(mock_pdf_reader):
    mock_pdf_reader.return_value = _reader_with_pages(["Page one text.", "Page two text."])

    result = extract_text_from_pdf(b"fake-pdf-bytes")

    assert result == "Page one text.\n\nPage two text."


@patch("pdf_extraction.PdfReader")
def test_extract_text_from_pdf_skips_blank_pages(mock_pdf_reader):
    mock_pdf_reader.return_value = _reader_with_pages(["Real content.", "", "   "])

    result = extract_text_from_pdf(b"fake-pdf-bytes")

    assert result == "Real content."


@patch("pdf_extraction.PdfReader")
def test_extract_text_from_pdf_raises_when_no_text_found(mock_pdf_reader):
    mock_pdf_reader.return_value = _reader_with_pages(["", "   ", None])

    with pytest.raises(ValueError, match="No extractable text found"):
        extract_text_from_pdf(b"fake-pdf-bytes")


@patch("pdf_extraction.PdfReader")
def test_extract_text_from_pdf_raises_when_no_pages_at_all(mock_pdf_reader):
    mock_pdf_reader.return_value = _reader_with_pages([])

    with pytest.raises(ValueError, match="No extractable text found"):
        extract_text_from_pdf(b"fake-pdf-bytes")
