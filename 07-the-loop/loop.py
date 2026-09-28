#!/usr/bin/env python3
"""Lab 07: you were the loop, retyping the command after every run. This runs it for you.

    python3 loop.py                              # keep running until a stop condition fires
    python3 loop.py --watch                      # same, with the browser window visible
    python3 loop.py --max-runs 3 --budget 400000
    python3 loop.py --fresh                      # forget everything, including what lab 06 found
    python3 loop.py --selftest                   # offline checks, no key needed
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

HERE = Path(__file__).parent
LOG = HERE / "loop-log.md"
PLATEAU_RUNS = 2
MAX_BULLETS = 10

CONSOLIDATE_PROMPT = (
    "You are the checker, not the agent that did the work. Below are FACTS recorded by harness code, which are "
    "trustworthy, and LESSONS written by the agent at the end of its runs, which may be wrong, stale or repeated. "
    f"Rewrite the lessons into at most {MAX_BULLETS} bullets, one per line, each starting with '- '. "
    "Merge duplicates. When a lesson disagrees with the facts, the facts win: fix the lesson or drop it. "
    "Leave out file ids, DocTypeIds and other exact identifiers, the facts already keep those. "
    "Keep only what helps the next run find the years that are still missing. "
    "Don't add anything that isn't in the facts or the lessons."
)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{25,}|DocTypeId\W*\d+|\bid=[\w-]{10,}", re.I)


def parse_years(text):
    years = set()
    for part in text.split(","):
        if "-" in part:
            start, end = part.split("-")
            years.update(range(int(start), int(end) + 1))
        elif part.strip():
            years.add(int(part))
    return sorted(years)


def seed(source, memory_dir, workspace):
    """Carry lab 06's progress over, but only into an empty lab 07, never on top of its own."""
    if (memory_dir / "facts.json").exists():
        runs = json.loads((memory_dir / "facts.json").read_text()).get("runs", 0)
        print(f"continuing from lab 07's own memory ({runs} run(s) so far).")
        return False
    if not (source / "memory" / "facts.json").exists():
        print("no earlier memory found, starting from nothing.")
        return False
    shutil.copytree(source / "memory", memory_dir, dirs_exist_ok=True)
    if (source / "workspace").exists():
        shutil.copytree(source / "workspace", workspace, dirs_exist_ok=True)
    print(f"seeded from {source.name}: copied its memory and workspace, so the papers it already found carry over.")
    return True


def progress(result):
    return len(result["new_pdfs"]) + len(result["new_unavailable"])


def stop_check(results, targets, have, unavailable, spent, max_runs, budget):
    """Decide in code whether the loop goes again. Returns (reason, explanation); reason is None to keep going."""
    # A loop whose only stop condition is the model deciding it's finished will run until your key
    # runs dry, probably while you're asleep. So every way out is plain arithmetic on real numbers.
    remaining = sorted(set(targets) - have - unavailable)
    if not remaining:
        return "GOAL MET", "every target year is downloaded or confirmed unavailable"
    if spent >= budget:
        return "BUDGET", f"spent {spent:,} input tokens, the cap is {budget:,}"
    if results and spent + results[-1]["tokens_in"] > budget:
        # Stopping once the cap is already crossed means one run can blow straight through it,
        # so assume the next run costs what the last one did.
        return "BUDGET", f"spent {spent:,}, and another run like the last ({results[-1]['tokens_in']:,}) would cross the cap of {budget:,}"
    stalled = 0
    for r in reversed(results):
        if progress(r):
            break
        stalled += 1
    if stalled >= PLATEAU_RUNS:
        return "PLATEAU", f"{stalled} runs in a row found no new paper and confirmed no missing year"
    if len(results) >= max_runs:
        return "MAX_RUNS", f"did {len(results)} runs, the limit is {max_runs}"
    return None, f"{len(remaining)} year(s) still missing ({', '.join(map(str, remaining))}), {stalled} run(s) in a row without progress"


def compact_facts(facts):
    return json.dumps({
        "downloaded": sorted({f"{d['year']} from {harness.host(d['url'])}" for d in facts["downloads"]}),
        "urls that served web pages instead of PDFs": {p: e["count"] for p, e in facts["refusals"].items()},
        "pages that returned errors": {u: e["status"] for u, e in facts.get("page_errors", {}).items()},
        "confirmed no Maths 1A": {y: [f"{e['host']}: {e['reason']}" for e in es] for y, es in facts.get("unavailable", {}).items()},
        "sites that gave real PDFs": facts["good_sources"],
        "visited with nothing to show": list(facts["dead_ends"]),
    }, indent=1)


