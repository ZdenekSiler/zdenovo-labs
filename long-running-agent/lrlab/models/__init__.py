"""Model registry: `make_model("provider:model")`.

  fake                         scripted, offline, deterministic
  ollama:<model>               local (e.g. ollama:llama3.2, ollama:granite4.1:3b)
  groq:<model>                 free tier, GROQ_API_KEY
  openrouter:<model>:free      free tier, OPENROUTER_API_KEY
  gemini:<model>               free tier, GEMINI_API_KEY
  openai:<model>               paid, OPENAI_API_KEY
  claude:<model>               paid, ANTHROPIC_API_KEY (claude-opus-5-5 | claude-sonnet-5-5 | claude-haiku-5-5)
"""
from __future__ import annotations

from .base import ModelClient, ModelError, ModelReply, ToolCall  # noqa: F401


def make_model(spec: str) -> ModelClient:
    provider, _, model = spec.partition(":")
    if provider == "fake":
        from .fake import ScriptedResearcher
        return ScriptedResearcher()
    if provider == "ollama":
        from .ollama import OllamaModel
        return OllamaModel(model)
    if provider == "claude":
        from .claude import ClaudeModel
        return ClaudeModel(model or "claude-opus-5-5")
    from .openai_compat import PROVIDERS, OpenAICompatModel
    if provider in PROVIDERS:
        return OpenAICompatModel(provider, model)
    raise ValueError(f"unknown model spec {spec!r}")
