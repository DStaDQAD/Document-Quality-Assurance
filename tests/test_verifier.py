from unittest.mock import Mock

import pytest
from langchain_core.runnables import RunnableLambda

from verifier import (
    BatchSqlQueries,
    BatchVerdicts,
    IndexedSqlQuery,
    IndexedVerdict,
    generate_and_run_sql_batch,
    verify_claim,
    verify_claims,
)


def _sql_chain_returning(responses):
    """A fake sql_chain that returns each BatchSqlQueries in `responses` on successive invocations."""
    calls = iter(responses)
    return RunnableLambda(lambda _input: next(calls))


def _fake_db(schema="CREATE TABLE indikator_ekonomi (...)", run_results=None, run_side_effect=None):
    db = Mock()
    db.get_table_info.return_value = schema
    if run_side_effect is not None:
        db.run.side_effect = run_side_effect
    else:
        db.run.return_value = run_results
    return db


def test_generate_and_run_sql_batch_succeeds_on_first_try():
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun=2023")])]
    )
    db = _fake_db(run_results="[(5.47,)]")

    states = generate_and_run_sql_batch(["Inflasi 2023 adalah 5.47%"], db, sql_chain)

    assert len(states) == 1
    assert states[0].sql_query == "SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun=2023"
    assert states[0].result == "[(5.47,)]"
    assert states[0].error is None
    db.run.assert_called_once()


def test_generate_and_run_sql_batch_only_retries_failed_claims():
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(
                queries=[
                    IndexedSqlQuery(claim_index=0, sql_query="SELECT ok FROM indikator_ekonomi"),
                    IndexedSqlQuery(claim_index=1, sql_query="SELEKT bad syntax"),
                ]
            ),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=1, sql_query="SELECT fixed FROM indikator_ekonomi")]),
        ]
    )
    db = _fake_db(run_side_effect=["[(1,)]", Exception("syntax error"), "[(2,)]"])

    states = generate_and_run_sql_batch(["Claim A", "Claim B"], db, sql_chain)

    assert states[0].sql_query == "SELECT ok FROM indikator_ekonomi"
    assert states[0].error is None
    assert states[1].sql_query == "SELECT fixed FROM indikator_ekonomi"
    assert states[1].result == "[(2,)]"
    assert states[1].error is None
    assert db.run.call_count == 3


def test_generate_and_run_sql_batch_retries_comparison_claim_with_only_one_row():
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun=2023 AND kuartal='Q1'")]),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun=2023")]),
        ]
    )
    db = _fake_db(run_side_effect=["[(5.47,)]", "[(5.47,), (3.52,), (2.28,), (2.61,)]"])

    states = generate_and_run_sql_batch(
        ["Inflasi Q1 2023 adalah yang tertinggi dibandingkan kuartal lain di tahun yang sama"], db, sql_chain
    )

    assert states[0].error is None
    assert states[0].result == "[(5.47,), (3.52,), (2.28,), (2.61,)]"
    assert db.run.call_count == 2


def test_generate_and_run_sql_batch_does_not_retry_point_claim_with_one_row():
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun=2023 AND kuartal='Q1'")])]
    )
    db = _fake_db(run_results="[(5.47,)]")

    states = generate_and_run_sql_batch(["Inflasi Q1 2023 sebesar 5.47%"], db, sql_chain)

    assert states[0].error is None
    db.run.assert_called_once()


def test_generate_and_run_sql_batch_does_not_retry_single_row_with_two_aliased_columns():
    # A self-join/CASE-pivot query answering a comparison with one row but two non-null columns
    # (e.g. `semester_2_avg, semester_1_avg`) is a valid shape the prompt explicitly encourages -
    # it must not be treated the same as a single bare value.
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT semester_2_avg, semester_1_avg FROM excel_facts")])]
    )
    db = _fake_db(run_results="[(150.0, 135.0)]")

    states = generate_and_run_sql_batch(
        ["Rata-rata penjualan semester 2 lebih tinggi dibanding semester 1 untuk produk a"], db, sql_chain
    )

    assert states[0].error is None
    db.run.assert_called_once()


def test_generate_and_run_sql_batch_retries_comparison_claim_with_all_null_result():
    # Reproduces the wrong-source_file/sheet guess bug: the query executes fine but every value is
    # NULL because the WHERE clause silently excluded all matching rows.
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT AVG(...) FROM excel_facts WHERE source_file = 'wrong.xlsx'")]),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT AVG(...) FROM excel_facts WHERE source_file = 'penjualan_2024.xlsx'")]),
        ]
    )
    db = _fake_db(run_side_effect=["[(None, None)]", "[(150.0, 135.0)]"])

    states = generate_and_run_sql_batch(
        ["Rata-rata penjualan semester 2 lebih tinggi dibanding semester 1 untuk produk a"], db, sql_chain
    )

    assert states[0].error is None
    assert states[0].result == "[(150.0, 135.0)]"
    assert db.run.call_count == 2


def test_generate_and_run_sql_batch_marks_claim_as_error_when_retry_is_still_insufficient():
    # The retried query also only manages to produce an all-NULL row (e.g. it guessed the wrong
    # filter again) - this must surface as an Error rather than silently reaching the judge as if
    # it were legitimate empty data.
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT AVG(...) FROM excel_facts WHERE source_file = 'wrong.xlsx'")]),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT AVG(...) FROM excel_facts WHERE source_file = 'still_wrong.xlsx'")]),
        ]
    )
    db = _fake_db(run_side_effect=["[(None, None)]", "[(None, None)]"])

    states = generate_and_run_sql_batch(
        ["Rata-rata penjualan semester 2 lebih tinggi dibanding semester 1 untuk produk a"], db, sql_chain
    )

    assert states[0].error is not None
    assert "still didn't retrieve enough data" in states[0].error


