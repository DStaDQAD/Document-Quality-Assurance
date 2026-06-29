"""LLM provider selection (Google Gemini / Groq / Ollama) driven by environment config."""

import os

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

_PROVIDER_KEYS = {"google": GOOGLE_API_KEY, "groq": GROQ_API_KEY, "ollama": None}

if LLM_PROVIDER not in _PROVIDER_KEYS:
    raise RuntimeError(f"Unsupported LLM_PROVIDER '{LLM_PROVIDER}'. Use 'google', 'groq', or 'ollama'.")

# Ollama runs locally and needs no API key - only cloud providers are checked here.
if LLM_PROVIDER != "ollama":
    _active_key = _PROVIDER_KEYS[LLM_PROVIDER]
    if not _active_key or _active_key.startswith("your_"):
        raise RuntimeError(
            f"{LLM_PROVIDER.upper()}_API_KEY is missing or still a placeholder. "
            "Set it in your .env file before starting the server."
        )


def get_llm(temperature: float = 0.0) -> BaseChatModel:
    if LLM_PROVIDER == "groq":
        return ChatGroq(model=GROQ_MODEL, temperature=temperature, groq_api_key=GROQ_API_KEY)
    if LLM_PROVIDER == "ollama":
        return ChatOllama(model=OLLAMA_MODEL, temperature=temperature, base_url=OLLAMA_BASE_URL)
    return ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        temperature=temperature,
        google_api_key=GOOGLE_API_KEY,
    )
