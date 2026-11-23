"""The program that runs INSIDE the Cathedral room for each tool call.

It is sent as `python3 -c <this file>` with a JSON request on stdin, so the room
needs nothing but the stock Python image. It prints one JSON object. Untrusted
input (the question, fetched pages, model-written code) only ever touches the
room, never the bot host. The Chutes, X and Cathedral keys never enter the room; an
optional BRAVE_API_KEY is passed to the search command only.
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) cathedral-grok-bot"
MAX_TEXT = 6000


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(2_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")


def _text(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def _ddg(query, limit):
    page = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
    results = []
    for m in re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                         r'(?:class="result__snippet"[^>]*>(.*?)</a>|</div>)', page):
        href = html.unescape(m.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        url = q.get("uddg", [href])[0]
        if url.startswith("//"):
            url = "https:" + url
        results.append({"title": _text(m.group(2)), "url": url, "snippet": _text(m.group(4) or "")})
        if len(results) >= limit:
            break
    return results


def _brave(query, limit, key):
    req = urllib.request.Request(
        "https://api.search.brave.com/res/v1/web/search?count=%d&q=%s" % (limit, urllib.parse.quote_plus(query)),
        headers={"Accept": "application/json", "X-Subscription-Token": key, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": _text(r.get("description", ""))}
            for r in (data.get("web") or {}).get("results", [])[:limit]]


def _wikipedia(query, limit):
    data = json.loads(_get("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=%d"
                           "&srsearch=%s" % (limit, urllib.parse.quote_plus(query))))
    return [{"title": r["title"], "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_")),
             "snippet": _text(r.get("snippet", ""))} for r in data.get("query", {}).get("search", [])]


def search(query, limit=5):
    """Brave Search when BRAVE_API_KEY is set for this one command, else DuckDuckGo, else Wikipedia.

    DuckDuckGo often refuses datacenter addresses; Wikipedia always answers but only for encyclopedic facts.
    """
    tried = []
    key = os.environ.get("BRAVE_API_KEY")
    engines = ([("brave", lambda: _brave(query, limit, key))] if key else []) + [
        ("duckduckgo", lambda: _ddg(query, limit)), ("wikipedia", lambda: _wikipedia(query, limit))]
    for name, run in engines:
        try:
            results = run()
        except Exception as exc:
            tried.append(name + ": " + type(exc).__name__)
            continue
        if results:
            return {"engine": name, "results": results}
        tried.append(name + ": no results")
    return {"results": [], "tried": tried}


def fetch(url):
    if not re.match(r"^https?://", url or ""):
        return {"error": "only http and https URLs"}
    return {"url": url, "text": _text(_get(url))[:MAX_TEXT]}


def run_python(code, timeout=25):
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout)
    return {"exit_code": proc.returncode, "stdout": proc.stdout[-MAX_TEXT:], "stderr": proc.stderr[-2000:]}


def main():
    req = json.loads(sys.stdin.read() or "{}")
    tool, args = req.get("tool"), req.get("args") or {}
    try:
        if tool == "search":
            out = search(str(args.get("query", "")))
        elif tool == "fetch":
            out = fetch(str(args.get("url", "")))
        elif tool == "run_python":
            out = run_python(str(args.get("code", "")))
        else:
            out = {"error": "unknown tool " + repr(tool)}
    except Exception as exc:
        out = {"error": type(exc).__name__ + ": " + str(exc)[:300]}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
