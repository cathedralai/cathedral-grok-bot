"""Cathedral confidential sandboxes over the public HTTPS API.

One room per answer: create, run the agent's tool calls with exec, delete.
Auth is an API key (`Authorization: Bearer`) or a Bittensor hotkey (btauth/1).
Contract: https://cathedral.computer/agents.md
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .btauth import Signer
from .http import Transport, parse_json, urllib_transport

READY_STATES = {"running"}
DEAD_STATES = {"failed", "error", "deleted", "deleting", "stopped", "expired"}
# 503 codes that mean "sold out right now, nothing charged": retry with a NEW idempotency key.
SOLD_OUT = {"sealed_capacity_unavailable", "sandbox_capacity_unavailable", "sealed_pool_throttled",
            "sealed_pool_unavailable"}


class CathedralError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "", body: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body or {}


@dataclass
class Room:
    id: str
    hotel: Optional[str]
    receipt_url: Optional[str]
    created_s: float
    exec_receipts: list = field(default_factory=list)


def _error_code(body: dict) -> str:
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("error") or detail.get("code") or "")
    return str(body.get("error") or body.get("error_code") or "")


class Cathedral:
    def __init__(self, api_base: str, api_key: Optional[str] = None, signer: Optional[Signer] = None,
                 transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, public_base: str = "https://cathedral.computer"):
        if bool(api_key) == bool(signer):
            raise ValueError("give exactly one of api_key or signer")
        self.api_base = api_base.rstrip("/")
        self.public_base = public_base.rstrip("/")
        self._api_key = api_key
        self._signer = signer
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    # -- transport ---------------------------------------------------------
    def call(self, method: str, path: str, body: Optional[dict] = None, idempotency_key: Optional[str] = None,
             timeout: float = 90.0) -> tuple:
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if self._signer is not None:
            # btauth/1 signs the path and query exactly as sent, and the exact body bytes.
            headers.update(self._signer.headers(method, path, raw))
        else:
            headers["Authorization"] = "Bearer " + self._api_key
        status, data = self._transport(method, self.api_base + path, headers, raw if body is not None else None, timeout)
        return status, parse_json(data)

    # -- rooms -------------------------------------------------------------
    def create_room(self, template: str = "sealed-1x2", network: str = "internet", lifetime_seconds: int = 600,
                    max_spend_usd: str = "0.002", labels: Optional[dict] = None, wait_seconds: float = 120.0) -> Room:
        body = {"required_hardware": "amd_sev_snp", "template": template, "network": network,
                "lifetime_seconds": lifetime_seconds, "max_spend_usd": max_spend_usd,
                "labels": labels or {"job": "grok-bot"}}
        t0 = self._clock()
        key = "grokbot-" + uuid.uuid4().hex
        data: dict = {}
        for attempt in range(4):
            status, data = self.call("POST", "/v1/sandboxes", body, idempotency_key=key)
            if status in (200, 201, 202):
                break
            code = _error_code(data)
            if status == 503 and code in SOLD_OUT and not (code == "sandbox_capacity_unavailable"
                                                           and (data.get("detail") or {}).get("reason") == "sealed_only"):
                key = "grokbot-" + uuid.uuid4().hex  # the same key would answer the same refusal
            elif status == 0 or status >= 500:
                pass  # lost answer or server error: resend the identical request with the same key
            else:
                raise CathedralError(f"create refused: {status} {code}", status, code, data)
            if attempt == 3:
                raise CathedralError(f"create failed after retries: {status} {code}", status, code, data)
            self._sleep(min(float(data.get("retry_after_s") or 2 ** attempt), 10.0))
        room_id = data["id"]
        state = data.get("state")
        while state not in READY_STATES:
            if state in DEAD_STATES:
                raise CathedralError(f"room {room_id} is {state}", 0, data.get("error_code") or "", data)
            if self._clock() - t0 > wait_seconds:
                self.delete_room(room_id)
                raise CathedralError(f"room {room_id} not running after {wait_seconds:.0f}s")
            self._sleep(0.5)
            _, data = self.call("GET", f"/v1/sandboxes/{room_id}")
            state = data.get("state")
        if data.get("hardware") not in (None, "amd_sev_snp"):
            self.delete_room(room_id)
            raise CathedralError(f"room {room_id} is not amd_sev_snp: {data.get('hardware')}")
        return Room(id=room_id, hotel=data.get("hotel"), receipt_url=self.public_url(data.get("receipt_url")),
                    created_s=self._clock() - t0)

    def exec(self, room: Room, command, stdin: Optional[str] = None, timeout_seconds: int = 40,
             env: Optional[dict] = None) -> dict:
        body = {"command": command, "timeout_seconds": timeout_seconds}
        if stdin is not None:
            body["stdin"] = stdin
        if env:
            body["env"] = env  # this one command only; receipts record names and hashes, never values
        status, data = self.call("POST", f"/v1/sandboxes/{room.id}/exec", body,
                                 idempotency_key="grokbot-exec-" + uuid.uuid4().hex, timeout=timeout_seconds + 30)
        if status != 200:
            raise CathedralError(f"exec refused: {status} {_error_code(data)}", status, _error_code(data), data)
        url = self.public_url(data.get("room_receipt_url"))
        if url:
            room.exec_receipts.append(url)
        return data

    def delete_room(self, room_id: str) -> int:
        key = "grokbot-del-" + uuid.uuid4().hex
        status = 0
        for attempt in range(3):
            status, _ = self.call("DELETE", f"/v1/sandboxes/{room_id}", idempotency_key=key)
            if status in (200, 202, 204, 404, 410):
                return status
            self._sleep(2 ** attempt)
        return status

    def public_url(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return path if path.startswith("http") else self.public_base + path
