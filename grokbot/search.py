"""Brave Search from the bot host (optional, BRAVE_API_KEY).

It runs here, not in the sandbox, so the key never reaches the place where
model-written code runs. Only search results (data) go back to the model.
"""
from __future__ import annotations

import re
import urllib.parse

from .http import Transport, parse_json, urllib_transport


class BraveError(RuntimeError):
    pass


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


class BraveSearch:
    def __init__(self, api_key: str, transport: Transport = urllib_transport, limit: int = 5):
        self._api_key = api_key
        self._transport = transport
        self.limit = limit

    def search(self, query: str) -> list:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": self.limit})
        status, raw = self._transport("GET", url, {"Accept": "application/json",
                                                   "X-Subscription-Token": self._api_key}, None, 15)
        if status != 200:
            raise BraveError(f"brave answered {status}")
        data = parse_json(raw)
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": _plain(r.get("description", ""))}
                for r in (data.get("web") or {}).get("results", [])[: self.limit]]
