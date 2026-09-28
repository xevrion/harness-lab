#!/usr/bin/env python3
"""Lab 04's paper downloader, with a choice of what the model gets to see each step.

    python3 harness.py --context full       # send the whole conversation every time (lab 04)
    python3 harness.py --context window     # only the task and the last few messages
    python3 harness.py --context stubs      # everything, but old pages shrunk to one line
    python3 harness.py --selftest           # check the policies offline, no key needed
"""

import argparse
import csv
import inspect
import json
import os
import re
import shutil
import sys
import time
import urllib.error
from datetime import datetime
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from ddgs import DDGS
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

WORKSPACE = Path(__file__).parent / "workspace"
RESULTS = Path(__file__).parent / "results.csv"
MAX_ATTEMPTS = 3
WINDOW_SIZE = 8
KEEP_READS = 2
BULKY_TOOLS = ("read_page", "web_search")
MAX_RETRIES = 4
MAX_WAIT = 60
MAX_PDF_BYTES = 25 * 1024 * 1024
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
DEFAULT_TASK = (
    "Find Telangana (TS) Intermediate 1st year Maths 1A previous year question papers "
    "and download the PDFs for the most recent years you can find, one folder per year."
)

SYSTEM_PROMPT = (
    "You are a research agent with a real web browser and a downloader. "
    "Search with web_search, since search engines block the browser. Open promising pages with goto, "
    "then read_page, which lists the PDF links on the page along with the text around each one. "
    "Save papers with download_pdf, using the year as the folder. "
    "A download only happened if download_pdf says it saved the file, so never claim one it didn't confirm. "
    "When you're finished, reply with a plain text summary and no tool call."
)

page = None
TOOLS = {}
usage_total = {"in": 0, "out": 0, "calls": 0}
last_call = {}
last_read = {}


def tool(description, **params):
    """Register a function as a tool. params maps each argument to (json type, what it means)."""
    def register(fn):
        signature = inspect.signature(fn)
        TOOLS[fn.__name__] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    # The description is prompt text. It's the only thing the model knows about
                    # the tool, so a vague one gets the tool misused or ignored.
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": {name: {"type": t, "description": d} for name, (t, d) in params.items()},
                        "required": [n for n, p in signature.parameters.items() if p.default is p.empty],
                    },
                },
            },
        }
        return fn
    return register


@tool(
    "Search the web and get back the top results as title, URL and a short snippet. "
    "Use this to find pages. It does not open them.",
    query=("string", "what to search for"),
)
def web_search(query):
    results = DDGS().text(query, max_results=8)
    if not results:
        return "no results"
    return "\n".join(f"- {r['title']}\n  {r['href']}\n  {r['body'][:150]}" for r in results)


@tool(
    "Open a web page in the browser. Don't use this for PDF files, use download_pdf for those.",
    url=("string", "full URL including https://"),
)
def goto(url):
    page.goto(url, wait_until="domcontentloaded")
    return f"now at {page.url} ({page.title()})"


READ_PAGE_JS = r"""
() => {
  document.querySelectorAll('[data-harness-id]').forEach(el => el.removeAttribute('data-harness-id'));
  const els = [...document.querySelectorAll('a, button, input, textarea, select, [role=button]')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .slice(0, 30);
  const items = els.map((el, i) => {
    el.setAttribute('data-harness-id', i);
    const label = (el.innerText || el.getAttribute('aria-label') || el.placeholder || el.value || el.name || '').trim();
    return `[${i}] <${el.tagName.toLowerCase()}> ${label.slice(0, 80)}`;
  });
  const seen = new Set();
  const pdfs = [...document.querySelectorAll('a[href]')]
    .filter(a => (/\.pdf(\?|#|$)/i.test(a.href) || /drive\.google\.com\/file\/d\//.test(a.href)) && !seen.has(a.href) && seen.add(a.href))
    .slice(0, 40)
    .map(a => {
      // Paper sites label every link "Click Here", so the row around it is the real name.
      const row = a.closest('tr, li, p');
      const label = (row ? row.innerText : a.innerText).replace(/\s+/g, ' ').trim();
      return `- ${label.slice(0, 90)}\n  ${a.href}`;
    });
  return { text: document.body.innerText, items, pdfs };
}
"""


