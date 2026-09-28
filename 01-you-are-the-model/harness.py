#!/usr/bin/env python3
"""A real agent harness where the model is you, typing replies by hand.

Run it, read the prompt it shows you, and answer the way a model would.
"""

import json
import re
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).parent / "workspace"
MAX_STEPS = 5

SYSTEM_PROMPT = (
    "You are a research agent. You can only reply with text. "
    "To use a tool, reply exactly: TOOL <name> <json args>. "
    'Tools: fetch {"url": ...}, write_file {"name": ..., "text": ...}, list_files {}. '
    "Reply with plain text (no TOOL) when you are finished."
)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "harness-lab"})
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read(5000).decode("utf-8", "replace")
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    # A raw page would flood the prompt, and you have to read every word of it.
    return " ".join(text.split())[:400]


def write_file(name, text):
    # Path(name).name throws away any "../" so the model can't write outside the workspace.
    path = WORKSPACE / Path(name).name
    path.write_text(text)
    return f"saved {path} ({len(text)} bytes)"


def list_files():
    return ", ".join(sorted(p.name for p in WORKSPACE.iterdir())) or "(empty)"


TOOLS = {"fetch": fetch, "write_file": write_file, "list_files": list_files}


def model(messages):
    print("\n" + "=" * 70)
    print("everything the model gets to see this turn:")
    for m in messages:
        print(f"  [{m['role']}] {m['content']}")
    print("=" * 70)
    return input("you are the model > ").strip()


def run(task):
    WORKSPACE.mkdir(exist_ok=True)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for step in range(1, MAX_STEPS + 1):
        print(f"\n--- step {step}/{MAX_STEPS} ---")
        reply = model(messages)

        if not reply.startswith("TOOL "):
            print(f"\nno tool call, so the model thinks it's done.\nfinal answer: {reply}")
            return

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
            # Errors go back to the model as text instead of crashing the run,
            # so it gets a chance to notice and try something else.
            result = f"error: {type(e).__name__}: {e}"

        print(f"tool result: {result}")
        # The model keeps no memory between calls, so the whole history is resent every time.
        messages.append({"role": "model", "content": reply})
        messages.append({"role": "tool", "content": result})

    print(f"\nhit MAX_STEPS={MAX_STEPS}, stopping the run no matter what the model wants.")


if __name__ == "__main__":
    run("Fetch https://example.com and save the page's main heading into heading.txt")
