import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from grokbot import btauth
from grokbot.agent import Agent, AgentConfig, Answer, ROOM_TOOLS_SRC
from grokbot.bot import Bot, RateLimit, format_replies, x_length
from grokbot.cathedral import Cathedral, CathedralError
from grokbot.chutes import Chutes
from grokbot.x_api import MockX, _normalize, build_thread, oauth1_header

ROOT = Path(__file__).resolve().parent.parent


class FakeKeypair:
    ss58_address = "5Sender"

    def __init__(self):
        self.signed = []

    def sign(self, data):
        self.signed.append(data)
        return hashlib.sha256(data).digest()


class FakeAPI:
    """A tiny Cathedral: records every request and answers from a script."""

    def __init__(self, create_answers=None, hotel="nc-sealed-2"):
        self.calls = []
        self.create_answers = list(create_answers or [(202, {"state": "creating"})])
        self.hotel = hotel
        self.deleted = []
        self.polls = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        path = url.split("://", 1)[1].split("/", 1)[1]
        if method == "POST" and path == "v1/sandboxes":
            status, data = self.create_answers.pop(0) if self.create_answers else (202, {"state": "creating"})
            if status == 202:
                data = {"id": "room-1", "hardware": "amd_sev_snp", "hotel": self.hotel,
                        "receipt_url": "/v1/receipts/r-create", **data}
            return status, json.dumps(data).encode()
        if method == "GET" and path == "v1/sandboxes/room-1":
            self.polls += 1
            return 200, json.dumps({"id": "room-1", "state": "running", "hardware": "amd_sev_snp",
                                    "hotel": self.hotel, "receipt_url": "/v1/receipts/r-create"}).encode()
        if method == "POST" and path == "v1/sandboxes/room-1/exec":
            req = json.loads(body)
            tool = json.loads(req["stdin"])
            out = {"search": {"results": [{"title": "t", "url": "https://x", "snippet": "128 subnets"}]},
                   "run_python": {"exit_code": 0, "stdout": "18446744073709551616\n", "stderr": ""}}[tool["tool"]]
            return 200, json.dumps({"exit_code": 0, "stdout": json.dumps(out), "stderr": "",
                                    "room_receipt_url": "/v1/receipts/r-exec"}).encode()
        if method == "DELETE" and path == "v1/sandboxes/room-1":
            self.deleted.append("room-1")
            return 202, b'{"state":"deleting"}'
        return 404, b"{}"


class FakeChutes:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        self.seen.append(json.loads(body))
        msg = self.script.pop(0)
        return 200, json.dumps({"choices": [{"message": msg}]}).encode()


