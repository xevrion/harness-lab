# 06: memory across runs

Lab 05 raced three context policies, and the thing that stood out wasn't in the results table. All three runs walked into the same aglasem viewer trap, all three got stuck on the same sakshi pages, and two of them found the same working adda247 link, each one from scratch. Context is the model's desk for one run. When the run ends the desk gets cleared, so the next run can't know any of it happened.

So this lab gives the harness a notebook that stays on disk between runs, in `memory/` (gitignored). There are two kinds of note in it, and they're kept apart on purpose:

- **facts.json** is written by harness code, straight from tool results: which files were saved and from where, which URL patterns served a web page instead of a PDF, which sites led to real papers, which sites were visited and gave nothing. Nothing the model says goes in here, so it can be trusted.
- **notes.md** is written by the model. At the end of each run the harness makes one extra call, hands over a short log of the run, and asks for a few specific lessons for next time. These are useful and also sometimes wrong, so they're labelled that way when they're shown back.

At the start of every run both get turned into a "MEMORY FROM PREVIOUS RUNS" block at the end of the system prompt, and it's printed so you can see exactly what the model is told.

## Run it twice

```
.venv/bin/python 06-memory-across-runs/harness.py
.venv/bin/python 06-memory-across-runs/harness.py
```

The workspace isn't wiped between runs anymore, since keeping progress is the whole point. Lab 02 wiped it because an old file could make the verifier pass on work that never happened, so here the facts log decides what's new: a run only passes if it saved PDFs itself, and the ones from earlier runs don't count.

On the second run, watch for:

- the memory block at the top, and whether the model actually skips the year it already has
- `download_pdf` refusing a known viewer URL straight away with "refused without trying", instead of fetching it and finding out again
- the steps and tokens it takes to get its first new PDF, compared with run 1. `results.csv` in this folder gets a row per run with the run number

`--fresh` wipes the workspace and the memory if you want to start over. `--selftest` checks the memory logic offline, no key needed.

## Bad memory is worse than no memory

A wrong note doesn't fail once, it gets believed by every run after it. When I first ran this, the run stopped after 4 steps and the model still wrote lessons about three sites it had never opened, confidently. So the harness defends itself in a few ways:

- a URL pattern is only auto-blocked after it has been refused twice, and never if it has ever given a real PDF, and the pattern is just the domain plus the first path segment, so blocking a viewer doesn't block the whole site
- a lesson that names a site this run never touched gets dropped before it's saved
- the notes are capped at 20, oldest out first, because memory ends up in the context too and would otherwise grow into the pile on the desk that lab 05 was about

The site check only proves a lesson is grounded, not that it's right. The model can still visit aglasem and then advise clicking its Download button, which is exactly the trap. If a lesson looks wrong, open `memory/notes.md` and delete the line. That's allowed, it's your notebook.

## One more fix from lab 05

In the stubs run the 2024 paper was sitting inside manabadi's own URL as a Google Drive `/preview` link, and lab 05 only knew how to handle `/view`. Now any Drive file id, anywhere in a URL, including percent-encoded ones inside another site's query string, gets turned into Drive's direct download link.