@tool("Read the current page: its text, a numbered list of links, buttons and inputs, and every PDF link on it.")
def read_page():
    data = page.evaluate(READ_PAGE_JS)
    text = " ".join(data["text"].split())[:1500]
    out = f"url: {page.url}\ntitle: {page.title()}\n\ntext:\n{text}\n\nelements:\n" + "\n".join(data["items"])
    if data["pdfs"]:
        out += "\n\npdf links:\n" + "\n".join(data["pdfs"])

    # In lab 04 the model read the same page twice in a row, which put a second full copy of it
    # in the conversation to be resent on every step after. Only when the previous call was this
    # same read, so the first copy is still the newest thing in context under every policy.
    if last_call.get("name") == "read_page" and last_read.get("url") == page.url and last_read.get("out") == out:
        return f"no change since step {last_read['step']}: same page, same text. Use what you read then, or go somewhere else."
    last_read.update(url=page.url, out=out, step=last_call.get("current_step"))
    return out


@tool("Click element number n from the latest read_page.", n=("integer", "the element's number"))
def click(n):
    target = f'[data-harness-id="{n}"]'
    try:
        page.click(target, timeout=5000)
    except PlaywrightTimeout:
        # Twice in lab 04 an ad was sitting on top of the link, so Playwright waited for it to
        # become clickable and gave up. A link's href doesn't care what's covering it.
        href = page.get_attribute(target, "href", timeout=1000)
        if not href:
            raise
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
        return f"click on [{n}] timed out (something was covering it), so I opened its link directly, now at {page.url}"
    page.wait_for_load_state("domcontentloaded")
    return f"clicked [{n}], now at {page.url}"


@tool(
    "Type text into input number n from the latest read_page, optionally pressing Enter.",
    n=("integer", "the input's number"),
    text=("string", "what to type"),
    submit=("boolean", "press Enter afterwards"),
)
def type_text(n, text, submit=False):
    target = f'[data-harness-id="{n}"]'
    page.fill(target, text, timeout=5000)
    if submit:
        page.press(target, "Enter")
        page.wait_for_load_state("domcontentloaded")
    return f"typed into [{n}]" + (f", submitted, now at {page.url}" if submit else "")


def clean(part):
    part = re.sub(r"\.{2,}", "", part)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip(".-")[:80]


def size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n // 1000} KB"


class TooBig(Exception):
    pass


def fetch_capped(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        length = r.headers.get("Content-Length")
        if length and int(length) > MAX_PDF_BYTES:
            raise TooBig(int(length))
        chunks, total = [], 0
        # Content-Length can be missing or wrong, so count the bytes as they arrive as well.
        while chunk := r.read(64 * 1024):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise TooBig(total)
            chunks.append(chunk)
        return b"".join(chunks), r.headers.get("Content-Type", "?")


@tool(
    "Download a PDF from a URL into the workspace. Checks it's really a PDF before saving. "
    "Put papers from the same year in the same folder.",
    url=("string", "the PDF's URL, usually from the pdf links in read_page"),
    filename=("string", "a short descriptive name, e.g. maths-1a-march-2024.pdf"),
    folder=("string", "optional subfolder, e.g. the year"),
)
def download_pdf(url, filename, folder=None):
    url = urljoin(page.url, url) if page else url
    # Paper sites often host on Google Drive, and a /view link is a viewer page, not the file.
    # The first real run burned its last steps on exactly this, so the harness handles it now.
    if match := re.match(r"https://drive\.google\.com/file/d/([\w-]+)", url):
        url = f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    if urlparse(url).scheme not in ("http", "https"):
        return f"refused: only http(s) URLs can be downloaded, got {url[:60]}"

    name = clean(Path(filename).name) or "download"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    target_dir = WORKSPACE / clean(folder) if folder and clean(folder) else WORKSPACE
    dest = target_dir / name
    # clean() already strips slashes, this just makes sure nothing can land outside the workspace.
    if not dest.resolve().is_relative_to(WORKSPACE.resolve()):
        return "refused: that path is outside the workspace"
    if dest.exists():
        return f"already have it: {dest.relative_to(WORKSPACE.parent)} ({size(dest.stat().st_size)})"

    try:
        data, content_type = fetch_capped(url)
    except TooBig as e:
        return f"refused: file is at least {size(e.args[0])}, the limit is {size(MAX_PDF_BYTES)}"
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403) or page is None:
            raise
        # Some sites only serve files to a browser that has visited them, so try again with its cookies.
        response = page.request.get(url, timeout=30000)
        data, content_type = response.body(), response.headers.get("content-type", "?")
        if len(data) > MAX_PDF_BYTES:
            return f"refused: file is {size(len(data))}, the limit is {size(MAX_PDF_BYTES)}"

    # Content-Type lies both ways, and paper sites love serving an HTML page at a .pdf URL.
    # The first bytes of a real PDF are always "%PDF", so they decide.
    if not data.startswith(b"%PDF"):
        return f"refused: that isn't a PDF (server said {content_type}, file starts with {data[:20]!r}). Probably a viewer or web page, not the file itself, so look for a direct .pdf link or try another site."

    target_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"saved {dest.relative_to(WORKSPACE.parent)} ({size(len(data))})"