def tool_call(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def cathedral(api, **kw):
    return Cathedral("https://api.example", api_key="test-key", transport=api, sleep=lambda s: None, **kw)


class BtauthTest(unittest.TestCase):
    def test_payload_is_the_documented_lines(self):
        body = b'{"a":1}'
        got = btauth.payload("post", "/v1/sandboxes?x=1", body, 123, "5S", "5R")
        self.assertEqual(got.decode().split("\n"), ["btauth/1", "sr25519", "POST", "/v1/sandboxes?x=1",
                                                     hashlib.sha256(body).hexdigest(), "123", "5S", "5R"])

    def test_headers_and_nonce_never_repeat(self):
        kp = FakeKeypair()
        s = btauth.Signer(kp, clock=lambda: 1000)
        h1, h2 = s.headers("GET", "/v1/x"), s.headers("GET", "/v1/x")
        self.assertEqual(h1["X-Bittensor-Hotkey"], "5Sender")
        self.assertEqual(h1["X-Bittensor-Receiver"], btauth.SERVICE_HOTKEY)
        self.assertEqual(h1["X-Bittensor-Version"], "1")
        self.assertTrue(h1["X-Bittensor-Signature"].startswith("0x"))
        self.assertLess(int(h1["X-Bittensor-Nonce"]), int(h2["X-Bittensor-Nonce"]))

    def test_cathedral_signs_exact_body_and_path_without_bearer(self):
        kp = FakeKeypair()
        api = FakeAPI()
        c = Cathedral("https://api.example", signer=btauth.Signer(kp), transport=api, sleep=lambda s: None)
        c.create_room()
        method, url, headers, body = api.calls[0]
        self.assertNotIn("Authorization", headers)
        lines = kp.signed[0].decode().split("\n")
        self.assertEqual(lines[2:5], ["POST", "/v1/sandboxes", hashlib.sha256(body).hexdigest()])

    def test_exactly_one_credential(self):
        with self.assertRaises(ValueError):
            Cathedral("https://a", api_key="k", signer=btauth.Signer(FakeKeypair()))
        with self.assertRaises(ValueError):
            Cathedral("https://a")


class CathedralTest(unittest.TestCase):
    def test_create_sends_sealed_body_and_returns_public_receipt(self):
        api = FakeAPI()
        room = cathedral(api).create_room()
        body = json.loads(api.calls[0][3])
        self.assertEqual(body["required_hardware"], "amd_sev_snp")
        self.assertEqual(body["template"], "sealed-1x2")
        self.assertIn("Idempotency-Key", api.calls[0][2])
        self.assertEqual(api.calls[0][2]["Authorization"], "Bearer test-key")
        self.assertEqual(room.receipt_url, "https://cathedral.computer/v1/receipts/r-create")
        self.assertEqual(room.hotel, "nc-sealed-2")

    def test_sold_out_retries_with_new_key_and_5xx_with_same_key(self):
        api = FakeAPI(create_answers=[(503, {"detail": {"error": "sealed_capacity_unavailable"}}),
                                      (500, {}), (202, {"state": "running"})])
        cathedral(api).create_room()
        keys = [c[2]["Idempotency-Key"] for c in api.calls if c[0] == "POST"]
        self.assertEqual(len(keys), 3)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(keys[1], keys[2])

    def test_402_is_not_retried(self):
        api = FakeAPI(create_answers=[(402, {"error": "insufficient_balance"})])
        with self.assertRaises(CathedralError) as ctx:
            cathedral(api).create_room()
        self.assertEqual(ctx.exception.code, "insufficient_balance")
        self.assertEqual(len(api.calls), 1)


class AgentTest(unittest.TestCase):
    def test_tools_run_in_the_room_and_room_is_deleted(self):
        api = FakeAPI()
        chutes = FakeChutes([tool_call("run_python", {"code": "print(2**64)"}),
                             {"role": "assistant", "content": "2^64 = 18446744073709551616."}])
        agent = Agent(cathedral(api), Chutes("chutes-secret-sentinel", "m", transport=chutes))
        ans = agent.answer([{"author": "bob", "text": "@bot 2^64?"}], "1002")
        self.assertEqual(ans.text, "2^64 = 18446744073709551616.")
        self.assertEqual(ans.tool_calls, 1)
        self.assertEqual(api.deleted, ["room-1"])
        self.assertEqual(ans.exec_receipts, ["https://cathedral.computer/v1/receipts/r-exec"])
        exec_body = json.loads([c for c in api.calls if c[1].endswith("/exec")][0][3])
        self.assertEqual(exec_body["command"][:2], ["python3", "-c"])
        # The tool result went back to the model, and no key went into the room.
        self.assertIn("18446744073709551616", chutes.seen[1]["messages"][-1]["content"])
        for c in api.calls:
            if c[3]:
                self.assertNotIn(b"chutes-secret-sentinel", c[3])

    def test_room_is_deleted_when_the_model_fails(self):
        api = FakeAPI()

        def broken(*a):
            return 500, b"{}"
        agent = Agent(cathedral(api), Chutes("ck", "m", transport=broken))
        with self.assertRaises(Exception):
            agent.answer([{"author": "a", "text": "q"}])
        self.assertEqual(api.deleted, ["room-1"])

    def test_search_key_goes_to_search_only(self):
        api = FakeAPI()
        chutes = FakeChutes([tool_call("search", {"query": "q"}), tool_call("run_python", {"code": "1"}),
                             {"content": "ok"}])
        agent = Agent(cathedral(api), Chutes("ck", "m", transport=chutes), search_env={"BRAVE_API_KEY": "bk"})
        agent.answer([{"author": "a", "text": "q"}])
        execs = [json.loads(c[3]) for c in api.calls if c[1].endswith("/exec")]
        self.assertEqual(execs[0]["env"], {"BRAVE_API_KEY": "bk"})
        self.assertNotIn("env", execs[1])

    def test_last_step_has_no_tools(self):
        api = FakeAPI()
        chutes = FakeChutes([tool_call("search", {"query": "q"})] * 2 + [{"content": "done"}])
        agent = Agent(cathedral(api), Chutes("ck", "m", transport=chutes), AgentConfig(max_steps=3))
        self.assertEqual(agent.answer([{"author": "a", "text": "q"}]).text, "done")
        self.assertNotIn("tools", chutes.seen[-1])


class RoomToolsTest(unittest.TestCase):
    def run_tool(self, req):
        out = subprocess.run([sys.executable, "-c", ROOM_TOOLS_SRC], input=json.dumps(req),
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)

    def test_run_python(self):
        self.assertEqual(self.run_tool({"tool": "run_python", "args": {"code": "print(6*7)"}})["stdout"], "42\n")

    def test_unknown_and_bad_url(self):
        self.assertIn("error", self.run_tool({"tool": "rm", "args": {}}))
        self.assertIn("error", self.run_tool({"tool": "fetch", "args": {"url": "file:///etc/passwd"}}))


class XTest(unittest.TestCase):
    def test_oauth1_matches_the_published_example(self):
        # The worked example from X's "Creating a signature" documentation.
        header = oauth1_header(
            "POST", "https://api.twitter.com/1.1/statuses/update.json",
            {"status": "Hello Ladies + Gentlemen, a signed OAuth request!", "include_entities": "true"},
            "xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
            "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb", "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
            nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958")
        self.assertIn('oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"', header)

    def test_normalize_maps_authors_and_parent(self):
        posts = _normalize({"data": [{"id": "2", "author_id": "u1", "text": "hi", "conversation_id": "1",
                                      "referenced_tweets": [{"type": "replied_to", "id": "1"}]}],
                            "includes": {"users": [{"id": "u1", "username": "alice"}]}})
        self.assertEqual(posts[0]["author"], "alice")
        self.assertEqual(posts[0]["replied_to"], "1")

    def test_mock_mentions_thread_and_replies(self):
        with tempfile.TemporaryDirectory() as d:
            x = MockX(str(ROOT / "examples" / "mentions.json"), os.path.join(d, "r.jsonl"))
            ms = x.mentions(None)
            self.assertEqual([m["id"] for m in ms], ["1002", "1003"])
            self.assertEqual([p["id"] for p in build_thread(x, ms[1])], ["1001", "1003"])
            self.assertEqual(x.mentions("1002")[0]["id"], "1003")
            x.reply("1002", "hello")
            self.assertEqual(json.loads(Path(d, "r.jsonl").read_text())["in_reply_to"], "1002")


class ReplyTest(unittest.TestCase):
    def test_short_answer_one_post_with_receipt(self):
        posts = format_replies(Answer("42.", "https://cathedral.computer/v1/receipts/abc"))
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].endswith("Receipt: https://cathedral.computer/v1/receipts/abc"))

    def test_long_answer_splits_and_fits(self):
        posts = format_replies(Answer("word " * 200, "https://cathedral.computer/v1/receipts/abc"))
        self.assertLessEqual(len(posts), 5)
        self.assertTrue(all(x_length(p) <= 280 for p in posts))
        self.assertIn("Receipt:", posts[-1])


