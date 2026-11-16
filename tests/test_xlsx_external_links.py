"""Workbooks that link to other workbooks carry a cached copy of each linked file inside the .xlsx
(xl/externalLinks/). SULNI's TABEL_INDONESIA holds 15 MB of them, to network files from the
2000s; opening one sheet parsed all of it (45 s, >100 MB) and 25 sheets took the 512 MB server
down (2026-11-16). The app reads only the values stored in the sheet, so the caches are skipped.

The test workbook's link cache is not even valid XML: a reader that still parses the caches fails,
one that skips them reads the sheet.
"""
import io
import zipfile

import openpyxl

import excel_parser_bi
import table_parser_generic

_LINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink"
_LINK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"


def _workbook_with_broken_link_cache() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TabI.1"
    ws.append(["Uraian", "Jan 2026", "Feb 2026"])
    ws.append(["Total", 100.0, 110.0])
    ws.append(["Pemerintah", 40.0, 45.0])
    buf = io.BytesIO()
    wb.save(buf)

    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", (
                    f'<Override PartName="/xl/externalLinks/externalLink1.xml" ContentType="{_LINK_TYPE}"/>'
                    "</Types>").encode())
            elif item.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(b"</Relationships>", (
                    f'<Relationship Id="rIdLink1" Type="{_LINK_REL}" Target="externalLinks/externalLink1.xml"/>'
                    "</Relationships>").encode())
            elif item.filename == "xl/workbook.xml":
                data = data.replace(b"</sheets>", (
                    '</sheets><externalReferences><externalReference '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                    'r:id="rIdLink1"/></externalReferences>'
                ).encode())
            dst.writestr(item, data)
        dst.writestr("xl/externalLinks/externalLink1.xml", "<externalLink><broken cache")
    return out.getvalue()


def test_generic_grid_skips_external_link_caches():
    grid = table_parser_generic._load_grid(_workbook_with_broken_link_cache(), "TabI.1")
    assert grid[1][:3] == ["Total", 100.0, 110.0]


def test_sheet_list_skips_external_link_caches():
    assert excel_parser_bi.list_sheet_names(_workbook_with_broken_link_cache()) == ["TabI.1"]


def test_bi_reader_skips_external_link_caches():
    # The BI reader may reject this small layout (ValueError); what must not happen is a failure
    # while opening the workbook, i.e. on the link cache.
    try:
        excel_parser_bi._read_xlsx(_workbook_with_broken_link_cache(), "TabI.1")
    except ValueError:
        pass