def is_real_pdf(path):
    with path.open("rb") as f:
        return f.read(4) == b"%PDF" and path.stat().st_size > 10_000


def verify_downloads(_answer):
    # This verifier doesn't know which papers exist. It only checks the downloads are real,
    # which is why it works for any download task and for nothing else.
    real = [p for p in WORKSPACE.rglob("*.pdf") if is_real_pdf(p)]
    if not real:
        return "there are no real PDFs in the workspace, nothing was actually downloaded"
    return None


def print_tree():
    files = sorted(p for p in WORKSPACE.rglob("*") if p.is_file())
    print(f"\n{WORKSPACE.name}/  ({len(files)} files)")
    last_dir = None
    for f in files:
        rel = f.relative_to(WORKSPACE)
        nested = rel.parent != Path(".")
        if nested and rel.parent != last_dir:
            print(f"  {rel.parent}/")
            last_dir = rel.parent
        status = "ok" if is_real_pdf(f) else "NOT a real pdf"
        print(f"{'    ' if nested else '  '}{f.name}  {size(f.stat().st_size)}  {status}")


def load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

    missing = [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
    if missing:
        print(f"missing {', '.join(missing)}.\n")
        print("copy .env.example to .env in the repo root and fill it in.")
        print("provider presets are in 03-real-model-browser/README.md.")
        sys.exit(1)


# The full history stays in memory. A policy only decides what gets sent on this one call.
# Bookkeeping lives in keys starting with "_" and is stripped before anything goes over the wire.

def wire(messages):
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def full(history):
    return wire(history)


def window(history):
    head, rest = history[:2], history[2:]
    start = max(0, len(rest) - WINDOW_SIZE)
    # A tool result without the assistant message that asked for it gets the whole request
    # rejected by OpenAI-compatible APIs, so never start the window in the middle of a batch.
    while start > 0 and rest[start]["role"] == "tool":
        start -= 1
    return wire(head + rest[start:])


def stubs(history):
    bulky = [i for i, m in enumerate(history) if m.get("_bulky")]
    old = set(bulky[:-KEEP_READS])
    out = []
    for i, m in enumerate(history):
        if i in old:
            # Same message, same tool_call_id, just a one-line receipt instead of the page.
            if m["_tool"] == "web_search":
                what, again = f"searched for {m['_query']!r}", "search again"
            else:
                what, again = f"read {m['_url']} ({m['_title']})", "go back and call read_page"
            m = {**m, "content": f"[{what} at step {m['_step']}, text removed to save context. {again} if you need it.]"}
        out.append(m)
    return wire(out)


POLICIES = {"full": full, "window": window, "stubs": stubs}


def call_model(messages, total):
    body = {
        "model": os.environ["LLM_MODEL"],
        "messages": messages,
        "tools": [t["schema"] for t in TOOLS.values()],
    }
    req = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
    )
    for retry in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            if (e.code == 429 or e.code >= 500) and retry < MAX_RETRIES:
                wait = min(retry_wait(e, error_body, retry), MAX_WAIT)
                print(f"  {'rate limited' if e.code == 429 else f'provider error {e.code}'}, waiting {wait:.0f}s (retry {retry + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            report_http_error(e.code, error_body)

    if usage := data.get("usage"):
        usage_total["in"] += usage.get("prompt_tokens") or 0
        usage_total["out"] += usage.get("completion_tokens") or 0
        usage_total["calls"] += 1
        print(f"  tokens: {usage.get('prompt_tokens')} in, {usage.get('completion_tokens')} out  (sent {len(messages)} of {total} messages)")
    return data["choices"][0]["message"]


def retry_wait(e, error_body, retry):
    if e.headers and (header := e.headers.get("Retry-After")):
        try:
            return float(header)
        except ValueError:
            pass
    if match := re.search(r'retry in ([\d.]+)\s*s|"retryDelay":\s*"([\d.]+)s"', error_body, re.I):
        return float(match.group(1) or match.group(2)) + 1
    return 2 ** (retry + 1)


def report_http_error(code, error_body):
    print(f"\nthe provider said {code}: {error_body[:500]}")
    if code in (401, 403):
        print("the key was rejected. check LLM_API_KEY in .env.")
    elif code in (400, 404):
        print("if that's about the model, the name may have changed. list what's available with:")
        print(f'  curl -H "Authorization: Bearer <your key>" {os.environ["LLM_BASE_URL"].rstrip("/")}/models')
    elif code == 429:
        print("still rate limited after retrying. wait a bit and rerun.")
    sys.exit(1)


def run(task, policy, max_steps):
    history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    attempts = 0

    for step in range(1, max_steps + 1):
        print(f"\n--- step {step}/{max_steps} ---")
        last_call["current_step"] = step
        msg = call_model(POLICIES[policy](history), len(history))
        tool_calls = msg.get("tool_calls") or []
        history.append({"role": "assistant", "content": msg.get("content"), **({"tool_calls": tool_calls} if tool_calls else {})})

        if not tool_calls:
            print(f"model says it's done: {msg.get('content') or ''}")
            problem = verify_downloads(msg.get("content"))
            if problem is None:
                print("\nVERIFIED: the workspace has real PDFs in it.")
                return "verified", step
            attempts += 1
            print(f"verify failed ({attempts}/{MAX_ATTEMPTS}): {problem}")
            if attempts == MAX_ATTEMPTS:
                print("out of attempts, marking the run FAILED.")
                return "failed", step
            history.append({"role": "user", "content": f"Not done: {problem}. Keep going."})
            continue

        for call in tool_calls:
            name, args = call["function"]["name"], {}
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                print(f"model calls {name}({args})")
                result = TOOLS[name]["fn"](**args) if name in TOOLS else f"error: no tool named '{name}'"
            except Exception as e:
                result = f"error: {type(e).__name__}: {e}"
            print("  " + result.replace("\n", " ")[:150])

            entry = {"role": "tool", "tool_call_id": call["id"], "content": result, "_tool": name, "_step": step}
            if name in BULKY_TOOLS and not result.startswith(("error", "no change")):
                entry.update(_bulky=True, _url=page.url, _title=page.title(), _query=args.get("query"))
            history.append(entry)
            last_call["name"] = name

    # Running out of steps while chasing one more year shouldn't throw away the papers already saved.
    print(f"\nhit MAX_STEPS={max_steps}, stopping.")
    problem = verify_downloads(None)
    print("PARTIAL: out of steps, but the workspace has real PDFs." if problem is None else f"FAILED: {problem}")
    return ("partial" if problem is None else "failed"), max_steps


def save_result(policy, max_steps, outcome, steps):
    pdfs = sum(1 for p in WORKSPACE.rglob("*.pdf") if is_real_pdf(p))
    new = not RESULTS.exists()
    with RESULTS.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["when", "model", "context", "outcome", "steps", "max_steps", "tokens_in", "tokens_out", "pdfs"])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"), os.environ["LLM_MODEL"], policy, outcome,
            steps, max_steps, usage_total["in"], usage_total["out"], pdfs,
        ])
    print(f"added this run to {RESULTS.name}")


