"""
llm_providers.py — pluggable LLM backends for the semantic judge: local
Ollama (default, free, needs a pulled model) or a hosted NVIDIA NIM
endpoint (OpenAI-compatible chat completions API, needs an API key).
Both expose the same chat_json(system, user, schema) -> dict method so
lib/llm_judge.py doesn't need to know which backend it's talking to.

Security: API keys are NEVER hardcoded or written to any file here.
NvidiaNIMProvider reads its key only from the NVIDIA_API_KEY environment
variable — export it in your shell before starting the app:
  export NVIDIA_API_KEY="nvapi-..."
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

import requests


def _schema_hint(schema: dict) -> str:
    """Render a JSON schema as a plain-English field list. Ollama enforces
    the schema via grammar-constrained decoding (the `format` param); hosted
    providers using plain JSON mode don't have that guarantee, so this is
    appended to the prompt as a redundancy net telling the model exactly
    which fields to produce."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines = ["Respond with a single JSON object with exactly these fields:"]
    for name, spec in props.items():
        req = "required" if name in required else "optional — include as empty string/0 if not applicable"
        typ = spec.get("type", "any")
        enum = f", one of {spec['enum']}" if "enum" in spec else ""
        lines.append(f'  - "{name}" ({typ}{enum}, {req})')
    return "\n".join(lines)


class LLMProvider(ABC):
    label: str

    @abstractmethod
    def chat_json(self, system: str, user: str, schema: dict,
                  temperature: float | None = None, seed: int | None = None) -> dict:
        """Call the model and return the parsed JSON response object.

        temperature/seed let a caller vary sampling on retry — a retry with
        identical (temperature, seed) to the failed attempt tends to fail
        the same way for a given prompt, especially on local models."""


class OllamaProvider(LLMProvider):
    """Local Ollama server. Uses grammar-constrained decoding via the
    `format` parameter, so output is guaranteed schema-shaped JSON."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model = model
        self.url = base_url.rstrip("/") + "/api/chat"
        self.label = f"Ollama ({model})"

    def chat_json(self, system: str, user: str, schema: dict,
                  temperature: float | None = None, seed: int | None = None) -> dict:
        options = {"temperature": temperature if temperature is not None else 0.6,
                   "num_ctx": 8192, "num_predict": 1536,
                   "repeat_penalty": 1.2, "repeat_last_n": 128, "min_p": 0.05}
        if seed is not None:
            options["seed"] = seed
        resp = requests.post(
            self.url,
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,
                "stream": False,
                "options": options,
            },
            timeout=300,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        return json.loads(content)


class NvidiaNIMProvider(LLMProvider):
    """Hosted, OpenAI-compatible chat completions API
    (https://integrate.api.nvidia.com/v1/chat/completions). Requires
    NVIDIA_API_KEY in the environment. Uses JSON mode (response_format)
    rather than grammar-constrained decoding, so the schema is reinforced
    via an explicit field list appended to the prompt (see _schema_hint)."""

    def __init__(self, model: str, base_url: str = "https://integrate.api.nvidia.com/v1"):
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.label = f"NVIDIA NIM ({model})"
        self.api_key = os.environ.get("NVIDIA_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY environment variable is not set. Export it in the shell "
                "that runs the Flask app (export NVIDIA_API_KEY=\"nvapi-...\") — API keys "
                "are never read from the UI or written to disk by this app."
            )

    def chat_json(self, system: str, user: str, schema: dict,
                  temperature: float | None = None, seed: int | None = None) -> dict:
        user_with_hint = f"{user}\n\n{_schema_hint(schema)}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system + "\n\nRespond with a single JSON object only, no prose before or after it."},
                {"role": "user", "content": user_with_hint},
            ],
            "max_tokens": 2048,
            "temperature": temperature if temperature is not None else 0.2,
            "top_p": 1.0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if seed is not None:
            payload["seed"] = seed
        resp = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)


PROVIDERS = {"ollama": OllamaProvider, "nvidia_nim": NvidiaNIMProvider}


def build_provider(provider_type: str, model: str) -> LLMProvider:
    if provider_type not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider_type}'. Choices: {list(PROVIDERS)}")
    return PROVIDERS[provider_type](model)
