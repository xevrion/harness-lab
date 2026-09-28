# 07: the loop

By the end of lab 06 I was the loop. Run the harness, read the output, decide whether it was worth going again, type the same command, repeat. This lab takes that job away from me. `loop.py` runs the lab 06 agent over and over, and after each run plain code decides whether to go again.

```
.venv/bin/python 07-the-loop/loop.py
.venv/bin/python 07-the-loop/loop.py --watch
.venv/bin/python 07-the-loop/loop.py --max-runs 3 --budget 400000
```

The first time you run it, it copies lab 06's `memory/` and `workspace/` over, so the papers you already have (and everything the notebook learned) carry on from there. After that it only uses its own. `--fresh` starts lab 07 from nothing, and `--selftest` checks the loop logic offline.

The loop also writes each run's task itself. The agent is told which target years (2017 to 2025 by default, `--years`) are still missing, instead of being handed the same vague prompt every time. Nobody types a prompt anymore; the loop builds one from what's on disk.

## When it stops

This is the part that has to be in code. If the only way out of a loop is the model deciding it's finished, it runs until the key is empty, and it'll do that while you're asleep. So after every run it checks, in this order:

- **GOAL MET**: every target year is either downloaded or confirmed unavailable
- **BUDGET**: total input tokens hit `--budget` (default 800k), or the next run would cross it if it costs what the last one did
- **PLATEAU**: two runs in a row found nothing new, so a third probably won't either
- **MAX_RUNS**: `--max-runs`, default 5

## Proving a negative

In my second lab 06 run the agent read manabadi's 2023 page three times and walked away every time. I opened it myself: it only has the Maths 1B paper, no 1A. The agent had no way to say "there's nothing here", so it kept coming back, and it would have kept coming back in every run forever.

So there's a new tool, `report_not_found(year, url, reason)`. The harness doesn't believe it any more than it believed "done" back in lab 02. It fetches the page itself and only accepts the report if the page mentions that year and has no Maths 1A on it (or the page is broken, a 404 or a 500). If the page does mention 1A, the model is told "I checked that page and it does mention Maths 1A. Look again." A year only counts as unavailable once three different sites agree, because one site not having a paper doesn't mean it doesn't exist.

## Maker and checker

The agent writes lessons at the end of every run, and they pile up and start contradicting each other. In lab 06, run 1 said "use the year-wise URL for every year" and run 2 said "the year-wise URL only works for 2023". Both stayed in the notebook.

So between runs a second model call, the checker, rewrites the notebook. It gets the harness's facts and the agent's lessons, and it's told the facts win. It merges duplicates, drops file ids (the facts already keep those), and keeps at most 10 bullets. The old notebook is saved as `memory/notes.prev.md` and the diff is printed. Set `LLM_CHECKER_MODEL` in `.env` to use a different model for this, since the model that wrote a lesson is the worst one to judge it.

The checker's output is still model output, so the same filters apply: no invented sites, no exact ids, and an empty or broken reply keeps the old notes instead of wiping them.

It still can't catch everything. In my test run the agent dropped a parameter from manabadi's syllabus URL, got a 500, and wrote "the syllabus-wise page returns HTTP 500, avoid it". The recorded fact agrees with that lesson, so the checker kept it, and the real page (with all its parameters) works fine. A lesson can be consistent with the facts and still be wrong.

## Reading what it did

The loop is fast and you're not watching, which is the point and also the problem. If it ships work you never looked at, you slowly stop understanding your own project. So every run gets a short entry in `loop-log.md`: what it was asked for, the sites it visited, what it downloaded, what got refused, what not-found reports were confirmed or rejected, how the notebook changed, and why the loop went again or stopped. Read it. If a lesson in `memory/notes.md` looks wrong, delete the line.

## Costs

A run is roughly 100k to 250k input tokens on this task. The defaults (5 runs, 800k budget) can use most of a free tier's daily allowance, so start with `--max-runs 2` if you're unsure.