def selftest():
    """Run the policies over a made-up history and check they never send a broken conversation."""
    def pairs_ok(msgs):
        waiting = set()
        for m in msgs:
            if m["role"] == "tool":
                if m["tool_call_id"] not in waiting:
                    return False
                waiting.discard(m["tool_call_id"])
            elif waiting:
                return False
            else:
                waiting = {c["id"] for c in m.get("tool_calls") or []}
        return not waiting

    def ask(*ids):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": i, "type": "function", "function": {"name": "x", "arguments": "{}"}} for i in ids]}

    def page_read(i, step):
        return {"role": "tool", "tool_call_id": i, "content": "page text " * 200, "_tool": "read_page", "_step": step, "_bulky": True, "_url": f"https://site/{step}", "_title": f"page {step}", "_query": None}

    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    history += [ask("s1"), {"role": "tool", "tool_call_id": "s1", "content": "results " * 100, "_tool": "web_search", "_step": 1, "_bulky": True, "_url": "", "_title": "", "_query": "maths 1a"}]
    for step in range(2, 6):
        history += [ask(f"r{step}"), page_read(f"r{step}", step)]
    download = {"role": "tool", "tool_call_id": "d6", "content": "saved workspace/2024/a.pdf (1.1 MB)", "_tool": "download_pdf", "_step": 6}
    history += [ask("d6", "r6"), download, page_read("r6", 6)]
    history += [ask("e7"), {"role": "tool", "tool_call_id": "e7", "content": "error: TimeoutError", "_tool": "click", "_step": 7}]

    checks = {}
    for name, policy in POLICIES.items():
        sent = policy(history)
        checks[f"{name}: tool calls still paired"] = pairs_ok(sent)
        checks[f"{name}: no bookkeeping keys sent"] = not any(k.startswith("_") for m in sent for k in m)
        checks[f"{name}: system prompt and task kept"] = sent[0]["content"] == "sys" and sent[1]["content"] == "task"

    checks["full: nothing dropped"] = len(full(history)) == len(history)
    # Cutting exactly WINDOW_SIZE messages here would start on a tool result, so the window has to back up one.
    checks["window: backed up to include the assistant message"] = window(history)[2]["role"] == "assistant"
    sent = stubs(history)
    contents = [m["content"] for m in sent]
    checks["stubs: same number of messages"] = len(sent) == len(history)
    checks["stubs: old search stubbed"] = "[searched for 'maths 1a' at step 1" in contents[3]
    checks["stubs: old page stubbed"] = contents[5].startswith("[read https://site/2 (page 2) at step 2")
    checks["stubs: last two reads kept"] = contents[11] == history[11]["content"] and contents[14] == history[14]["content"]
    checks["stubs: download result kept"] = download["content"] in contents
    checks["stubs: error kept"] = "error: TimeoutError" in contents

    for check, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {check}")
    chars = lambda msgs: sum(len(m.get("content") or "") for m in msgs)
    print(f"\n{len(history)} messages. window sends {len(window(history))}. stubs sends {chars(stubs(history))} characters instead of {chars(full(history))}.")
    sys.exit(0 if all(checks.values()) else 1)


