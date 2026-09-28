# 02: the verify step

In lab 01 the harness believed whatever you said. Say "done" and the run ended, whether the file existed or not. When I tried it I saved the page into a file called `fetch` instead of the file the task asked for, said "ok im done", and the harness was perfectly happy.

This lab adds a `verify()` function. When the model says it's finished, the harness goes and checks the workspace itself: is `title.txt` there, is it non-empty, and does it match the page's real `<title>`? It fetches the page on its own to answer that last one, so nothing the model said is trusted.

```
python3 harness.py
```

If verify fails, the model is told what's wrong (without being handed the answer) and gets another go. After three failed attempts the run is marked failed.

## Things to try

1. Save the title into a file with the wrong name and say you're done.
2. Save the whole page into `title.txt` instead of just the title.
3. Don't call any tools at all, just keep insisting you're finished.
4. Do it properly and watch it pass.

The task is "title" rather than "main heading" because example.com has no `<h1>` at all, and a verify step can only check something that's actually defined.
