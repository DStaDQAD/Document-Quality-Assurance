from unittest.mock import Mock, patch

import orchestrator
from schemas import ClaimVerificationResult


@patch("orchestrator.verify_claims")
@patch("orchestrator.build_judge_chain")
@patch("orchestrator.build_sql_chain")
@patch("orchestrator.get_readonly_db")
@patch("orchestrator.extract_claims")
@patch("orchestrator.get_llm")
def test_verify_document_builds_per_claim_report_with_status_counts(
    mock_get_llm,
    mock_extract_claims,
    mock_get_readonly_db,
    mock_build_sql_chain,
    mock_build_judge_chain,
    mock_verify_claims,
):
    mock_get_llm.return_value = Mock()
    mock_extract_claims.return_value = ["Claim A", "Claim B", "Claim C", "Claim D"]
    mock_get_readonly_db.return_value = Mock()
    mock_build_sql_chain.return_value = Mock()
    mock_build_judge_chain.return_value = Mock()
    mock_verify_claims.return_value = [
        ClaimVerificationResult(claim="Claim A", status="Entailed", sql_query_used="SELECT 1", reasoning="ok A"),
        ClaimVerificationResult(claim="Claim B", status="Refuted", sql_query_used="SELECT 2", reasoning="ok B"),
        ClaimVerificationResult(claim="Claim C", status="Error", sql_query_used=None, reasoning="pipeline exploded"),
        ClaimVerificationResult(claim="Claim D", status="Inconclusive", sql_query_used="SELECT 3", reasoning="missing comparison data"),
    ]

    report = orchestrator.verify_document("some long document")

    assert report.total_claims == 4
    assert report.entailed_count == 1
    assert report.refuted_count == 1
    assert report.inconclusive_count == 1
    assert report.error_count == 1
    assert [r.status for r in report.results] == ["Entailed", "Refuted", "Error", "Inconclusive"]
    assert report.results[2].claim == "Claim C"
    assert "pipeline exploded" in report.results[2].reasoning
    assert "4 claim(s) extracted" in report.summary
    mock_verify_claims.assert_called_once_with(
        ["Claim A", "Claim B", "Claim C", "Claim D"],
        mock_get_readonly_db.return_value,
        mock_build_sql_chain.return_value,
        mock_build_judge_chain.return_value,
    )


@patch("orchestrator.get_readonly_db")
@patch("orchestrator.extract_claims")
@patch("orchestrator.get_llm")
def test_verify_document_skips_db_setup_when_no_claims_found(
    mock_get_llm, mock_extract_claims, mock_get_readonly_db
):
    mock_get_llm.return_value = Mock()
    mock_extract_claims.return_value = []

    report = orchestrator.verify_document("a document with nothing checkable")

    assert report.total_claims == 0
    assert report.results == []
    mock_get_readonly_db.assert_not_called()
