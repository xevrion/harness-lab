# 03: a real model, a real browser

Labs 01 and 02 had you typing the model's replies. This one hands the chair to an actual LLM and gives it a Chromium window you can watch. The harness is the same idea as before, it's just that the tools now drive a browser through Playwright and the model on the other end is real.

Two things worth noticing before you run it.

Calling the model is just an HTTP POST. There's no SDK here on purpose, `call_model()` builds a JSON body with the conversation and the tool schemas, sends it to `/chat/completions`, and reads back a message. That's all an "AI call" is.

The tool calls come back structured. In lab 01 the harness had to find `TOOL name {...}` in free text, and one missing word ended the run. Here the tools are sent as JSON schemas and the model's calls come back in their own `tool_calls` field, with an id that each result has to point back to.

## Setup

From the repo root:

```
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env
```

Then put a key in `.env`. Any provider with an OpenAI-compatible API works, these three are the easy ones:

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Key |
|---|---|---|---|
| Gemini (free tier) | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Groq (free tier) | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | [console.groq.com/keys](https://console.groq.com/keys) |
| DeepSeek (paid, a few cents) | `https://api.deepseek.com` | `deepseek-chat` | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |

Model names change more often than you'd like. If you get a 400 or 404 about the model, ask the provider for its current list at `<base url>/models`.

Free tiers are stingy (Gemini's gave me 5 requests a minute, and a single task can use more than that). When the provider says 429, the harness waits however long it was told to and tries again, up to 4 times, and those retries don't count as steps.

## Running

```
.venv/bin/python 03-real-model-browser/harness.py
```

The default task is to find the title of the #1 story on Hacker News. A browser window opens, slowed down a bit so you can follow it, and the terminal prints every tool call the model makes, what came back, and how many tokens each call used. Watch the token count on the way in grow every step, since the whole conversation gets resent each time, the same as in lab 01.

When the model answers, `verify()` fetches Hacker News on its own and checks the real title is in the answer. Get it wrong and the model is told so (not what the right answer is) and gets another try.

You can also give it your own task:

```
.venv/bin/python 03-real-model-browser/harness.py "search wikipedia for playwright and tell me who makes it"
```

There's no verifier for custom tasks, so the harness says so and takes the final answer on trust. Try a few and see how often that trust is misplaced.

## Things to try

1. Run the default task and read the terminal next to the browser. Every line is one trip through the loop.
2. Drop `MAX_STEPS` to 3 and give it something that needs more clicks.
3. Ask for something behind a login, like upvoting a story. That's the task from Tejas Kumar's talk. Does it admit it couldn't, or does it claim it worked?
4. Break `read_page` so it returns only the text and no numbered elements. Watch what the model does when it can see the page but has nothing to click.
5. Set `HARNESS_HEADLESS=1` in `.env` and it runs without a window, the way it would on a server.

One thing that can trip you up: Hacker News sometimes reshuffles between the model reading it and `verify()` checking, and then a correct answer fails. A verifier is code too, and code can be wrong.
