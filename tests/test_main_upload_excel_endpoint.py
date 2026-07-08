from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_upload_excel_source_endpoint_is_disabled():
    """The ground-truth upload endpoint (/api/upload-excel-source) is intentionally
    disabled for the shared, multi-division deployment so that no caller can alter
    the reference data later fact-checks are compared against. When the route is
    commented out in main.py, FastAPI returns 404 for it.

    If the endpoint is ever re-enabled (ideally behind an admin-only token),
    restore the original success/400 tests from git history."""
    response = client.post(
        "/api/upload-excel-source",
        files={
            "file": (
                "penjualan_2024.xlsx",
                b"fake xlsx bytes",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 404