def consolidate(memory_dir):
    """The checker rewrites the agent's notebook. Returns (old bullets, new bullets)."""
    mem = harness.Memory(memory_dir)
    old = [text for _, text in mem.notes]
    if not old:
        return old, old
    facts = compact_facts(mem.facts)
    # Ideally a different model: the one that wrote the lessons is the worst judge of them.
    checker = os.environ.get("LLM_CHECKER_MODEL") or None
    msg = harness.call_model(
        [{"role": "system", "content": CONSOLIDATE_PROMPT},
         {"role": "user", "content": f"FACTS:\n{facts}\n\nLESSONS:\n" + "\n".join(f"- {t}" for t in old)}],
        2, tools=False, model=checker,
    )
    bullet = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
    new = [bullet.sub("", line).strip()[:220] for line in (msg.get("content") or "").splitlines() if bullet.match(line)]

    # The checker is a model too, so its output gets the same treatment as the agent's lessons.
    known = (facts + " " + " ".join(old)).lower()
    kept = []
    for line in new:
        if IDENTIFIER.search(line):
            print(f"  consolidation: dropped, it carries an exact id that belongs in the facts: {line[:90]}")
        elif not all(d.lower() in known for d in harness.DOMAIN.findall(line)):
            print(f"  consolidation: dropped, it names a site that's in neither the facts nor the lessons: {line[:90]}")
        else:
            kept.append(line)
    kept = kept[:MAX_BULLETS]
    if not kept:
        print("  consolidation came back empty, keeping the old notes rather than wiping the notebook.")
        return old, old

    shutil.copy(mem.notes_file, memory_dir / "notes.prev.md")
    mem.notes = [(mem.run - 1, text) for text in kept]
    mem.write_notes()
    return old, kept


