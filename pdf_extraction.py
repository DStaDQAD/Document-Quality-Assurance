"""Extracts plain text from digital PDFs (i.e. PDFs with a real text layer, not scans)."""

import io

from pypdf import PdfReader


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract and concatenate text from every page of a PDF.

    Raises ValueError if no text could be extracted (e.g. a scanned/image-only PDF -
    OCR is not supported). Lets pypdf's own parsing errors propagate for corrupt/non-PDF input.
    """
    reader = PdfReader(io.BytesIO(file_bytes))
    pages_text = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p for p in pages_text if p.strip())

    if not text.strip():
        raise ValueError(
            "No extractable text found in the PDF. It may be a scanned/image-only document - "
            "OCR is not supported."
        )

    return text
