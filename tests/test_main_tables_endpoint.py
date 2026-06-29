from unittest.mock import patch

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@patch("main.list_tables")
def test_list_tables_endpoint_returns_table_names(mock_list_tables):
    mock_list_tables.return_value = ["excel_facts", "indikator_ekonomi"]

    response = client.get("/api/tables")

    assert response.status_code == 200
    assert response.json() == {"tables": ["excel_facts", "indikator_ekonomi"]}


@patch("main.fetch_table_rows")
@patch("main.list_tables")
def test_get_table_data_endpoint_returns_rows_for_a_known_table(mock_list_tables, mock_fetch_table_rows):
    mock_list_tables.return_value = ["excel_facts"]
    mock_fetch_table_rows.return_value = (["row_label", "value"], [("Surabaya", 120.0)], 1)

    response = client.get("/api/tables/excel_facts?limit=50&offset=0")

    assert response.status_code == 200
    assert response.json() == {
        "table": "excel_facts",
        "columns": ["row_label", "value"],
        "rows": [["Surabaya", 120.0]],
        "total_rows": 1,
        "limit": 50,
        "offset": 0,
    }
    mock_fetch_table_rows.assert_called_once_with("excel_facts", limit=50, offset=0)


@patch("main.list_tables")
def test_get_table_data_endpoint_404s_for_an_unknown_table(mock_list_tables):
    mock_list_tables.return_value = ["excel_facts"]

    response = client.get("/api/tables/not_a_real_table")

    assert response.status_code == 404


@patch("main.list_tables")
def test_get_table_data_endpoint_clamps_limit(mock_list_tables):
    mock_list_tables.return_value = ["excel_facts"]

    with patch("main.fetch_table_rows", return_value=([], [], 0)) as mock_fetch_table_rows:
        response = client.get("/api/tables/excel_facts?limit=5000&offset=-10")

    assert response.status_code == 200
    mock_fetch_table_rows.assert_called_once_with("excel_facts", limit=1000, offset=0)