def show_diff(old, new):
    lines = [l for l in difflib.unified_diff(old, new, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    for line in lines[:16]:
        print(f"    {line[0]} {line[1:][:110]}")
    if len(lines) > 16:
        print(f"    ... and {len(lines) - 16} more changes")
    added = sum(1 for l in lines if l.startswith("+"))
    return f"{len(old)} bullets became {len(new)} ({added} added or rewritten, {len(lines) - added} removed or rewritten)"


def log_run(log, result, notes_summary, stop_line):
    ev = result["events"]
    pick = lambda prefix: sorted({e[len(prefix):] for e in ev if e.startswith(prefix)})
    downloads, refused = pick("downloaded: "), pick("refused: ")
    confirmed, rejected, errors = pick("not found, confirmed: "), pick("not found, rejected: "), pick("page error: ")
    entry = [
        f"## run {result['run']} ({datetime.now():%Y-%m-%d %H:%M})",
        "",
        f"- asked for: {', '.join(map(str, result['task_years'])) or 'nothing left'}",
        f"- outcome: {result['outcome']} after {result['steps']} steps, {result['tokens_in']:,} tokens in, {result['tokens_out']:,} out",
        f"- sites: {', '.join(result['visited']) or 'none'}",
        f"- downloaded: {'; '.join(downloads) or 'nothing'}",
    ]
    if refused:
        entry.append(f"- refused: {'; '.join(refused)}")
    if confirmed or rejected:
        entry.append(f"- not found: {len(confirmed)} confirmed ({'; '.join(confirmed)[:300]}), {len(rejected)} rejected by the checker")
    if errors:
        entry.append(f"- error pages: {'; '.join(errors)[:300]}")
    entry += [f"- notes: {notes_summary}", f"- stop check: {stop_line}", "", ""]
    with log.open("a") as f:
        f.write("\n".join(entry))


def main():
    parser = argparse.ArgumentParser(description="lab 07: run the harness again and again until a stop condition says stop")
    parser.add_argument("--years", default=f"{harness.TARGET_YEARS[0]}-{harness.TARGET_YEARS[-1]}", help="target years, e.g. 2017-2025 or 2020,2023")
    parser.add_argument("--max-runs", type=int, default=5)
    parser.add_argument("--budget", type=int, default=800_000, help="cap on total input tokens across the loop")
    parser.add_argument("--steps", type=int, default=25, help="max agent steps per run")
    parser.add_argument("--context", choices=harness.POLICIES, default="stubs")
    parser.add_argument("--watch", action="store_true", help="show the browser window")
    parser.add_argument("--seed-from", default="06", help="lab whose memory and workspace to start from, if lab 07 has none yet")
    parser.add_argument("--fresh", action="store_true", help="wipe lab 07's memory and workspace and don't seed")
    parser.add_argument("--selftest", action="store_true", help="offline checks, no key needed")
    args = parser.parse_args()
    if args.selftest:
        selftest()

    harness.load_env()
    targets = parse_years(args.years)
    if args.fresh:
        shutil.rmtree(harness.MEMORY_DIR, ignore_errors=True)
        shutil.rmtree(harness.WORKSPACE, ignore_errors=True)
        print("--fresh: wiped lab 07's memory and workspace.")
    else:
        source = HERE.parent / next((p.name for p in HERE.parent.iterdir() if p.name.startswith(args.seed_from)), args.seed_from)
        seed(source, harness.MEMORY_DIR, harness.WORKSPACE)

    with LOG.open("a") as f:
        f.write(f"# loop started {datetime.now():%Y-%m-%d %H:%M}\n\ntargets {args.years}, max {args.max_runs} runs, "
                f"budget {args.budget:,} input tokens, {args.steps} steps per run, model {os.environ['LLM_MODEL']}\n\n")

    results, spent, reason = [], 0, None
    while reason is None:
        print(f"\n{'=' * 70}\nloop: starting run {len(results) + 1} of at most {args.max_runs}, {spent:,} tokens spent so far\n{'=' * 70}")
        try:
            result = harness.run_once(targets, args.steps, args.context, headless=not args.watch)
        except SystemExit:
            reason, why = "PROVIDER ERROR", "the model provider kept failing, see the error above"
            break
        except KeyboardInterrupt:
            reason, why = "STOPPED BY YOU", "ctrl-c"
            break
        results.append(result)
        spent += result["tokens_in"]

        print("\nloop: consolidating the notebook (maker wrote it, checker rewrites it)")
        before = harness.usage_total["in"]
        try:
            old, new = consolidate(harness.MEMORY_DIR)
            notes_summary = show_diff(old, new) if old != new else "unchanged"
        except (Exception, SystemExit) as e:
            notes_summary = f"consolidation failed ({type(e).__name__}), notes left as they were"
        print(f"  {notes_summary}")
        spent += harness.usage_total["in"] - before

        mem = harness.Memory(harness.MEMORY_DIR)
        reason, why = stop_check(results, targets, mem.have_years(harness.WORKSPACE), mem.unavailable_years(),
                                 spent, args.max_runs, args.budget)
        stop_line = f"{reason}: {why}" if reason else f"go again, {why}"
        print(f"\nloop: {stop_line}")
        log_run(LOG, result, notes_summary, stop_line)

    mem = harness.Memory(harness.MEMORY_DIR)
    have, gone = mem.have_years(harness.WORKSPACE), mem.unavailable_years()
    print(f"\n{'=' * 70}\nloop stopped: {reason}. {why}\n")
    for r in results:
        print(f"  run {r['run']}: {r['outcome']:8} {len(r['new_pdfs'])} new pdf(s), {len(r['new_unavailable'])} confirmed not-found, {r['tokens_in']:,} tokens in")
    print(f"\n  have: {', '.join(map(str, sorted(have))) or 'nothing'}")
    print(f"  confirmed unavailable: {', '.join(map(str, sorted(gone))) or 'none'}")
    print(f"  still missing: {', '.join(map(str, sorted(set(targets) - have - gone))) or 'none'}")
    print(f"  input tokens across the loop: {spent:,}")
    if reason in ("PROVIDER ERROR", "STOPPED BY YOU"):
        with LOG.open("a") as f:
            f.write(f"loop stopped during a run: {reason} ({why})\n\n")
    print(f"\nwhat happened while you weren't watching: {LOG}")


def selftest():
    checks = harness.selftest()
    tmp = Path(tempfile.mkdtemp())

    def fake(run, pdfs=0, unavailable=0, tokens=100_000):
        return {"run": run, "new_pdfs": ["x.pdf"] * pdfs, "new_unavailable": [("2022", "h")] * unavailable, "tokens_in": tokens}

    targets = [2023, 2024, 2025]
    check = lambda results, have=(), gone=(), spent=0, max_runs=5, budget=800_000: stop_check(results, targets, set(have), set(gone), spent, max_runs, budget)[0]
    checks["stop: goal met when every year is had or unavailable"] = check([fake(1, pdfs=1)], have={2024, 2025}, gone={2023}) == "GOAL MET"
    checks["stop: budget once the cap is crossed"] = check([fake(1, pdfs=1)], spent=900_000) == "BUDGET"
    checks["stop: budget when the next run would cross it"] = check([fake(1, pdfs=1, tokens=300_000)], spent=600_000) == "BUDGET"
    checks["stop: plateau after two runs with no progress"] = check([fake(1, pdfs=1), fake(2), fake(3)], spent=300_000) == "PLATEAU"
    checks["stop: one empty run isn't a plateau"] = check([fake(1, pdfs=1), fake(2)], spent=200_000) is None
    checks["stop: a confirmed not-found counts as progress"] = check([fake(1), fake(2, unavailable=1)], spent=200_000) is None
    checks["stop: max runs"] = check([fake(i, pdfs=1) for i in range(1, 4)], spent=300_000, max_runs=3) == "MAX_RUNS"
    checks["years: ranges and lists parse"] = parse_years("2017-2019,2023") == [2017, 2018, 2019, 2023]

    # seeding: lab 06's progress comes over once, and never overwrites lab 07's own memory
    six, mem_dir, ws = tmp / "06-memory-across-runs", tmp / "memory", tmp / "workspace"
    (six / "memory").mkdir(parents=True)
    (six / "workspace" / "2024").mkdir(parents=True)
    (six / "memory" / "facts.json").write_text(json.dumps({"runs": 2, "downloads": [{"run": 1, "year": "2024", "file": "2024/a.pdf", "url": "https://x/a.pdf"}]}))
    (six / "memory" / "notes.md").write_text("- [run 1] use manabadi year pages\n")
    (six / "workspace" / "2024" / "a.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 12_000)
    checks["seed: copies memory and workspace into an empty lab 07"] = seed(six, mem_dir, ws) and (ws / "2024" / "a.pdf").exists()
    checks["seed: run numbers carry on from lab 06"] = harness.Memory(mem_dir).run == 3
    checks["seed: never on top of lab 07's own memory"] = seed(six, mem_dir, ws) is False

    # consolidation: facts go to the checker, and its output is filtered like any model output
    facts = json.loads((mem_dir / "facts.json").read_text())
    facts["page_errors"] = {"https://www.manabadi.co.in/x.aspx?bqpYear=2022": {"status": 500, "run": 2}}
    (mem_dir / "facts.json").write_text(json.dumps(facts))
    (mem_dir / "notes.md").write_text("- [run 1] for each year use the manabadi year-wise url\n- [run 2] year-wise url works for every year\n")
    sent = {}

    def fake_model(reply):
        def call(messages, total, tools=True, model=None):
            sent["prompt"] = messages[0]["content"] + messages[1]["content"]
            return {"content": reply}
        return call

    real_call = harness.call_model
    harness.call_model = fake_model("\n".join([
        "- the manabadi year-wise url errors for 2022, use the syllabus page for older years",
        "- the 2020 paper is file 16C0_I3WIBOq0a-Hdb7KmDysV7cyHOVuq on drive",
        "- tsbie.cgg.gov.in has every paper",
    ] + [f"- keep checking year {y}" for y in range(2010, 2022)]))
    old, new = consolidate(mem_dir)
    checks["consolidate: the checker is shown the facts and told they win"] = "bqpYear=2022" in sent["prompt"] and "the facts win" in sent["prompt"]
    checks["consolidate: exact ids are dropped"] = not any("16C0_" in line for line in new)
    checks["consolidate: invented sites are dropped"] = not any("tsbie" in line for line in new)
    checks[f"consolidate: at most {MAX_BULLETS} bullets"] = len(new) == MAX_BULLETS and new[0].startswith("the manabadi year-wise url errors")
    checks["consolidate: old notes kept as notes.prev.md"] = "works for every year" in (mem_dir / "notes.prev.md").read_text()
    checks["consolidate: rewriting notes doesn't bump the run number"] = harness.Memory(mem_dir).run == 3
    block = harness.Memory(mem_dir).prompt_block()
    checks["consolidate: facts still reach the next run, above the lessons"] = block.index("Pages that returned errors") < block.index("Lessons (if one disagrees")
    harness.call_model = fake_model("sorry, I can't help with that")
    checks["consolidate: an empty reply keeps the old notes"] = consolidate(mem_dir)[1] == new
    harness.call_model = real_call

    log = tmp / "loop-log.md"
    result = {"run": 3, "task_years": [2023, 2022], "outcome": "partial", "steps": 25, "tokens_in": 150_000, "tokens_out": 3_000,
              "visited": ["manabadi.co.in"], "new_pdfs": [], "new_unavailable": [],
              "events": ["downloaded: 2020/a.pdf from drive.google.com", "not found, confirmed: 2023 at manabadi.co.in (only 1B)",
                         "refused: docs.aglasem.com/view (web page, not a PDF)"]}
    log_run(log, result, "12 bullets became 8", "go again, 5 year(s) still missing")
    text = log.read_text()
    checks["log: one readable entry per run"] = all(s in text for s in (
        "## run 3", "asked for: 2023, 2022", "150,000 tokens in", "downloaded: 2020/a.pdf", "1 confirmed", "refused: docs.aglasem.com/view",
        "notes: 12 bullets became 8", "stop check: go again"))

    shutil.rmtree(tmp)
    for name, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    print(f"\n{sum(checks.values())}/{len(checks)} checks passed")
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