def test_generate_and_run_sql_batch_marks_claim_as_failed_after_two_attempts():
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELEKT bad")]),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELEKT still bad")]),
        ]
    )
    db = _fake_db(run_side_effect=[Exception("syntax error 1"), Exception("syntax error 2")])

    states = generate_and_run_sql_batch(["some claim"], db, sql_chain)

    assert "Failed to generate a working SQL query after 2 attempts" in states[0].error


def test_verify_claims_builds_results_from_query_results_and_judge_output():
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun=2023 AND kuartal='Q1'")])]
    )
    db = _fake_db(run_results="[(5.47,)]")

    judge_chain = Mock()
    judge_chain.invoke.return_value = BatchVerdicts(
        verdicts=[IndexedVerdict(claim_index=0, status="Entailed", reasoning="Data matches the claim (5.47%).")]
    )

    results = verify_claims(["Inflasi Q1 2023 sebesar 5.47%"], db, sql_chain, judge_chain)

    assert len(results) == 1
    assert results[0].status == "Entailed"
    assert "5.47" in results[0].reasoning
    assert "indikator_ekonomi" in results[0].sql_query_used
    items_block = judge_chain.invoke.call_args[0][0]["items_block"]
    assert "Inflasi Q1 2023 sebesar 5.47%" in items_block
    assert "[(5.47,)]" in items_block


def test_verify_claims_marks_claim_as_error_when_sql_generation_fails_twice():
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELEKT bad")]),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELEKT still bad")]),
        ]
    )
    db = _fake_db(run_side_effect=[Exception("boom"), Exception("boom again")])
    judge_chain = Mock()

    results = verify_claims(["A claim"], db, sql_chain, judge_chain)

    assert results[0].status == "Error"
    assert "Failed to generate a working SQL query" in results[0].reasoning
    judge_chain.invoke.assert_not_called()


def test_verify_claims_passes_through_inconclusive_verdict():
    # No comparison keywords in the claim, so the SQL retry guard doesn't kick in - this isolates
    # pure pass-through of an Inconclusive verdict from the judge (e.g. for a claim the schema
    # simply can't answer), independent of the comparison-claim retry behavior tested elsewhere.
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT inflasi_persen FROM indikator_ekonomi WHERE tahun = 2023 AND kuartal = 'Q3'")])]
    )
    db = _fake_db(run_results="[]")
    judge_chain = Mock()
    judge_chain.invoke.return_value = BatchVerdicts(
        verdicts=[
            IndexedVerdict(
                claim_index=0,
                status="Inconclusive",
                reasoning="The query returned no rows, so there is no data to confirm or contradict the claim.",
            )
        ]
    )

    results = verify_claims(["Inflasi Q3 2023 sebesar 2.28%"], db, sql_chain, judge_chain)

    assert results[0].status == "Inconclusive"
    assert "no rows" in results[0].reasoning


def test_verify_claims_marks_claim_as_error_when_judge_fails():
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT 1 FROM indikator_ekonomi")])]
    )
    db = _fake_db(run_results="some data")
    judge_chain = Mock()
    judge_chain.invoke.side_effect = Exception("judge crashed")

    results = verify_claims(["A claim"], db, sql_chain, judge_chain)

    assert results[0].status == "Error"
    assert "judge crashed" in results[0].reasoning


def test_verify_claims_isolates_claims_from_each_other():
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(
                queries=[
                    IndexedSqlQuery(claim_index=0, sql_query="SELECT ok FROM indikator_ekonomi"),
                    IndexedSqlQuery(claim_index=1, sql_query="SELEKT bad"),
                ]
            ),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=1, sql_query="SELEKT still bad")]),
        ]
    )
    db = _fake_db(run_side_effect=["[(1,)]", Exception("bad"), Exception("still bad")])
    judge_chain = Mock()
    judge_chain.invoke.return_value = BatchVerdicts(
        verdicts=[IndexedVerdict(claim_index=0, status="Entailed", reasoning="ok")]
    )

    results = verify_claims(["Claim A", "Claim B"], db, sql_chain, judge_chain)

    assert results[0].status == "Entailed"
    assert results[1].status == "Error"


def test_verify_claim_single_claim_wrapper_returns_response_on_success():
    sql_chain = _sql_chain_returning(
        [BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELECT 1 FROM indikator_ekonomi")])]
    )
    db = _fake_db(run_results="[(5.47,)]")
    judge_chain = Mock()
    judge_chain.invoke.return_value = BatchVerdicts(
        verdicts=[IndexedVerdict(claim_index=0, status="Entailed", reasoning="matches")]
    )

    result = verify_claim("A claim", db, sql_chain, judge_chain)

    assert result.status == "Entailed"
    assert result.sql_query_used == "SELECT 1 FROM indikator_ekonomi"


def test_verify_claim_single_claim_wrapper_raises_on_error():
    sql_chain = _sql_chain_returning(
        [
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELEKT bad")]),
            BatchSqlQueries(queries=[IndexedSqlQuery(claim_index=0, sql_query="SELEKT still bad")]),
        ]
    )
    db = _fake_db(run_side_effect=[Exception("boom"), Exception("boom again")])
    judge_chain = Mock()

    with pytest.raises(RuntimeError, match="Failed to generate a working SQL query"):
        verify_claim("A claim", db, sql_chain, judge_chain)
