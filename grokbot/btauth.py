"""btauth/1: sign Cathedral API requests with a Bittensor hotkey instead of an API key.

A hotkey that spent Gamma at Cathedral's service hotkey on netuid 94 is a
credential on its own: no account and no API key. Every request carries the
hotkey's sr25519 signature over these lines joined by a newline:

    btauth/1, sr25519, METHOD, path with query as sent, sha256(body) hex,
    nonce (Unix time in ns), sender hotkey, receiver hotkey

The key is read with the optional `bittensor-wallet` package and never leaves
this process. Nothing here reads the chain or moves funds.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable, Optional

PROTOCOL = "btauth/1"
SCHEME = "sr25519"
# Cathedral's Gamma service hotkey on netuid 94. Check it against
# https://cathedral.computer/.well-known/cathedral-services.json
SERVICE_HOTKEY = "5FQsFkui1UvS7S7Sf3JAqN9FPCR7RmufSEZTGuVPPN5EDmsN"


def payload(method: str, path: str, body: bytes, nonce_ns: int, sender: str, receiver: str) -> bytes:
    return "\n".join((
        PROTOCOL, SCHEME, method.upper(), path, hashlib.sha256(body).hexdigest(),
        str(nonce_ns), sender, receiver,
    )).encode()


class Signer:
    """Signs one request at a time. `keypair` has `ss58_address` and `sign(bytes) -> bytes`."""

    def __init__(self, keypair, receiver: str = SERVICE_HOTKEY,
                 clock: Callable[[], int] = time.time_ns) -> None:
        self.keypair = keypair
        self.receiver = receiver
        self._clock = clock
        self._last_nonce = 0

    @property
    def hotkey(self) -> str:
        return self.keypair.ss58_address

    def headers(self, method: str, path: str, body: bytes = b"") -> dict:
        # The API refuses a repeated nonce, so never reuse one even if the clock stalls.
        nonce = max(self._clock(), self._last_nonce + 1)
        self._last_nonce = nonce
        signature = bytes(self.keypair.sign(payload(method, path, body, nonce, self.hotkey, self.receiver)))
        return {
            "X-Bittensor-Version": "1",
            "X-Bittensor-Hotkey": self.hotkey,
            "X-Bittensor-Receiver": self.receiver,
            "X-Bittensor-Nonce": str(nonce),
            "X-Bittensor-Signature": "0x" + signature.hex(),
        }


def signer_from_wallet(wallet: str, hotkey: str, path: Optional[str] = None,
                       receiver: str = SERVICE_HOTKEY) -> Signer:
    try:
        from bittensor_wallet import Wallet
    except ImportError:
        raise RuntimeError("hotkey auth needs the bittensor-wallet package: pip install bittensor-wallet") from None
    kwargs = {"name": wallet, "hotkey": hotkey}
    if path:
        kwargs["path"] = path
    try:
        keypair = Wallet(**kwargs).hotkey
    except Exception as exc:  # the wallet library raises several types; never echo key material
        raise RuntimeError(f"cannot read hotkey {hotkey!r} of wallet {wallet!r}: {type(exc).__name__}") from None
    return Signer(keypair, receiver=receiver)
