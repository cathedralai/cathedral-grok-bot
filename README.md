# cathedral-grok-bot

An X mention bot you can fork. Someone tags your bot with a question; the bot reads the thread, thinks with a model on [Chutes](https://chutes.ai), runs its web search, page fetches and Python inside a fresh [Cathedral](https://cathedral.computer) confidential sandbox (AMD SEV-SNP), deletes the sandbox, and replies with the answer and a link to the sandbox's signed receipt.

```
@yourbot what is 2 to the power 64, exactly?

  2 to the power of 64 is 18,446,744,073,709,551,616.

  Ran in a confidential Cathedral sandbox. Receipt: https://cathedral.computer/v1/receipts/...
```

- Python 3.10+, standard library only. Paying with Gamma adds one package (`bittensor-wallet`).
- One sandbox per answer: `POST /v1/sandboxes`, `POST /v1/sandboxes/{id}/exec` per tool call, `DELETE /v1/sandboxes/{id}`.
- Pay with a Cathedral API key, or with Gamma from your own Bittensor hotkey (btauth/1, no account).
- Mock X mode: run the whole bot from a JSON file, no X keys.
- MIT licensed. Guide, prices and measured numbers: https://cathedral.computer/grok-bot

## 10-minute setup

### 1. Get the code (1 min)

```bash
git clone https://github.com/cathedralai/cathedral-grok-bot
cd cathedral-grok-bot
cp .env.example .env
```

### 2. Pay for compute (3 min): pick one

**A. API key.** Sign in at https://cathedral.computer/account/, add credit, create a key with the `sandboxes:control` scope, and put it in `.env`:

```
CATHEDRAL_API_KEY=...
```

**B. Gamma, no account.** Your hotkey pays and signs every request. On netuid 94:

```bash
pip install bittensor-wallet
btcli gamma mint --netuid 94 --hotkey <YOUR_HOTKEY_SS58> --amount-tao <TAO>
btcli gamma spend --netuid 94 --service-hotkey 5FQsFkui1UvS7S7Sf3JAqN9FPCR7RmufSEZTGuVPPN5EDmsN \
  --amount <GAMMA> --wallet <WALLET> --wallet-hotkey <HOTKEY_NAME>
curl "https://cathedral.computer/v1/gamma/balance?hotkey=<YOUR_HOTKEY_SS58>"
```

1 Gamma is 1 US dollar of credit. Then in `.env` (leave `CATHEDRAL_API_KEY` empty):

```
CATHEDRAL_WALLET=<WALLET>
CATHEDRAL_HOTKEY=<HOTKEY_NAME>
```

The service hotkey and API base are published at https://cathedral.computer/.well-known/cathedral-services.json; if they differ from this page, that file wins.

### 3. Model (1 min)

Create a key at https://chutes.ai and set `CHUTES_API_KEY`. The default model is `deepseek-ai/DeepSeek-V3.2-TEE`; any Chutes model with tool calling works (`CHUTES_MODEL`).

### 4. Try it with no X at all (2 min)

```bash
python3 -m grokbot --ask "What is 2 to the power 64, exactly?"
python3 -m grokbot --mock --once
cat mock_replies.jsonl
```

`--mock` reads mentions from `examples/mentions.json` (edit it; a mention is any post containing `@yourbot`, and `replied_to` builds a thread) and writes the replies to `mock_replies.jsonl`. Run it again and it answers only new mentions.

### 5. Go live on X (3 min)

1. In the X developer portal, create an app with **Read and Write** permission. Reading mentions needs an X API plan that includes the mentions endpoint; check the current plans there.
2. Generate the consumer key and secret, and the access token and secret **for the bot account**.
3. Put them in `.env`:

```
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
```

4. Run it:

```bash
python3 -m grokbot              # polls mentions every 60 s (POLL_SECONDS)
```

Run it under `systemd`, `tmux` or any process manager. State (the last mention answered) is in `grokbot_state.json`.

## How it works

```
X mention -> thread (up to 8 parents) -> Chutes model
                                          | tool call: search / fetch / run_python
                                          v
                      Cathedral confidential sandbox (one per answer)
                      exec: python3 -c room_tools.py  <- JSON on stdin
                                          |
                      answer + receipt link -> reply on X -> DELETE sandbox
```

| File | What it does |
| --- | --- |
| `grokbot/bot.py` | Poll loop, state, rate limits, reply splitting, CLI |
| `grokbot/agent.py` | The agent step: sandbox create, model loop, tools in the sandbox, delete |
| `grokbot/room_tools.py` | The program that runs inside the sandbox for each tool call |
| `grokbot/cathedral.py` | Cathedral HTTPS client (API key or btauth/1), retry rules from the agent contract |
| `grokbot/btauth.py` | btauth/1 request signing with a Bittensor hotkey |
| `grokbot/chutes.py` | Chutes chat completions |
| `grokbot/x_api.py` | X API v2 with OAuth 1.0a, thread building, mock X |

## Why a sandbox per answer

- The question, fetched pages and model-written code are untrusted. They run in a confidential sandbox, not on your bot host.
- Your Chutes, X and Cathedral keys never enter the sandbox. The optional `BRAVE_API_KEY` goes to the search command only.
- Each create and each command leaves a receipt signed by Cathedral; the create receipt carries the confidential host's own signed room receipt. The reply links the create receipt (public, no key). What a receipt proves and what it does not: https://cathedral.computer/docs/receipts/
- A receipt proves Cathedral signed a statement about the create or the command. It does not prove the answer is right.

## Cost

A `sealed-1x2` sandbox (1 vCPU, 2 GiB) is $0.005 per hour, billed by the second. Each create holds `rate x lifetime` (600 s by default, so about $0.00083) and the delete returns the unused part. Measured cost per 1,000 answers, with method and sample size: https://cathedral.computer/grok-bot

Defaults you can change in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CATHEDRAL_TEMPLATE` | `sealed-1x2` | Sandbox size (`sealed-2x4`, `sealed-4x8`, `sealed-8x16`) |
| `CATHEDRAL_ROOM_SECONDS` | `600` | Sandbox lifetime; the sandbox stops by itself after this even if the bot dies |
| `CATHEDRAL_ROOM_MAX_USD` | `0.002` | Most one sandbox may cost |
| `ANSWERS_PER_AUTHOR_PER_HOUR` | `5` | Per-author limit |
| `ANSWERS_PER_HOUR` | `200` | Total limit |
| `BRAVE_API_KEY` | empty | Better web search. Without it: DuckDuckGo, then Wikipedia (DuckDuckGo often refuses datacenter addresses) |

## Limits to know

- An account runs 2 live confidential sandboxes at once by default; a 429 `sealed_room_limit_reached` means that. The bot answers one mention at a time, so the default is enough; ask Cathedral to raise it if you answer in parallel.
- `exec` waits up to 45 seconds; tool commands here time out at 40.
- Sandboxes live at most 24 hours. Full list: https://cathedral.computer/docs/sandboxes/limits/
- 402 `insufficient_credits` (API key) or `insufficient_balance` (Gamma): add credit or spend more Gamma. Nothing ran and nothing was charged.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests need no network and no keys: Cathedral, Chutes and X are fakes. They cover btauth/1 signing, the create retry rules, the sandbox always being deleted, keys never reaching the sandbox, OAuth 1.0a against X's published example, thread building, reply splitting and the mock bot end to end.

## Security

- Keep `.env` out of git (it is in `.gitignore`). Never put a key in a label, a command or a log.
- Mentions are untrusted input: the system prompt tells the model that thread text and tool output are data, and everything they can trigger runs in the sandbox.
- Report a problem in this repo's issues, or to Cathedral at https://cathedral.computer.

## License

MIT. See `LICENSE`.
