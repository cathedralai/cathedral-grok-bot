"""Chutes inference (OpenAI-compatible chat completions on Bittensor)."""
from __future__ import annotations

import json
from typing import Optional

from .http import Transport, parse_json, urllib_transport


class ChutesError(RuntimeError):
    pass


class Chutes:
    def __init__(self, api_key: str, model: str, base: str = "https://llm.chutes.ai/v1",
                 transport: Transport = urllib_transport, timeout: float = 120.0):
        self._api_key = api_key
        self.model = model
        self.base = base.rstrip("/")
        self._transport = transport
        self.timeout = timeout

    def chat(self, messages: list, tools: Optional[list] = None, max_tokens: int = 700) -> dict:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}
        status, raw = self._transport("POST", self.base + "/chat/completions", headers,
                                      json.dumps(body).encode(), self.timeout)
        data = parse_json(raw)
        if status != 200 or not data.get("choices"):
            raise ChutesError(f"chutes answered {status}: {str(data)[:200]}")
        return data["choices"][0]["message"]
