"""The bot: poll mentions, answer each in its own Cathedral room, reply with the receipt link.

    python -m grokbot --ask "what is 2**64?"     one question, no X at all
    python -m grokbot --mock --once               answer examples/mentions.json once
    python -m grokbot                             run against X until stopped
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from .agent import Agent, AgentConfig, Answer
from .btauth import signer_from_wallet
from .cathedral import Cathedral, CathedralError
from .chutes import Chutes, ChutesError
from .search import BraveSearch
from .x_api import MockX, XClient, XError, build_thread

log = logging.getLogger("grokbot")

X_LIMIT = 280
X_URL_LEN = 23  # X counts every link as 23 characters
RECEIPT_PREFIX = "Ran in a confidential Cathedral sandbox. Receipt: "


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or default


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _weight(ch: str) -> int:
    """X counts these ranges as 1 and every other character (CJK, emoji) as 2."""
    cp = ord(ch)
    return 1 if cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F or 0x2032 <= cp <= 0x2037 else 2


def x_length(text: str) -> int:
    """X's weighted length: links count as 23, CJK and emoji as 2."""
    total = 0
    for token in re.split(r"(\s+)", text):
        if token.startswith(("http://", "https://")):
            total += X_URL_LEN
        else:
            total += sum(_weight(c) for c in token)
    return total


def _fit(text: str, limit: int) -> str:
    """The longest prefix of text whose weighted length is at most limit."""
    used, out = 0, []
    for ch in text:
        used += _weight(ch)
        if used > limit:
            break
        out.append(ch)
    return "".join(out)


def format_replies(answer: Answer, max_posts: int = 4) -> list:
    """Split the answer into posts that fit X; the receipt link closes the last one."""
    footer = RECEIPT_PREFIX + answer.receipt_url if answer.receipt_url else ""
    words = " ".join(answer.text.split()).split(" ")
    posts, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if x_length(candidate) <= X_LIMIT:
            current = candidate
            continue
        if current:
            posts.append(current)
        # A single word longer than a post (or text with no spaces, such as Chinese) is cut by weight.
        while x_length(word) > X_LIMIT:
            head = _fit(word, X_LIMIT)
            posts.append(head)
            word = word[len(head):]
        current = word
    if current:
        posts.append(current)
    if len(posts) > max_posts:
        posts = posts[:max_posts]
        posts[-1] = _fit(posts[-1], X_LIMIT - 2).rstrip() + "\u2026"
    if footer:
        if posts and x_length(posts[-1] + "\n\n" + footer) <= X_LIMIT:
            posts[-1] = posts[-1] + "\n\n" + footer
        else:
            posts.append(footer)
    return posts or ["I could not work that out."]


class RateLimit:
    """At most `per_hour` answers per author, and `total_per_hour` overall."""

    def __init__(self, per_hour: int, total_per_hour: int, clock=time.monotonic):
        self.per_hour, self.total_per_hour, self.clock = per_hour, total_per_hour, clock
        self.by_author: dict = defaultdict(deque)
        self.total: deque = deque()

    def check(self, author: str) -> str:
        """"ok" (and counts it), "author" (this author is over the limit) or "total" (the bot is)."""
        now = self.clock()
        for q in (self.by_author[author], self.total):
            while q and now - q[0] > 3600:
                q.popleft()
        if len(self.total) >= self.total_per_hour:
            return "total"
        if len(self.by_author[author]) >= self.per_hour:
            return "author"
        self.by_author[author].append(now)
        self.total.append(now)
        return "ok"

    def allow(self, author: str) -> bool:
        return self.check(author) == "ok"


class Bot:
    def __init__(self, x, agent: Agent, state_file: str, limit: RateLimit):
        self.x, self.agent, self.state_file, self.limit = x, agent, Path(state_file), limit

    def _since_id(self) -> Optional[str]:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text()).get("since_id")
        return None

    def _save(self, since_id: str) -> None:
        self.state_file.write_text(json.dumps({"since_id": since_id}))

    def poll_once(self) -> int:
        """Answer every new mention once. Returns how many were answered."""
        me = self.x.me()
        answered = 0
        for mention in self.x.mentions(self._since_id()):
            if mention.get("author", "").lower() == me.get("username", "").lower():
                self._save(mention["id"])
                continue
            verdict = self.limit.check(mention.get("author", ""))
            if verdict == "total":
                # The bot's own hourly cap: stop here without moving since_id, so this mention and the
                # ones after it are answered in a later poll instead of being dropped.
                log.info("hourly answer cap reached; %s and later mentions wait", mention["id"])
                break
            # Save before answering: a mention that crashes the bot is skipped, not retried forever.
            self._save(mention["id"])
            if verdict == "author":
                log.info("@%s is over its hourly limit, skipping %s", mention.get("author"), mention["id"])
                continue
            try:
                thread = build_thread(self.x, mention)
                answer = self.agent.answer(thread, question_id=mention["id"])
            except (CathedralError, ChutesError, XError) as exc:
                log.error("could not answer %s: %s", mention["id"], exc)
                continue
            parent = mention["id"]
            try:
                for text in format_replies(answer):
                    parent = self.x.reply(parent, text)
            except XError as exc:
                # Keep going with the other mentions; the answer and its receipt are in this log line.
                log.error("reply to %s failed (%s); answer was: %r receipt=%s", mention["id"], exc, answer.text,
                          answer.receipt_url)
                continue
            answered += 1
            log.info("answered %s room=%s hotel=%s tools=%d timings=%s receipt=%s", mention["id"], answer.room_id,
                     answer.hotel, answer.tool_calls, answer.timings, answer.receipt_url)
        return answered

    def run(self, interval: float) -> None:
        while True:
            try:
                self.poll_once()
            except XError as exc:
                log.error("%s", exc)
            time.sleep(interval)


