"""OpenScale (DeepSeek V4.1 Flash over an OpenAI-compatible API) as a text provider, and the switch
that lets claim extraction run on the text model while PDF pages stay with the vision model."""
import asyncio
import importlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_openai import ChatOpenAI

import llm_provider
from structured_extractor import extraction_llms


@pytest.fixture
def openscale(monkeypatch):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "openscale")
    monkeypatch.setattr(llm_provider, "OPENSCALE_API_KEY", "test-key")
    return llm_provider


def test_get_llm_is_deepseek_on_openscale_without_thinking(openscale):
    llm = openscale.get_llm()

    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "deepseek/deepseek-v4.1-flash"
    assert llm.openai_api_base == "https://api.openscale.so/v1"
    assert llm.reasoning_effort == "none" and llm.temperature == 0.0


def test_the_review_model_thinks_a_little(openscale):
    assert openscale.get_review_llm().reasoning_effort == "low"


def test_openscale_without_a_key_refuses_to_start(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openscale")
    monkeypatch.setenv("OPENSCALE_API_KEY", "")
    try:
        with pytest.raises(RuntimeError, match="OPENSCALE_API_KEY"):
            importlib.reload(llm_provider)
    finally:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        importlib.reload(llm_provider)


@pytest.mark.parametrize("setting, expected", [("", "vision"), ("vision", "vision"), ("text", "text"), (" TEXT ", "text")])
def test_extraction_runs_on_the_vision_model_unless_told_otherwise(monkeypatch, setting, expected):
    monkeypatch.setenv("EXTRACTION_LLM", setting)
    text, vision = Mock(name="text"), Mock(name="vision")

    primary, fallback = extraction_llms(text, vision)

    assert (primary, fallback) == ((vision, text) if expected == "vision" else (text, None))


def test_without_a_vision_model_extraction_uses_the_text_model(monkeypatch):
    monkeypatch.delenv("EXTRACTION_LLM", raising=False)
    text = Mock(name="text")

    assert extraction_llms(text, None) == (text, None)


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_extracts_with_the_text_model_when_told(mock_parse, mock_extract, monkeypatch):
    from excel_parser_bi import BITableData
    from paired_verifier import verify_paired

    monkeypatch.setenv("EXTRACTION_LLM", "text")
    monkeypatch.delenv("STRICT_EXTRACTION", raising=False)
    table = BITableData(title="PMI", unit="%, Indeks", row_labels=["PMI - BI"])
    table._data[("PMI - BI", 2026, "Q1")] = 52.03
    mock_parse.return_value = table
    text, vision = Mock(name="text"), Mock(name="vision")

    asyncio.run(verify_paired(narrative_text="[== Halaman 1 ==]\nPMI-BI tercatat 52,03%.\n",
                              excel_sources=[(b"x", "T1", "t.xlsx")], llm=text, vision_llm=vision))

    assert mock_extract.call_args.args[2] is text
    assert mock_extract.call_args.kwargs["fallback_llm"] is None


def test_each_model_gets_its_own_async_connection(openscale):
    # Regression run 2026-10-26: langchain-openai shared one async HTTP client across requests;
    # the test client runs each request on a new event loop, so later documents lost chunks to
    # "Event loop is closed" and came back with half their claims.
    first, second = openscale.get_llm(), openscale.get_llm()

    assert first.root_async_client is not second.root_async_client
    assert first.root_async_client._client is not second.root_async_client._client
