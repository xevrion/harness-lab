# harness-lab

Small labs I'm writing while I learn harness engineering (and later, loop engineering).

I got into this after a friend built a little research agent that went off and collected every past paper he needed for his exams, sorted by year, while he did something else. My first reaction was "how", and the honest answer turned out to be less magical than I thought. The model in the middle only ever reads text and writes text. Everything that actually touches the internet or the disk is ordinary code wrapped around it, and that code is the harness.

So each lab here is one idea, small enough to read in one sitting, stdlib Python only, no frameworks. The early ones don't even need an API key.

## Labs

| # | Lab | The idea |
|---|---|---|
| 01 | [you are the model](01-you-are-the-model/) | You type the model's replies by hand into a real harness, and find out what the model does and doesn't do |

More get added as I go: the agent loop with a real model, a tool registry, context management, guardrails, and the verify step.

## Running

```
python3 01-you-are-the-model/harness.py
```

Needs Python 3.8 or newer. Each lab writes into its own `workspace/` folder, which is gitignored, so delete it whenever you want a clean run.

## What I'm learning from

- Tejas Kumar, [Harnesses in AI: A Deep Dive](https://www.youtube.com/watch?v=C_GG5g38vLU) (AI Engineer). The login-page story in there is the reason lab 01 has a "lie to the harness" exercise.
- Addy Osmani, [Agent Harness Engineering](https://addyosmani.com/blog/agent-harness-engineering/) and [Loop Engineering](https://addyosmani.com/blog/loop-engineering/)
- LangChain, [The Anatomy of an Agent Harness](https://www.langchain.com/blog/the-anatomy-of-an-agent-harness)
