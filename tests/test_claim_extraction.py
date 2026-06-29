from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

from claim_extraction import ExtractedClaims, extract_claims


def _llm_returning(claims):
    structured = RunnableLambda(lambda _prompt_value: ExtractedClaims(claims=claims))
    llm = Mock()
    llm.with_structured_output = Mock(return_value=structured)
    return llm


def test_extract_claims_strips_and_drops_blank_entries():
    llm = _llm_returning(["  Inflasi Q1 2023 sebesar 5.47%  ", "", "   ", "Suku bunga naik di Q3 2023"])

    result = extract_claims("some document text", llm)

    assert result == ["Inflasi Q1 2023 sebesar 5.47%", "Suku bunga naik di Q3 2023"]


def test_extract_claims_returns_empty_list_when_nothing_checkable():
    llm = _llm_returning([])

    result = extract_claims("a document with no checkable claims", llm)

    assert result == []


def test_extract_claims_renders_document_into_the_prompt():
    captured = {}

    def fake_structured_llm(prompt_value):
        captured["text"] = "\n".join(m.content for m in prompt_value.to_messages())
        return ExtractedClaims(claims=["x"])

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(fake_structured_llm))

    extract_claims("UNIQUE_DOCUMENT_MARKER", llm)

    assert "UNIQUE_DOCUMENT_MARKER" in captured["text"]


def test_extract_claims_requests_structured_output_for_the_right_schema():
    llm = _llm_returning(["a claim"])

    extract_claims("doc", llm)

    llm.with_structured_output.assert_called_once_with(ExtractedClaims)
