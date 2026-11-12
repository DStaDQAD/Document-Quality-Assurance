"""A Copilot Studio agent as a LangChain chat model, over Direct Line with the agent's Web channel
security secret (no user sign-in). Spike for the model benchmark (2026-11-11): the agent is a chat
bot, not a raw model API, so

- the prompt (system + user messages) goes out as ONE message in a fresh conversation, and the
  agent's reply texts come back as the answer;
- structured output is JSON the agent is asked to write against the schema, parsed here, with one
  re-ask when the reply does not parse;
- an agent that stays silent is retried (probe 2026-11-11: one of six calls got no reply in 3 min);
- there are no token counts: the agent bills Copilot Studio credits, not tokens.
"""
import asyncio
import concurrent.futures
import json
import re
import time
from typing import Any, List, Optional

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

DIRECTLINE_URL = "https://directline.botframework.com"
_USER = {"id": "fact-checker", "name": "fact-checker"}
# Several report chunks run at once; a handful of open agent conversations at a time.
_CONCURRENCY = 4
_semaphores: dict = {}


class CopilotNoReply(RuntimeError):
    """The agent sent nothing back."""


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content or [])


def flatten(messages: List[BaseMessage]) -> str:
    parts = []
    for m in messages:
        if isinstance(m, SystemMessage):
            parts.append("[INSTRUKSI]\n" + _text(m.content))
        elif isinstance(m, AIMessage):
            parts.append("[JAWABANMU SEBELUMNYA]\n" + _text(m.content))
        else:
            parts.append(_text(m.content))
    return "\n\n".join(parts)


def extract_json(reply: str) -> str:
    s = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start, end = s.find("{"), s.rfind("}")
    return s[start:end + 1] if start != -1 and end > start else s


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(_CONCURRENCY)
    return _semaphores[loop]


class CopilotStudioChat(BaseChatModel):
    secret: str
    base_url: str = DIRECTLINE_URL
    timeout_s: float = 240.0
    max_attempts: int = 3

    @property
    def _llm_type(self) -> str:
        return "copilot-studio"

    async def _send(self, text: str) -> str:
        """One message in a new conversation; the agent's reply texts joined ('' when none came)."""
        base = self.base_url.rstrip("/") + "/v3/directline"
        async with _semaphore():
            async with httpx.AsyncClient(timeout=60.0, headers={"Authorization": f"Bearer {self.secret}"}) as c:
                conv = (await c.post(f"{base}/conversations")).raise_for_status().json()
                cid = conv["conversationId"]
                if conv.get("token"):
                    c.headers["Authorization"] = f"Bearer {conv['token']}"
                sent = (await c.post(f"{base}/conversations/{cid}/activities",
                                     json={"type": "message", "from": _USER, "text": text, "locale": "id-ID"})
                        ).raise_for_status().json()
                t0, watermark, replies = time.monotonic(), None, []
                while time.monotonic() - t0 < self.timeout_s:
                    await asyncio.sleep(1.5)
                    got = (await c.get(f"{base}/conversations/{cid}/activities",
                                       params={"watermark": watermark} if watermark else {})).raise_for_status().json()
                    watermark = got.get("watermark", watermark)
                    done = False
                    for a in got.get("activities", []):
                        if a.get("from", {}).get("id") == _USER["id"] or a.get("replyToId") != sent["id"]:
                            continue
                        if a.get("type") == "message" and a.get("text"):
                            replies.append(a["text"])
                        elif a.get("type") == "event" and a.get("name") == "turn.complete":
                            done = True
                    if done:
                        break
                return "\n".join(replies)

    async def _agenerate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                         run_manager=None, **kwargs: Any) -> ChatResult:
        text = flatten(messages)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                reply = await self._send(text)
                if reply.strip():
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])
                last_error = CopilotNoReply(f"no reply to a {len(text)}-character message")
            except httpx.HTTPError as e:  # throttling or a transient Direct Line error
                last_error = e
            if attempt + 1 < self.max_attempts:
                await asyncio.sleep(5 * (attempt + 1))
        raise last_error if isinstance(last_error, CopilotNoReply) else CopilotNoReply(str(last_error))

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager=None, **kwargs: Any) -> ChatResult:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, self._agenerate(messages, stop, **kwargs)).result()

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs: Any):
        instruction = ("Jawab HANYA dengan satu objek JSON yang valid sesuai JSON Schema berikut, tanpa teks "
                       "lain dan tanpa penjelasan:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False))

        async def run(value):
            messages = self._convert_input(value).to_messages() + [HumanMessage(content=instruction)]
            raw = await self.ainvoke(messages)
            try:
                parsed, error = schema.model_validate_json(extract_json(_text(raw.content))), None
            except ValueError as e:
                retry = messages + [raw, HumanMessage(content=f"Jawabanmu tidak bisa dibaca sebagai JSON sesuai skema ({e}). "
                                                              "Kirim ulang HANYA objek JSON-nya.")]
                raw = await self.ainvoke(retry)
                try:
                    parsed, error = schema.model_validate_json(extract_json(_text(raw.content))), None
                except ValueError as e2:
                    if not include_raw:
                        raise
                    parsed, error = None, e2
            return {"raw": raw, "parsed": parsed, "parsing_error": error} if include_raw else parsed

        def run_sync(value):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                return pool.submit(asyncio.run, run(value)).result()

        return RunnableLambda(run_sync, afunc=run)
