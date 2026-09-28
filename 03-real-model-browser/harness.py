#!/usr/bin/env python3
"""A real model drives a real browser. Same shape as lab 02, minus you.

    python3 harness.py                      # default task, with a verifier
    python3 harness.py "your own task"      # anything you like, no verifier
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

MAX_STEPS = 12
MAX_ATTEMPTS = 3
DEFAULT_TASK = "Go to news.ycombinator.com and tell me the exact title of the #1 story on the front page."

SYSTEM_PROMPT = (
    "You control a real web browser through tools. "
    "Call read_page to see the page and get numbered elements before you click or type, "
    "and call it again after anything that changes the page, because old numbers go stale. "
    "When the task is done, reply with a plain text answer and no tool call."
)

page = None


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
        print("copy .env.example to .env in the repo root and fill it in, e.g. for Gemini's free tier:")
        print("  LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai")
        print("  LLM_API_KEY=<key from https://aistudio.google.com/apikey>")
        print("  LLM_MODEL=gemini-2.5-flash")
        print("\nGroq and DeepSeek presets are in 03-real-model-browser/README.md.")
        sys.exit(1)


def goto(url):
    page.goto(url, wait_until="domcontentloaded")
    return f"now at {page.url} ({page.title()})"


READ_PAGE_JS = """
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
  return { text: document.body.innerText, items };
}
"""


def read_page():
    # The model can't see pixels, so the page becomes text plus a numbered list of things it can act on.
    data = page.evaluate(READ_PAGE_JS)
    text = " ".join(data["text"].split())[:1500]
    return f"url: {page.url}\ntitle: {page.title()}\n\ntext:\n{text}\n\nelements:\n" + "\n".join(data["items"])


def click(n):
    page.click(f'[data-harness-id="{n}"]', timeout=5000)
    page.wait_for_load_state("domcontentloaded")
    return f"clicked [{n}], now at {page.url}"


def type_text(n, text, submit=False):
    target = f'[data-harness-id="{n}"]'
    page.fill(target, text, timeout=5000)
    if submit:
        page.press(target, "Enter")
        page.wait_for_load_state("domcontentloaded")
    return f"typed into [{n}]" + (f", submitted, now at {page.url}" if submit else "")


TOOLS = {
    "goto": {
        "fn": goto,
        "description": "Open a URL in the browser.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    "read_page": {
        "fn": read_page,
        "description": "Read the current page: its text and a numbered list of links, buttons and inputs.",
        "parameters": {"type": "object", "properties": {}},
    },
    "click": {
        "fn": click,
        "description": "Click element number n from the latest read_page.",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    },
    "type_text": {
        "fn": type_text,
        "description": "Type text into input number n from the latest read_page, optionally pressing Enter.",
        "parameters": {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
            "required": ["n", "text"],
        },
    },
}


def call_model(messages):
    # In lab 01 the harness had to fish "TOOL name {...}" out of free text. Here the tools go
    # up as JSON schemas and the tool calls come back in their own field, so nothing is guessed.
    body = {
        "model": os.environ["LLM_MODEL"],
        "messages": messages,
        "tools": [
            {"type": "function", "function": {"name": name, "description": t["description"], "parameters": t["parameters"]}}
            for name, t in TOOLS.items()
        ],
    }
    req = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"\nthe provider said {e.code}: {e.read().decode(errors='replace')[:500]}")
        if e.code in (401, 403):
            print("the key was rejected. check LLM_API_KEY in .env.")
        elif e.code in (400, 404):
            print("if that's about the model, the name may have changed. list what's available with:")
            print(f'  curl -H "Authorization: Bearer <your key>" {os.environ["LLM_BASE_URL"].rstrip("/")}/models')
        elif e.code == 429:
            print("rate limited. free tiers allow only a few requests a minute, wait a bit and rerun.")
        sys.exit(1)

    if usage := data.get("usage"):
        print(f"  tokens: {usage.get('prompt_tokens')} in, {usage.get('completion_tokens')} out")
    return data["choices"][0]["message"]


def hn_top_title():
    req = urllib.request.Request("https://news.ycombinator.com/", headers={"User-Agent": "harness-lab"})
    with urllib.request.urlopen(req, timeout=10) as r:
        page_html = r.read().decode("utf-8", "replace")
    match = re.search(r'<span class="titleline"><a [^>]*>(.*?)</a>', page_html)
    return html.unescape(match.group(1)).strip()


def verify(answer):
    squash = lambda s: " ".join(s.split()).lower()
    # HN can reshuffle between the model reading it and this check, so a correct answer
    # can fail here. A verifier is code too, and it can be wrong.
    if squash(hn_top_title()) not in squash(answer or ""):
        return "your answer doesn't contain the exact title of the current #1 story"
    return None


def run(task, verifier):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    attempts = 0

    for step in range(1, MAX_STEPS + 1):
        print(f"\n--- step {step}/{MAX_STEPS} ---")
        msg = call_model(messages)
        tool_calls = msg.get("tool_calls") or []
        # Keep tool_calls exactly as they came back. Some providers tuck extra fields in there
        # that they expect to see again on the next request.
        messages.append({"role": "assistant", "content": msg.get("content"), **({"tool_calls": tool_calls} if tool_calls else {})})

        if not tool_calls:
            answer = msg.get("content") or ""
            print(f"model says it's done: {answer}")
            if verifier is None:
                return
            problem = verifier(answer)
            if problem is None:
                print("\nVERIFIED against the live page.")
                return
            attempts += 1
            print(f"verify failed ({attempts}/{MAX_ATTEMPTS}): {problem}")
            if attempts == MAX_ATTEMPTS:
                print("out of attempts, marking the run FAILED.")
                return
            messages.append({"role": "user", "content": f"Not done: {problem}. Check the page again."})
            continue

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                print(f"model calls {name}({args})")
                result = TOOLS[name]["fn"](**args) if name in TOOLS else f"error: no tool named '{name}'"
            except Exception as e:
                result = f"error: {type(e).__name__}: {e}"
            print("  " + result.replace("\n", " ")[:150])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    print(f"\nhit MAX_STEPS={MAX_STEPS}, stopping. marking the run FAILED.")


def main():
    global page
    load_env()
    task = " ".join(sys.argv[1:]) or DEFAULT_TASK
    verifier = verify if task == DEFAULT_TASK else None
    if verifier is None:
        print("custom task: there's no verifier for it, so whatever the model says at the end is taken on trust.")
    print(f"task: {task}\nmodel: {os.environ['LLM_MODEL']}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("HARNESS_HEADLESS") == "1", slow_mo=300)
        page = browser.new_page()
        try:
            run(task, verifier)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