def make_cathedral() -> Cathedral:
    api_key = env("CATHEDRAL_API_KEY")
    wallet, hotkey = env("CATHEDRAL_WALLET"), env("CATHEDRAL_HOTKEY")
    if api_key and (wallet or hotkey):
        sys.exit("set CATHEDRAL_API_KEY or CATHEDRAL_WALLET + CATHEDRAL_HOTKEY, not both")
    if api_key:
        return Cathedral(env("CATHEDRAL_API_BASE", "https://cathedral.computer"), api_key=api_key)
    if wallet and hotkey:
        signer = signer_from_wallet(wallet, hotkey, env("CATHEDRAL_WALLET_PATH"))
        # btauth/1 is verified at api.polaris.computer, not at the cathedral.computer edge.
        return Cathedral(env("CATHEDRAL_API_BASE", "https://api.polaris.computer"), signer=signer)
    sys.exit("set CATHEDRAL_API_KEY, or CATHEDRAL_WALLET and CATHEDRAL_HOTKEY to pay with Gamma")


def make_agent() -> Agent:
    chutes_key = env("CHUTES_API_KEY")
    if not chutes_key:
        sys.exit("set CHUTES_API_KEY (https://chutes.ai)")
    chutes = Chutes(chutes_key, env("CHUTES_MODEL", "deepseek-ai/DeepSeek-V3.2-TEE"))
    config = AgentConfig(template=env("CATHEDRAL_TEMPLATE", "sealed-1x2"),
                         lifetime_seconds=int(env("CATHEDRAL_ROOM_SECONDS", "600")),
                         max_spend_usd=env("CATHEDRAL_ROOM_MAX_USD", "0.002"))
    brave = env("BRAVE_API_KEY")
    return Agent(make_cathedral(), chutes, config, brave=BraveSearch(brave) if brave else None)


def make_x(mock: bool):
    if mock:
        return MockX(env("MOCK_MENTIONS", "examples/mentions.json"), env("MOCK_REPLIES", "mock_replies.jsonl"),
                     env("X_BOT_USERNAME", "yourbot"))
    keys = [env(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")]
    if not all(keys):
        sys.exit("set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, or run with --mock")
    return XClient(*keys)


def main(argv=None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="grokbot", description="X mention bot on Cathedral confidential sandboxes + Chutes")
    p.add_argument("--ask", help="answer one question and print it; no X needed")
    p.add_argument("--mock", action="store_true", help="read mentions from a file instead of X (no X keys)")
    p.add_argument("--once", action="store_true", help="poll once and exit")
    p.add_argument("--interval", type=float, default=float(env("POLL_SECONDS", "60")),
                   help="seconds between polls (X free/basic tiers rate limit mentions)")
    args = p.parse_args(argv)
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    agent = make_agent()
    if args.ask:
        answer = agent.answer([{"author": "you", "text": args.ask}], question_id="cli")
        for post in format_replies(answer):
            print(post + "\n---")
        print(json.dumps({"room": answer.room_id, "hotel": answer.hotel, "tool_calls": answer.tool_calls,
                          "timings": answer.timings, "exec_receipts": answer.exec_receipts}, indent=1))
        return 0
    mock = args.mock or env("X_MODE") == "mock"
    bot = Bot(make_x(mock), agent, env("STATE_FILE", "grokbot_state.json"),
              RateLimit(int(env("ANSWERS_PER_AUTHOR_PER_HOUR", "5")), int(env("ANSWERS_PER_HOUR", "200"))))
    if args.once:
        n = bot.poll_once()
        log.info("answered %d mention(s)", n)
        return 0
    bot.run(args.interval)
    return 0
