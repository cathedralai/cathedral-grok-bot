"""X (Twitter) API v2: read mentions, build the thread, post replies.

`XClient` talks to the real API with OAuth 1.0a user context (the bot account's
four keys). `MockX` reads mentions from a JSON file and writes replies to a
JSONL file, so anyone can run the whole bot without X keys.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from .http import Transport, parse_json, urllib_transport

API = "https://api.x.com/2"
TWEET_FIELDS = "conversation_id,referenced_tweets,author_id,created_at,text"


def _pct(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~-._")


def oauth1_header(method: str, url: str, params: dict, consumer_key: str, consumer_secret: str,
                  token: str, token_secret: str, nonce: Optional[str] = None,
                  timestamp: Optional[str] = None) -> str:
    """RFC 5849 HMAC-SHA1. `params` are the query (and form) parameters; a JSON body is not signed."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    pairs = sorted((_pct(k), _pct(v)) for k, v in {**params, **oauth}.items())
    param_str = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join((method.upper(), _pct(url), _pct(param_str)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


class XError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class XClient:
    def __init__(self, consumer_key: str, consumer_secret: str, token: str, token_secret: str,
                 transport: Transport = urllib_transport, api: str = API):
        self._creds = (consumer_key, consumer_secret, token, token_secret)
        self._transport = transport
        self.api = api.rstrip("/")
        self._me: Optional[dict] = None

    def _call(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
        params = {k: str(v) for k, v in (params or {}).items() if v is not None}
        url = self.api + path
        headers = {"Authorization": oauth1_header(method, url, params, *self._creds)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        full = url + ("?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote) if params else "")
        status, raw = self._transport(method, full, headers, json.dumps(body).encode() if body is not None else None, 30)
        data = parse_json(raw)
        if status == 429:
            raise XError("X rate limit (429): wait and poll less often", status)
        if status not in (200, 201):
            raise XError(f"X answered {status}: {str(data)[:300]}", status)
        return data

    def me(self) -> dict:
        if self._me is None:
            self._me = self._call("GET", "/users/me")["data"]
        return self._me

    def mentions(self, since_id: Optional[str], max_pages: int = 10) -> list:
        """New mentions, oldest first, each {"id", "author", "text", "conversation_id", "replied_to"}."""
        # Collect every page before answering: answering the newest page first would move since_id past
        # older mentions on later pages and skip them for good.
        posts, token = [], None
        for _ in range(max_pages):
            data = self._call("GET", f"/users/{self.me()['id']}/mentions", {
                "since_id": since_id, "max_results": 100, "pagination_token": token,
                "tweet.fields": TWEET_FIELDS, "expansions": "author_id", "user.fields": "username"})
            posts.extend(_normalize(data))
            token = (data.get("meta") or {}).get("next_token")
            if not token:
                break
        return sorted(posts, key=lambda p: int(p["id"]))

    def get_posts(self, ids: list) -> list:
        if not ids:
            return []
        data = self._call("GET", "/tweets", {"ids": ",".join(ids), "tweet.fields": TWEET_FIELDS,
                                             "expansions": "author_id", "user.fields": "username"})
        return _normalize(data)

    def reply(self, in_reply_to: str, text: str) -> str:
        data = self._call("POST", "/tweets", body={"text": text, "reply": {"in_reply_to_tweet_id": in_reply_to}})
        return data["data"]["id"]


def _normalize(data: dict) -> list:
    users = {u["id"]: u.get("username", "") for u in (data.get("includes") or {}).get("users", [])}
    posts = []
    for t in data.get("data") or []:
        replied = next((r["id"] for r in t.get("referenced_tweets") or [] if r.get("type") == "replied_to"), None)
        posts.append({"id": t["id"], "author": users.get(t.get("author_id"), t.get("author_id", "")),
                      "text": t.get("text", ""), "conversation_id": t.get("conversation_id"),
                      "replied_to": replied})
    return posts


def build_thread(x, mention: dict, depth: int = 8) -> list:
    """The mention and the posts it replies to, oldest first, at most `depth` posts above it."""
    chain = [mention]
    parent = mention.get("replied_to")
    while parent and len(chain) <= depth:
        found = x.get_posts([parent])
        if not found:
            break
        chain.append(found[0])
        parent = found[0].get("replied_to")
    return list(reversed(chain))


class MockX:
    """Mentions from a JSON file, replies to a JSONL file. Same interface as XClient."""

    def __init__(self, mentions_file: str, replies_file: str, bot_username: str = "yourbot"):
        self.mentions_file = Path(mentions_file)
        self.replies_file = Path(replies_file)
        self._me = {"id": "0", "username": bot_username}
        self._posts: dict = {}
        self._reply_seq = 0

    def me(self) -> dict:
        return self._me

    def _load(self) -> list:
        posts = json.loads(self.mentions_file.read_text()) if self.mentions_file.exists() else []
        for p in posts:
            self._posts[str(p["id"])] = {**p, "id": str(p["id"])}
        return [self._posts[str(p["id"])] for p in posts]

    def mentions(self, since_id: Optional[str]) -> list:
        handle = "@" + self._me["username"].lower()
        found = [p for p in self._load() if handle in p.get("text", "").lower()]
        if since_id:
            found = [p for p in found if int(p["id"]) > int(since_id)]
        return sorted(found, key=lambda p: int(p["id"]))

    def get_posts(self, ids: list) -> list:
        self._load()
        return [self._posts[i] for i in ids if i in self._posts]

    def reply(self, in_reply_to: str, text: str) -> str:
        self._reply_seq += 1
        reply_id = f"mock-{int(time.time())}-{self._reply_seq}"
        with self.replies_file.open("a") as fh:
            fh.write(json.dumps({"id": reply_id, "in_reply_to": in_reply_to, "text": text}) + "\n")
        return reply_id
