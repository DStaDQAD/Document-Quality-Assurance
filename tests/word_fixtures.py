"""In-memory EMF pictures and .docx files for the Word-reader tests.

A helper module, not a test file: pytest puts tests/ on sys.path (rootdir, no __init__.py), so
test modules import it as `from word_fixtures import ...`.
"""

import io
import struct
import zipfile
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

_EMR_HEADER, _EMR_EXTTEXTOUTW, _EMR_EOF = 1, 84, 14


def emf_bytes(runs: List[Tuple[int, int, str]]) -> bytes:
    """An EMF holding one EMR_EXTTEXTOUTW record per (x, y, text) run."""
    header = bytearray(88)
    struct.pack_into("<II", header, 0, _EMR_HEADER, 88)
    header[40:44] = b" EMF"
    records = [bytes(header)]
    for x, y, text in runs:
        encoded = text.encode("utf-16-le")
        encoded += b"\0" * (-len(encoded) % 4)
        record = bytearray(76)
        struct.pack_into("<II", record, 0, _EMR_EXTTEXTOUTW, 76 + len(encoded))
        struct.pack_into("<ii", record, 36, x, y)
        struct.pack_into("<II", record, 44, len(text), 76)
        records.append(bytes(record) + encoded)
    records.append(struct.pack("<II", _EMR_EOF, 20) + bytes(12))
    return b"".join(records)


# Shaped like Tabel 1 of the April 2026 M2 report as its EMF actually stores it: an annotation
# row, a stub header on its own line, the period row, and a label that wraps one unit above its
# values ("Uang Kartal …" at y=152, its values at y=153).
SNIPPET_RUNS = [
    (120, 5, "2026"), (300, 5, "% (yoy)"),
    (0, 19, "Komponen Uang Beredar"),
    (100, 34, "Mar"), (160, 34, "Apr*"), (240, 34, "Mar'26"), (320, 34, "Apr'26*"),
    (0, 77, "Uang Beredar Luas (M2)"), (100, 77, "10.355,7"), (160, 77, "10.253,7"),
    (240, 77, "9,7"), (320, 77, "9,2"),
    (0, 119, "Uang Beredar Sempit (M1)"), (100, 119, "6.033,8"), (160, 119, "5.936,1"),
    (240, 119, "14,4"), (320, 119, "13,6"),
    (0, 152, "Uang Kartal di Luar Bank Umum dan BPR"), (100, 153, "1.206,1"),
    (160, 153, "1.186,3"), (240, 153, "10,8"), (320, 153, "15,7"),
]

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)


def para(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def picture(rid: str) -> str:
    return (f'<w:p><w:r><w:drawing><a:graphic><a:graphicData>'
            f'<a:blip r:embed="{rid}"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>')


def docx_bytes(body_xml: str, media: Optional[Dict[str, bytes]] = None) -> bytes:
    """A minimal .docx. Media are related as rId1, rId2, … in the dict's order."""
    media = media or {}
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/'
        f'2006/relationships/image" Target="media/{name}"/>'
        for i, name in enumerate(media, 1)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?>'
                   f'<w:document {_NS}><w:body>{body_xml}</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", '<?xml version="1.0"?><Relationships xmlns='
                   f'"http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
                   '</Relationships>')
        for name, data in media.items():
            z.writestr(f"word/media/{name}", data)
    return buf.getvalue()
