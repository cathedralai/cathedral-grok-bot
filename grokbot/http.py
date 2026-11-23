"""The one HTTP function every client uses. Tests replace it with a fake."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

USER_AGENT = "cathedral-grok-bot/0.1"

# transport(method, url, headers, body, timeout) -> (status, body bytes)
Transport = Callable[[str, str, dict, Optional[bytes], float], Tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float) -> Tuple[int, bytes]:
    headers = {"User-Agent": USER_AGENT, **headers}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def parse_json(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:500].decode(errors="replace")}
    return data if isinstance(data, dict) else {"data": data}