class BotTest(unittest.TestCase):
    def test_mock_run_answers_each_mention_once(self):
        with tempfile.TemporaryDirectory() as d:
            x = MockX(str(ROOT / "examples" / "mentions.json"), os.path.join(d, "r.jsonl"))
            api = FakeAPI()
            chutes = FakeChutes([{"content": "a1"}, {"content": "a2"}])
            bot = Bot(x, Agent(cathedral(api), Chutes("ck", "m", transport=chutes)),
                      os.path.join(d, "state.json"), RateLimit(5, 100))
            self.assertEqual(bot.poll_once(), 2)
            self.assertEqual(bot.poll_once(), 0)
            replies = [json.loads(l) for l in Path(d, "r.jsonl").read_text().splitlines()]
            self.assertEqual([r["in_reply_to"] for r in replies], ["1002", "1003"])
            self.assertTrue(all("cathedral.computer/v1/receipts/" in r["text"] for r in replies))
            # The thread reached the model: carol's question carries alice's post above it.
            self.assertIn("@alice", chutes.seen[1]["messages"][1]["content"])

    def test_rate_limit(self):
        t = [0.0]
        rl = RateLimit(2, 3, clock=lambda: t[0])
        self.assertEqual([rl.allow("a"), rl.allow("a"), rl.allow("a"), rl.allow("b"), rl.allow("c")],
                         [True, True, False, True, False])
        t[0] = 3601
        self.assertTrue(rl.allow("a"))


if __name__ == "__main__":
    unittest.main()