def main():
    global page
    parser = argparse.ArgumentParser(description="lab 05: the same agent with a choice of context policy")
    parser.add_argument("task", nargs="*", help="a download task (default: TS Inter Maths 1A papers)")
    parser.add_argument("--context", choices=POLICIES, default="full", help="what gets sent to the model each step")
    parser.add_argument("--steps", type=int, default=25, help="max agent steps (default 25)")
    parser.add_argument("--selftest", action="store_true", help="check the policies offline and exit")
    args = parser.parse_args()
    if args.selftest:
        selftest()

    load_env()
    task = " ".join(args.task) or DEFAULT_TASK
    if task != DEFAULT_TASK:
        print("custom task: the verifier here only knows how to check downloads, so give it a download task.")
    print(f"task: {task}\nmodel: {os.environ['LLM_MODEL']}\ncontext: {args.context}\ntools: {', '.join(TOOLS)}")

    # Same reason as lab 02: files from an earlier run would let the verifier pass on work this run never did.
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir()

    outcome, steps = "crashed", 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("HARNESS_HEADLESS") == "1", slow_mo=300)
        page = browser.new_page()
        try:
            outcome, steps = run(task, args.context, args.steps)
        finally:
            browser.close()
            print_tree()
            print(f"\ncontext: {args.context}. tokens: {usage_total['in']} in, {usage_total['out']} out over {usage_total['calls']} calls.")
            save_result(args.context, args.steps, outcome, steps)


if __name__ == "__main__":
    main()
