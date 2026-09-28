# 01: you are the model

A language model can't browse, can't save files and doesn't remember your last message. It gets a pile of text and gives back some text. This lab makes you feel that from the inside: the harness is real Python, but the model is you.

```
python3 harness.py
```

Every turn it prints exactly what the model would receive, and you type the reply. To use a tool, reply like this:

```
TOOL fetch {"url": "https://example.com"}
```

Anything that doesn't start with `TOOL` counts as the final answer, and the run ends.

## Four runs to try

Do them one at a time, each on a fresh run.

1. **Do the task properly.** Fetch the page, save the heading with `write_file`, then say you're done. While you do it, keep an eye on how the text you're shown gets longer every turn. The model forgets everything between calls, so the harness has to send the whole conversation again each time.
2. **Try to escape.** Ask for `{"name": "../../evil.txt", "text": "hi"}`, then look in `workspace/` to see where it really ended up.
3. **Refuse to stop.** Keep calling `list_files` forever and see what happens at step 5. You can't argue with a `for` loop.
4. **Lie.** Skip every tool and just reply `Done! I saved heading.txt`. The harness believes you. Then check the folder.

Run 4 is the one worth thinking about. Nothing in this harness checks whether the work actually happened, it just takes the model's word for it. In Tejas Kumar's talk the same thing happens with a real model: his agent hit a login page, never upvoted anything, and reported success anyway. That's where the next lab starts.
