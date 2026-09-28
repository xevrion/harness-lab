#!/usr/bin/env python3
"""Lab 01's harness plus a verify step: "done" only counts if the work is really there.

You are still the model. Try to get away with a half-finished job.
"""

import json
import re
import shutil
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).parent / "workspace"
MAX_STEPS = 8
MAX_ATTEMPTS = 3
TASK_URL = "https://example.com"
TASK = f"Fetch {TASK_URL} and save the page's title into title.txt"

SYSTEM_PROMPT = (
    "You are a research agent. You can only reply with text. "
    "To use a tool, reply exactly: TOOL <name> <json args>. "
    'Tools: fetch {"url": ...}, write_file {"name": ..., "text": ...}, list_files {}. '
    "Reply with plain text (no TOOL) when you are finished."
)


def get_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "harness-lab"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(5000).decode("utf-8", "replace")


def fetch(url):
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", get_html(url), flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    return " ".join(text.split())[:400]


def write_file(name, text):
    path = WORKSPACE / Path(name).name
    path.write_text(text)
    return f"saved {path} ({len(text)} bytes)"


def list_files():
    return ", ".join(sorted(p.name for p in WORKSPACE.iterdir())) or "(empty)"


TOOLS = {"fetch": fetch, "write_file": write_file, "list_files": list_files}


def verify():
    """Return None if the task is really done, otherwise a reason it isn't."""
    path = WORKSPACE / "title.txt"
    if not path.exists():
        return "title.txt does not exist in the workspace"

    saved = path.read_text().strip()
    if not saved:
        return "title.txt is empty"

    # Fetch the page ourselves. The model's copy of it, and its claims about it, count for nothing here.
    match = re.search(r"<title>(.*?)</title>", get_html(TASK_URL), flags=re.S | re.I)
    real_title = match.group(1).strip()
    if saved != real_title:
        # Say what's wrong without handing over the answer, or the model just copies it.
        return f"title.txt contains {len(saved)} characters, and that is not the page's title"

    return None


def model(messages):
    print("\n" + "=" * 70)
    print("everything the model gets to see this turn:")
    for m in messages:
        print(f"  [{m['role']}] {m['content']}")
    print("=" * 70)
    return input("you are the model > ").strip()


def run():
    # Leftovers from an earlier run would let verify() pass on work this run never did.
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": TASK},
    ]
    attempts = 0

    for step in range(1, MAX_STEPS + 1):
        print(f"\n--- step {step}/{MAX_STEPS} ---")
        reply = model(messages)
        messages.append({"role": "model", "content": reply})

        if not reply.startswith("TOOL "):
            problem = verify()
            if problem is None:
                print(f"\nVERIFIED. the file is there and it's right.\nfinal answer: {reply}")
                return

            attempts += 1
            print(f"\nverify failed ({attempts}/{MAX_ATTEMPTS}): {problem}")
            if attempts == MAX_ATTEMPTS:
                print("out of attempts. marking the run FAILED, whatever the model says.")
                return
            messages.append({"role": "harness", "content": f"Not done: {problem}. Keep working."})
            continue

        parts = reply.split(" ", 2)
        name = parts[1] if len(parts) > 1 else ""
        try:
            args = json.loads(parts[2]) if len(parts) > 2 else {}
            if name not in TOOLS:
                result = f"error: no tool named '{name}'"
            else:
                print(f"running {name}({args})")
                result = TOOLS[name](**args)
        except Exception as e:
            result = f"error: {type(e).__name__}: {e}"

        print(f"tool result: {result}")
        messages.append({"role": "tool", "content": result})

    print(f"\nhit MAX_STEPS={MAX_STEPS}. marking the run FAILED.")


if __name__ == "__main__":
    run()
