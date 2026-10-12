"""The Tinjauan AI stage gets its own thinking budget; everything else keeps the global one."""

from langchain_ollama import ChatOllama

import llm_provider


def test_thinking_kwargs_passes_a_budget_of_zero_or_more():
    assert llm_provider._thinking_kwargs(0) == {"thinking_budget": 0}
    assert llm_provider._thinking_kwargs(1024) == {"thinking_budget": 1024}


def test_thinking_kwargs_leaves_minus_one_to_the_model_default():
    assert llm_provider._thinking_kwargs(-1) == {}


def test_review_llm_falls_back_to_the_ordinary_model_off_google():
    # conftest forces LLM_PROVIDER=ollama
    assert isinstance(llm_provider.get_review_llm(), ChatOllama)
