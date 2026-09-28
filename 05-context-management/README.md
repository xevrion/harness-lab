# 05: context management

Lab 04 found two papers and used 250,333 input tokens doing it. Step 1 sent 993. The model never got more wordy, it's that every page it read stayed in the conversation, and the whole conversation goes out again on every single call. By the end it was paying to re-read the same aglasem viewer page for the fifteenth time.

So this lab is the same agent with one new knob: what actually gets sent each step. The full history always stays in memory, the policy only decides what the model sees on this call.

Think of the model's context as a desk:

- **full**: never clear the desk. Everything you've ever looked at stays on it (this is lab 04)
- **window**: keep the task card and the last 8 things, throw the rest in the bin
- **stubs**: keep a note of every page you looked at, but only the last two pages stay open, the older ones get folded down to one line saying where they were

There's one rule all of them have to respect. If the model asks for a tool, the answer to that exact request has to be sent along with it, and OpenAI-compatible APIs reject the whole request if a tool result shows up without the message that asked for it. So `window` backs up a message when its cut would land in the middle of a batch, and `stubs` shrinks a result's text but never removes the message itself. `--selftest` checks both, offline, no key needed.

## Race them

Same task, same model, three policies:

```
.venv/bin/python 05-context-management/harness.py --context full
.venv/bin/python 05-context-management/harness.py --context window
.venv/bin/python 05-context-management/harness.py --context stubs
```

Each run adds a line to `results.csv` in this folder (gitignored), so after the three runs you can just `cat` it. Things to compare:

- total tokens in, obviously
- how many PDFs each one actually got, because saving tokens is worthless if the agent gets dumber
- whether `window` goes back to a site it already tried. It has no memory of anything older than 8 messages, so it can't know. The "already have it" check in `download_pdf` stops it downloading twice, but it can still waste steps finding the same page again
- whether `stubs` ever calls `read_page` again on a page it folded, and if so, whether that cost less than keeping the page open would have

Per step it prints `sent 10 of 24 messages`, so you can watch each policy kick in. `--steps 10` makes a quicker run.

## Two smaller fixes from lab 04's run

- `click` timed out twice on paper sites because an ad was sitting on top of the link. Now if a click times out and the element is a link, the harness just opens its `href` directly and tells the model it did that.
- The model called `read_page` twice in a row on the same page, which put a second full copy of it in the conversation. If nothing happened in between and the page is identical, the second read now returns a one-line "no change since step 13" instead.
