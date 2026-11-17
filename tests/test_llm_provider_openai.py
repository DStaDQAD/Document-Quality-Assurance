"""OpenAI (GPT-6 Luna) for the model benchmark: the same OpenAI-compatible client as OpenScale,
pointed at OpenAI with its own key."""

import importlib

import pytest
from langchain_openai import ChatOpenAI

import llm_provider


@pytest.fixture
def openai(monkeypatch):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_provider, "OPENAI_API_KEY", "test-key")
    return llm_provider


def test_get_llm_is_gpt_luna_on_openai_without_thinking(openai):
    llm = openai.get_llm()

    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "gpt-6-luna"
    assert llm.openai_api_base is None  # OpenAI's own endpoint
    assert llm.openai_api_key.get_secret_value() == "test-key"
    assert llm.reasoning_effort == "none" and llm.temperature == 0.0


def test_the_openai_review_model_thinks_a_little_and_sends_no_temperature(openai):
    # GPT-6 rejects temperature whenever reasoning effort is not "none".
    review = openai.get_review_llm()

    assert review.reasoning_effort == "low" and review.temperature is None


def test_openai_without_a_key_refuses_to_start(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    try:
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            importlib.reload(llm_provider)
    finally:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        importlib.reload(llm_provider)
