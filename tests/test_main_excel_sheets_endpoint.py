import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

import main
from excel_parser_bi import list_sheet_names

client = TestClient(main.app)


def _make_xlsx(names) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for n in names:
        wb.create_sheet(n)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_list_sheet_names_reads_xlsx_metadata():
    data = _make_xlsx(["I.1", "II.1", "Ringkasan"])
    assert list_sheet_names(data) == ["I.1", "II.1", "Ringkasan"]


def test_list_sheet_names_rejects_non_excel():
    with pytest.raises(ValueError):
        list_sheet_names(b"this is not an excel file")


def test_excel_sheets_endpoint_returns_sheet_list():
    data = _make_xlsx(["I.1", "II.1"])
    resp = client.post(
        "/api/excel-sheets",
        files={
            "file": (
                "tabel.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"sheets": ["I.1", "II.1"]}


def test_excel_sheets_endpoint_400_on_unreadable_file():
    resp = client.post(
        "/api/excel-sheets",
        files={"file": ("bad.xlsx", b"garbage bytes", "application/octet-stream")},
    )
    assert resp.status_code == 400
