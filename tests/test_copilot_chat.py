"""A Copilot Studio agent over Direct Line as a chat model: the prompt goes out as one message, the
agent's reply comes back as text, and structured output is the JSON the agent writes, parsed."""
import asyncio

import pytest
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

import copilot_chat
import llm_provider


class _Figure(BaseModel):
    label: str
    value: float


class _Figures(BaseModel):
    items: list[_Figure]


def _model(replies):
    """A model whose agent answers with the given texts in turn, recording what it was sent."""
    sent = []
    queue = list(replies)

    async def fake_send(text):
        sent.append(text)
        return queue.pop(0)

    m = copilot_chat.CopilotStudioChat(secret="s")
    m._send = fake_send  # noqa: SLF001 - the transport seam
    return m, sent


def test_system_and_user_messages_go_out_as_one_message():
    m, sent = _model(["oke"])
    prompt = ChatPromptTemplate.from_messages([("system", "Kamu pemeriksa angka."), ("human", "Cek {x}.")])

    out = asyncio.run((prompt | m).ainvoke({"x": "M2"}))

    assert out.content == "oke"
    assert len(sent) == 1 and "Kamu pemeriksa angka." in sent[0] and "Cek M2." in sent[0]


def test_structured_output_parses_the_json_the_agent_writes_even_in_a_code_fence():
    m, sent = _model(['```json\n{"items": [{"label": "M2 yoy", "value": 8.3}]}\n```'])

    out = asyncio.run(m.with_structured_output(_Figures).ainvoke("Daftar angka: M2 tumbuh 8,3%."))

    assert out == _Figures(items=[_Figure(label="M2 yoy", value=8.3)])
    assert '"items"' in sent[0]  # the schema travels with the prompt


def test_a_reply_that_is_not_the_schema_is_asked_once_more():
    m, sent = _model(["Maaf, saya tidak bisa.", '{"items": []}'])

    out = asyncio.run(m.with_structured_output(_Figures).ainvoke("Daftar angka."))

    assert out == _Figures(items=[]) and len(sent) == 2


def test_the_copilot_provider_is_the_agent_for_both_stages(monkeypatch):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "copilot")
    monkeypatch.setattr(llm_provider, "COPILOT_DIRECTLINE_SECRET", "s")

    assert isinstance(llm_provider.get_llm(), copilot_chat.CopilotStudioChat)
    assert isinstance(llm_provider.get_review_llm(), copilot_chat.CopilotStudioChat)


def test_no_reply_is_an_error_not_an_empty_answer():
    m, _ = _model([""])
    m.max_attempts = 1
    with pytest.raises(copilot_chat.CopilotNoReply):
        asyncio.run(m.ainvoke("halo"))
