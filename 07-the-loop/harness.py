#!/usr/bin/env python3
"""Lab 06's harness, reshaped so a loop can call it: run_once() does one run and returns what happened.

loop.py is the normal way in. Running this file directly does a single run, same as lab 06.

    python3 harness.py                  # one run
    python3 harness.py --selftest       # offline checks, no key needed
"""

import argparse
import csv
import difflib
import html
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from ddgs import DDGS
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
WORKSPACE = HERE / "workspace"
MEMORY_DIR = HERE / "memory"
RESULTS = HERE / "results.csv"
MAX_ATTEMPTS = 3
WINDOW_SIZE = 8
KEEP_READS = 2
BULKY_TOOLS = ("read_page", "web_search")
MAX_RETRIES = 4
MAX_WAIT = 60
MAX_PDF_BYTES = 25 * 1024 * 1024
BLOCK_AFTER = 2
MAX_NOTES = 20
MAX_NEW_LESSONS = 6
NOT_FOUND_SOURCES = 3
TARGET_YEARS = list(range(2017, 2026))
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def build_task(missing):
    years = ", ".join(str(y) for y in missing)
    return (
        "Find Telangana (TS) Intermediate 1st year Maths 1A previous year question papers "
        f"for these years, which you don't have yet: {years}. Download each one as a PDF into a folder named after its year. "
        "If you check a page and it clearly has no Maths 1A paper for a year, call report_not_found so later runs don't search it again."
    )


SYSTEM_PROMPT = (
    "You are a research agent with a real web browser and a downloader. "
    "Search with web_search, since search engines block the browser. Open promising pages with goto, "
    "then read_page, which lists the PDF links on the page along with the text around each one. "
    "Save papers with download_pdf, using the year as the folder. "
    "A download only happened if download_pdf says it saved the file, so never claim one it didn't confirm. "
    "When a page you checked has no Maths 1A paper for a year, say so with report_not_found. "
    "If there is a memory section below, use it: skip years that are already downloaded and don't retry known dead ends. "
    "When you're finished, reply with a plain text summary and no tool call."
)

LESSON_PROMPT = (
    "You just finished a run as a web agent. Below is the log of that run. "
    f"Write at most {MAX_NEW_LESSONS} lessons for the next run of the same task, one per line, each starting with '- '. "
    "Only specific, actionable things that would save steps next time: which sites or URL patterns gave real PDFs, "
    "which ones were traps, and where the papers for each year actually were. No generic advice. "
    "Only write what this log actually shows, never guess about sites that aren't in it. "
    "If nothing useful happened, reply with just: none"
)
DOMAIN = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|in|org|net|io|edu|gov)\b", re.I)

page = None
memory = None
TOOLS = {}
usage_total = {"in": 0, "out": 0, "calls": 0}
last_call = {}
last_read = {}


def host(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def url_pattern(url):
    # Domain plus the first path segment, e.g. docs.aglasem.com/view. Narrow enough that blocking
    # a viewer doesn't take a whole site down with it, wide enough to catch the next viewer URL.
    first = next((part for part in urlparse(url).path.split("/") if part), "")
    return f"{host(url)}/{first}" if first else host(url)


DRIVE_ID = re.compile(r"(?:drive|docs)\.google\.com/(?:file/d/|open\?(?:[^#\s]*?&)?id=|uc\?(?:[^#\s]*?&)?id=)([\w-]{10,})")


def drive_id(url):
    # Paper sites wrap Drive links inside their own URLs, sometimes percent-encoded,
    # so decode a couple of times and look anywhere in the string, not just at the start.
    for candidate in (url, unquote(url), unquote(unquote(url))):
        if match := DRIVE_ID.search(candidate):
            return match.group(1)
    return None


def is_real_pdf(path):
    with path.open("rb") as f:
        return f.read(4) == b"%PDF" and path.stat().st_size > 10_000


ONE_A = re.compile(r"(?<![a-z0-9])(?:1|i)\s*[-(]?\s*a\s*\)?(?![a-z0-9])", re.I)


def mentions_maths_1a(page_html):
    text = html.unescape(unquote(unquote(page_html))).lower()
    # Only look just after "math", so "1A" in a phone number or a css class doesn't count.
    # Covers "Maths-1A", "Maths I(A)", "Mathematics(EM) IA", and doesn't fire on "Maths-1B".
    return any(ONE_A.search(text[m.end():m.end() + 40]) for m in re.finditer(r"math", text))


def check_not_found(year, url):
    """Open the page ourselves and decide whether "no Maths 1A for this year here" holds up."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404 or e.code >= 500:
            return True, f"the page itself is broken (HTTP {e.code})"
        return False, f"couldn't check it (HTTP {e.code}), so it doesn't count"
    except Exception as e:
        return False, f"couldn't load it ({type(e).__name__}), so it doesn't count"

    # A page about 2024 says nothing about 2019, so it can't confirm 2019 is missing.
    if str(year) not in body and str(year) not in url:
        return False, f"that page doesn't mention {year} at all, so it can't tell us anything about {year}"
    if mentions_maths_1a(body):
        return False, "I checked that page and it does mention Maths 1A. Look again"
    return True, "no Maths 1A anywhere on the page"


class Memory:
    """What the harness remembers between runs.

    facts.json is written by harness code from tool results, so it can be trusted.
    notes.md is written by the model at the end of a run, so it's useful but could be wrong.
    """

    def __init__(self, folder):
        self.folder = Path(folder)
        self.facts_file = self.folder / "facts.json"
        self.notes_file = self.folder / "notes.md"
        self.facts = {"runs": 0, "downloads": [], "refusals": {}, "ok_patterns": [], "good_sources": {}, "dead_ends": {},
                      "unavailable": {}, "page_errors": {}}
        if self.facts_file.exists():
            self.facts.update(json.loads(self.facts_file.read_text()))
        self.notes = []
        if self.notes_file.exists():
            for line in self.notes_file.read_text().splitlines():
                if match := re.match(r"^- (?:\[run (\d+)\] )?(.+)", line):
                    self.notes.append((int(match.group(1) or 0), match.group(2)))
        self.run = self.facts["runs"] + 1
        self.visited, self.useful = set(), set()
        # What happened this run, in plain words, for the loop's log.
        self.events = []

    def record_visit(self, url):
        if h := host(url):
            self.visited.add(h)

    def record_page_error(self, url, status):
        self.facts["page_errors"][url[:200]] = {"status": status, "run": self.run}
        self.events.append(f"page error: {url[:100]} (HTTP {status})")

    def record_not_found(self, year, url, reason):
        entries = self.facts["unavailable"].setdefault(str(year), [])
        if not any(e["url"] == url for e in entries):
            entries.append({"url": url, "host": host(url), "reason": reason, "run": self.run})
        self.events.append(f"not found, confirmed: {year} at {host(url)} ({reason})")

    def have_years(self, workspace):
        return {int(d["year"]) for d in self.facts["downloads"]
                if str(d["year"]).isdigit() and (workspace / d["file"]).exists() and is_real_pdf(workspace / d["file"])}

    def unavailable_years(self):
        # Three pages on the same site are one opinion, so it takes three different sites.
        # One confirmation is cheap to get wrong and it would stop every future run from looking.
        return {int(y) for y, entries in self.facts["unavailable"].items()
                if y.isdigit() and len({e["host"] for e in entries}) >= NOT_FOUND_SOURCES}

    def record_download(self, url, file, folder, page_url):
        self.facts["downloads"].append({"run": self.run, "year": folder, "file": file, "url": url})
        self.events.append(f"downloaded: {file} from {host(url)}")
        # Credit the page the link was found on as well as the file host. On manabadi the file lives
        # on Google Drive, but manabadi is the site worth going back to.
        for h in {host(url), host(page_url)} - {""}:
            self.facts["good_sources"][h] = self.facts["good_sources"].get(h, 0) + 1
            self.useful.add(h)
        if url_pattern(url) not in self.facts["ok_patterns"]:
            self.facts["ok_patterns"].append(url_pattern(url))

    def record_refusal(self, url, reason):
        entry = self.facts["refusals"].setdefault(
            url_pattern(url), {"count": 0, "first_run": self.run, "reason": reason, "example": url[:120]}
        )
        entry["count"] += 1
        self.events.append(f"refused: {url_pattern(url)} ({reason})")

    def blocked(self, url):
        # Bad memory poisons every run after it, so one refusal is never enough to block a pattern,
        # and a pattern that has ever given a real PDF is never blocked at all.
        return self.pattern_blocked(url_pattern(url))

    def pattern_blocked(self, pattern):
        entry = self.facts["refusals"].get(pattern)
        if entry and entry["count"] >= BLOCK_AFTER and pattern not in self.facts["ok_patterns"]:
            return entry
        return None

    def new_this_run(self, workspace):
        files = [workspace / d["file"] for d in self.facts["downloads"] if d["run"] == self.run]
        return [f for f in files if f.exists() and is_real_pdf(f)]

    def finish_run(self):
        for h in self.visited - self.useful:
            entry = self.facts["dead_ends"].setdefault(h, {"runs": 0})
            entry["runs"] += 1
            entry["last_run"] = self.run
        for h in self.useful:
            self.facts["dead_ends"].pop(h, None)

    def add_lessons(self, lessons):
        def norm(text):
            return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())

        for lesson in lessons:
            # "the 2023 paper is on sakshi" and "the 2024 paper is on sakshi" are nearly the same
            # string but different facts, so only call it a duplicate when the numbers match too.
            duplicate = any(
                re.findall(r"\d+", lesson) == re.findall(r"\d+", old)
                and difflib.SequenceMatcher(None, norm(lesson), norm(old)).ratio() >= 0.85
                for _, old in self.notes
            )
            if not duplicate:
                self.notes.append((self.run, lesson))
        # Memory is context too. Left alone it grows forever and ends up as the pile on the desk again.
        self.notes = self.notes[-MAX_NOTES:]

    def new_unavailable_this_run(self):
        return [(y, e["host"]) for y, entries in self.facts["unavailable"].items() for e in entries if e["run"] == self.run]

    def fact_count(self):
        f = self.facts
        return (len(f["downloads"]) + len(f["refusals"]) + len(f["good_sources"]) + len(f["dead_ends"])
                + sum(len(v) for v in f["unavailable"].values()) + len(f["page_errors"]))

    def save(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.facts["runs"] = self.run
        self.facts_file.write_text(json.dumps(self.facts, indent=2))
        self.write_notes()

    def write_notes(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        header = f"# lessons from earlier runs\n\nWritten by the model at the end of each run. The harness keeps the newest {MAX_NOTES}.\n\n"
        self.notes_file.write_text(header + "".join(f"- [run {run}] {text}\n" for run, text in self.notes))

    def prompt_block(self):
        f = self.facts
        if not (f["downloads"] or f["refusals"] or f["good_sources"] or f["unavailable"] or f["page_errors"] or self.notes):
            return ""
        lines = [f"MEMORY FROM PREVIOUS RUNS ({f['runs']} so far). The facts were recorded by the harness itself. The lessons at the end were written by a model and could be wrong."]
        if f["downloads"]:
            lines.append("Already downloaded, don't download these years again:")
            lines += [f"- {d['year'] or 'no year'}: {d['file']} (from {host(d['url'])}, run {d['run']})" for d in f["downloads"]]
        if f["refusals"]:
            lines.append("URL patterns that served web pages instead of PDFs:")
            for pattern, e in sorted(f["refusals"].items(), key=lambda kv: -kv[1]["count"])[:8]:
                note = "download_pdf will skip it" if self.pattern_blocked(pattern) else "not blocked yet"
                lines.append(f"- {pattern}: refused {e['count']} time(s), {note}")
        if f["good_sources"]:
            lines.append("Sites that led to real PDFs:")
            lines += [f"- {h} ({n})" for h, n in sorted(f["good_sources"].items(), key=lambda kv: -kv[1])[:6]]
        if f["dead_ends"]:
            lines.append("Visited before with no PDF coming from it:")
            lines += [f"- {h} ({e['runs']} run(s))" for h, e in sorted(f["dead_ends"].items(), key=lambda kv: -kv[1]["runs"])[:6]]
        if f["unavailable"]:
            lines.append(f"Pages checked by the harness and confirmed to have no Maths 1A for that year "
                         f"(a year counts as unavailable once {NOT_FOUND_SOURCES} different sites agree):")
            for year, entries in sorted(f["unavailable"].items()):
                sites = len({e["host"] for e in entries})
                lines += [f"- {year}: {e['url'][:110]} ({e['reason']}), {sites}/{NOT_FOUND_SOURCES} sites" for e in entries[:4]]
        if f["page_errors"]:
            lines.append("Pages that returned errors:")
            lines += [f"- {url[:110]} (HTTP {e['status']}, run {e['run']})" for url, e in list(f["page_errors"].items())[-6:]]
        if self.notes:
            lines.append("Lessons (if one disagrees with a fact above, the fact wins):")
            lines += [f"- {text}" for _, text in self.notes]
        return "\n".join(lines)


def tool(description, **params):
    """Register a function as a tool. params maps each argument to (json type, what it means)."""
    def register(fn):
        signature = inspect.signature(fn)
        TOOLS[fn.__name__] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    # The description is prompt text. It's the only thing the model knows about
                    # the tool, so a vague one gets the tool misused or ignored.
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": {name: {"type": t, "description": d} for name, (t, d) in params.items()},
                        "required": [n for n, p in signature.parameters.items() if p.default is p.empty],
                    },
                },
            },
        }
        return fn
    return register


@tool(
    "Search the web and get back the top results as title, URL and a short snippet. "
    "Use this to find pages. It does not open them.",
    query=("string", "what to search for"),
)
def web_search(query):
    results = DDGS().text(query, max_results=8)
    if not results:
        return "no results"
    return "\n".join(f"- {r['title']}\n  {r['href']}\n  {r['body'][:150]}" for r in results)


@tool(
    "Open a web page in the browser. Don't use this for PDF files, use download_pdf for those.",
    url=("string", "full URL including https://"),
)
def goto(url):
    response = page.goto(url, wait_until="domcontentloaded")
    memory.record_visit(page.url)
    if response and response.status >= 400:
        memory.record_page_error(page.url, response.status)
        return f"now at {page.url} ({page.title()}), but the server answered HTTP {response.status}"
    return f"now at {page.url} ({page.title()})"


READ_PAGE_JS = r"""
() => {
  document.querySelectorAll('[data-harness-id]').forEach(el => el.removeAttribute('data-harness-id'));
  const els = [...document.querySelectorAll('a, button, input, textarea, select, [role=button]')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .slice(0, 30);
  const items = els.map((el, i) => {
    el.setAttribute('data-harness-id', i);
    const label = (el.innerText || el.getAttribute('aria-label') || el.placeholder || el.value || el.name || '').trim();
    return `[${i}] <${el.tagName.toLowerCase()}> ${label.slice(0, 80)}`;
  });
  const seen = new Set();
  const pdfs = [...document.querySelectorAll('a[href]')]
    .filter(a => (/\.pdf(\?|#|$)/i.test(a.href) || /drive\.google\.com/.test(decodeURIComponent(a.href))) && !seen.has(a.href) && seen.add(a.href))
    .slice(0, 40)
    .map(a => {
      // Paper sites label every link "Click Here", so the row around it is the real name.
      const row = a.closest('tr, li, p');
      const label = (row ? row.innerText : a.innerText).replace(/\s+/g, ' ').trim();
      return `- ${label.slice(0, 90)}\n  ${a.href}`;
    });
  return { text: document.body.innerText, items, pdfs };
}
"""


@tool("Read the current page: its text, a numbered list of links, buttons and inputs, and every PDF link on it.")
def read_page():
    data = page.evaluate(READ_PAGE_JS)
    text = " ".join(data["text"].split())[:1500]
    out = f"url: {page.url}\ntitle: {page.title()}\n\ntext:\n{text}\n\nelements:\n" + "\n".join(data["items"])
    if data["pdfs"]:
        out += "\n\npdf links:\n" + "\n".join(data["pdfs"])

    # In lab 04 the model read the same page twice in a row, which put a second full copy of it
    # in the conversation to be resent on every step after. Only when the previous call was this
    # same read, so the first copy is still the newest thing in context under every policy.
    if last_call.get("name") == "read_page" and last_read.get("url") == page.url and last_read.get("out") == out:
        return f"no change since step {last_read['step']}: same page, same text. Use what you read then, or go somewhere else."
    last_read.update(url=page.url, out=out, step=last_call.get("current_step"))
    return out


@tool("Click element number n from the latest read_page.", n=("integer", "the element's number"))
def click(n):
    target = f'[data-harness-id="{n}"]'
    try:
        page.click(target, timeout=5000)
    except PlaywrightTimeout:
        # Twice in lab 04 an ad was sitting on top of the link, so Playwright waited for it to
        # become clickable and gave up. A link's href doesn't care what's covering it.
        href = page.get_attribute(target, "href", timeout=1000)
        if not href:
            raise
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
        memory.record_visit(page.url)
        return f"click on [{n}] timed out (something was covering it), so I opened its link directly, now at {page.url}"
    page.wait_for_load_state("domcontentloaded")
    memory.record_visit(page.url)
    return f"clicked [{n}], now at {page.url}"


@tool(
    "Type text into input number n from the latest read_page, optionally pressing Enter.",
    n=("integer", "the input's number"),
    text=("string", "what to type"),
    submit=("boolean", "press Enter afterwards"),
)
def type_text(n, text, submit=False):
    target = f'[data-harness-id="{n}"]'
    page.fill(target, text, timeout=5000)
    if submit:
        page.press(target, "Enter")
        page.wait_for_load_state("domcontentloaded")
    return f"typed into [{n}]" + (f", submitted, now at {page.url}" if submit else "")


def clean(part):
    part = re.sub(r"\.{2,}", "", part)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip(".-")[:80]


def size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n // 1000} KB"


class TooBig(Exception):
    pass


def fetch_capped(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        length = r.headers.get("Content-Length")
        if length and int(length) > MAX_PDF_BYTES:
            raise TooBig(int(length))
        chunks, total = [], 0
        # Content-Length can be missing or wrong, so count the bytes as they arrive as well.
        while chunk := r.read(64 * 1024):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise TooBig(total)
            chunks.append(chunk)
        return b"".join(chunks), r.headers.get("Content-Type", "?")


@tool(
    "Download a PDF from a URL into the workspace. Checks it's really a PDF before saving. "
    "Put papers from the same year in the same folder.",
    url=("string", "the PDF's URL, usually from the pdf links in read_page"),
    filename=("string", "a short descriptive name, e.g. maths-1a-march-2024.pdf"),
    folder=("string", "optional subfolder, e.g. the year"),
)
def download_pdf(url, filename, folder=None):
    url = urljoin(page.url, url) if page else url
    # In lab 05 the paper was sitting inside manabadi's own URL as a Drive /preview link, and
    # the old check only knew /view. Any Drive file id, anywhere in the URL, now gets the direct link.
    if file_id := drive_id(url):
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
    if urlparse(url).scheme not in ("http", "https"):
        return f"refused: only http(s) URLs can be downloaded, got {url[:60]}"
    if hit := memory.blocked(url):
        return (f"refused without trying: {url_pattern(url)} is a known dead end, it served a web page instead of a PDF "
                f"{hit['count']} times since run {hit['first_run']}. Look for a direct .pdf link somewhere else.")

    name = clean(Path(filename).name) or "download"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    target_dir = WORKSPACE / clean(folder) if folder and clean(folder) else WORKSPACE
    dest = target_dir / name
    # clean() already strips slashes, this just makes sure nothing can land outside the workspace.
    if not dest.resolve().is_relative_to(WORKSPACE.resolve()):
        return "refused: that path is outside the workspace"
    if dest.exists():
        return f"already have it: {dest.relative_to(WORKSPACE.parent)} ({size(dest.stat().st_size)})"

    try:
        data, content_type = fetch_capped(url)
    except TooBig as e:
        return f"refused: file is at least {size(e.args[0])}, the limit is {size(MAX_PDF_BYTES)}"
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403) or page is None:
            raise
        # Some sites only serve files to a browser that has visited them, so try again with its cookies.
        response = page.request.get(url, timeout=30000)
        data, content_type = response.body(), response.headers.get("content-type", "?")
        if len(data) > MAX_PDF_BYTES:
            return f"refused: file is {size(len(data))}, the limit is {size(MAX_PDF_BYTES)}"

    # Content-Type lies both ways, and paper sites love serving an HTML page at a .pdf URL.
    # The first bytes of a real PDF are always "%PDF", so they decide.
    if not data.startswith(b"%PDF"):
        memory.record_refusal(url, "web page, not a PDF")
        return f"refused: that isn't a PDF (server said {content_type}, file starts with {data[:20]!r}). Probably a viewer or web page, not the file itself, so look for a direct .pdf link or try another site."

    target_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    memory.record_download(url, str(dest.relative_to(WORKSPACE)), clean(folder) if folder else "", page.url if page else "")
    return f"saved {dest.relative_to(WORKSPACE.parent)} ({size(len(data))})"


@tool(
    "Report that a page you checked has no Maths 1A paper for a year, so later runs skip it. "
    "The harness opens the page itself before believing you, so only report pages you actually looked at.",
    year=("string", "the year, e.g. 2023"),
    url=("string", "the page you checked"),
    reason=("string", "what you saw on it, e.g. 'lists only Maths 1B for 2023'"),
)
def report_not_found(year, url, reason):
    year = str(year).strip()
    if not re.fullmatch(r"(19|20)\d\d", year):
        return f"error: year should look like 2023, got {year!r}"
    # Same rule as download_pdf: the model's word is not evidence, the page is.
    ok, why = check_not_found(year, url)
    if not ok:
        memory.events.append(f"not found, rejected: {year} at {host(url)} ({why})")
        return why
    memory.record_not_found(year, url, reason[:150])
    sites = len({e["host"] for e in memory.facts["unavailable"][year]})
    return f"confirmed: {why}. Noted for {year}, {sites}/{NOT_FOUND_SOURCES} sites so far. Move on to another year or another site."


def verify_downloads(_answer):
    # Lab 02 wiped the workspace so an old file couldn't pass for new work. Here the workspace has
    # to survive, since keeping progress is the whole point, so the facts log decides what's new.
    new = memory.new_this_run(WORKSPACE)
    if new or memory.new_unavailable_this_run():
        return None
    total = sum(1 for p in WORKSPACE.rglob("*.pdf") if is_real_pdf(p))
    if total:
        return (f"no new PDFs were saved and no missing year was confirmed with report_not_found this run. "
                f"The workspace has {total} from earlier runs, and those don't count")
    return "there are no real PDFs in the workspace, nothing was actually downloaded"


def print_tree():
    files = sorted(p for p in WORKSPACE.rglob("*") if p.is_file())
    new = set(memory.new_this_run(WORKSPACE))
    print(f"\n{WORKSPACE.name}/  ({len(files)} files)")
    last_dir = None
    for f in files:
        rel = f.relative_to(WORKSPACE)
        nested = rel.parent != Path(".")
        if nested and rel.parent != last_dir:
            print(f"  {rel.parent}/")
            last_dir = rel.parent
        status = "ok" if is_real_pdf(f) else "NOT a real pdf"
        print(f"{'    ' if nested else '  '}{f.name}  {size(f.stat().st_size)}  {status}{'  (new this run)' if f in new else ''}")


def load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

    missing = [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(k)]
    if missing:
        print(f"missing {', '.join(missing)}.\n")
        print("copy .env.example to .env in the repo root and fill it in.")
        print("provider presets are in 03-real-model-browser/README.md.")
        sys.exit(1)


# The full history stays in memory. A policy only decides what gets sent on this one call.
# Bookkeeping lives in keys starting with "_" and is stripped before anything goes over the wire.

def wire(messages):
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def full(history):
    return wire(history)


def window(history):
    head, rest = history[:2], history[2:]
    start = max(0, len(rest) - WINDOW_SIZE)
    # A tool result without the assistant message that asked for it gets the whole request
    # rejected by OpenAI-compatible APIs, so never start the window in the middle of a batch.
    while start > 0 and rest[start]["role"] == "tool":
        start -= 1
    return wire(head + rest[start:])


def stubs(history):
    bulky = [i for i, m in enumerate(history) if m.get("_bulky")]
    old = set(bulky[:-KEEP_READS])
    out = []
    for i, m in enumerate(history):
        if i in old:
            # Same message, same tool_call_id, just a one-line receipt instead of the page.
            if m["_tool"] == "web_search":
                what, again = f"searched for {m['_query']!r}", "search again"
            else:
                what, again = f"read {m['_url']} ({m['_title']})", "go back and call read_page"
            m = {**m, "content": f"[{what} at step {m['_step']}, text removed to save context. {again} if you need it.]"}
        out.append(m)
    return wire(out)


POLICIES = {"full": full, "window": window, "stubs": stubs}


def call_model(messages, total, tools=True, model=None):
    body = {"model": model or os.environ["LLM_MODEL"], "messages": messages}
    if tools:
        body["tools"] = [t["schema"] for t in TOOLS.values()]
    req = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
    )
    for retry in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            if (e.code == 429 or e.code >= 500) and retry < MAX_RETRIES:
                wait = min(retry_wait(e, error_body, retry), MAX_WAIT)
                print(f"  {'rate limited' if e.code == 429 else f'provider error {e.code}'}, waiting {wait:.0f}s (retry {retry + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            report_http_error(e.code, error_body)

    if usage := data.get("usage"):
        usage_total["in"] += usage.get("prompt_tokens") or 0
        usage_total["out"] += usage.get("completion_tokens") or 0
        usage_total["calls"] += 1
        print(f"  tokens: {usage.get('prompt_tokens')} in, {usage.get('completion_tokens')} out  (sent {len(messages)} of {total} messages)")
    return data["choices"][0]["message"]


def retry_wait(e, error_body, retry):
    if e.headers and (header := e.headers.get("Retry-After")):
        try:
            return float(header)
        except ValueError:
            pass
    if match := re.search(r'retry in ([\d.]+)\s*s|"retryDelay":\s*"([\d.]+)s"', error_body, re.I):
        return float(match.group(1) or match.group(2)) + 1
    return 2 ** (retry + 1)


def report_http_error(code, error_body):
    print(f"\nthe provider said {code}: {error_body[:500]}")
    if code in (401, 403):
        print("the key was rejected. check LLM_API_KEY in .env.")
    elif code in (400, 404):
        print("if that's about the model, the name may have changed. list what's available with:")
        print(f'  curl -H "Authorization: Bearer <your key>" {os.environ["LLM_BASE_URL"].rstrip("/")}/models')
    elif code == 429:
        print("still rate limited after retrying. wait a bit and rerun.")
    sys.exit(1)


def run(history, policy, max_steps):
    attempts = 0

    for step in range(1, max_steps + 1):
        print(f"\n--- step {step}/{max_steps} ---")
        last_call["current_step"] = step
        msg = call_model(POLICIES[policy](history), len(history))
        tool_calls = msg.get("tool_calls") or []
        history.append({"role": "assistant", "content": msg.get("content"), **({"tool_calls": tool_calls} if tool_calls else {})})

        if not tool_calls:
            print(f"model says it's done: {msg.get('content') or ''}")
            problem = verify_downloads(msg.get("content"))
            if problem is None:
                print(f"\nVERIFIED: {len(memory.new_this_run(WORKSPACE))} new PDF(s), "
                      f"{len(memory.new_unavailable_this_run())} confirmed not-found this run.")
                return "verified", step
            attempts += 1
            print(f"verify failed ({attempts}/{MAX_ATTEMPTS}): {problem}")
            if attempts == MAX_ATTEMPTS:
                print("out of attempts, marking the run FAILED.")
                return "failed", step
            history.append({"role": "user", "content": f"Not done: {problem}. Keep going."})
            continue

        for call in tool_calls:
            name, args = call["function"]["name"], {}
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                print(f"model calls {name}({args})")
                result = TOOLS[name]["fn"](**args) if name in TOOLS else f"error: no tool named '{name}'"
            except Exception as e:
                result = f"error: {type(e).__name__}: {e}"
            print("  " + result.replace("\n", " ")[:150])

            entry = {"role": "tool", "tool_call_id": call["id"], "content": result, "_tool": name, "_step": step}
            if name in BULKY_TOOLS and not result.startswith(("error", "no change")):
                entry.update(_bulky=True, _url=page.url, _title=page.title(), _query=args.get("query"))
            history.append(entry)
            last_call["name"] = name

    # Running out of steps while chasing one more year shouldn't throw away the papers already saved.
    print(f"\nhit MAX_STEPS={max_steps}, stopping.")
    problem = verify_downloads(None)
    print("PARTIAL: out of steps, but this run made real progress." if problem is None else f"FAILED: {problem}")
    return ("partial" if problem is None else "failed"), max_steps


def run_log(history):
    calls, lines = {}, []
    for m in history:
        for c in m.get("tool_calls") or []:
            calls[c["id"]] = (c["function"]["name"], c["function"].get("arguments") or "{}")
        if m["role"] == "tool":
            name, args = calls.get(m["tool_call_id"], ("?", "{}"))
            result = " ".join(m["content"].split())[:160]
            lines.append(f"step {m['_step']}: {name}({args[:160]}) -> {result}")
    log = "\n".join(lines)
    return log if len(log) <= 8000 else log[:3000] + "\n...\n" + log[-5000:]


def write_lessons(history, outcome):
    print("\nasking the model what it would tell its next run...")
    msg = call_model(
        [{"role": "system", "content": LESSON_PROMPT}, {"role": "user", "content": f"outcome: {outcome}\n\n{run_log(history)}"}],
        2, tools=False,
    )
    text = msg.get("content") or ""
    bullet = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
    lessons = [bullet.sub("", line).strip()[:200] for line in text.splitlines() if bullet.match(line)]
    lessons = [lesson for lesson in lessons if lesson][:MAX_NEW_LESSONS]
    kept, dropped = grounded(lessons, history)
    for lesson in dropped:
        print(f"  dropped, it names a site this run never touched: {lesson}")
    return kept


def grounded(lessons, history):
    # The first real run of this lab stopped after 4 steps and the model still "remembered" three
    # sites it had never opened. A lesson like that gets trusted by every run after it, so any
    # lesson naming a domain has to find that domain somewhere in what this run actually did.
    seen = " ".join(
        (m.get("content") or "") + " " + " ".join(c["function"].get("arguments") or "" for c in m.get("tool_calls") or [])
        for m in history[2:]
    ).lower()
    kept, dropped = [], []
    for lesson in lessons:
        (kept if all(d.lower() in seen for d in DOMAIN.findall(lesson)) else dropped).append(lesson)
    return kept, dropped


def save_result(policy, max_steps, outcome, steps):
    new_pdfs = len(memory.new_this_run(WORKSPACE))
    total_pdfs = sum(1 for p in WORKSPACE.rglob("*.pdf") if is_real_pdf(p))
    new_file = not RESULTS.exists()
    with RESULTS.open("a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["when", "run", "model", "context", "outcome", "steps", "max_steps", "tokens_in", "tokens_out", "new_pdfs", "total_pdfs"])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"), memory.run, os.environ["LLM_MODEL"], policy, outcome,
            steps, max_steps, usage_total["in"], usage_total["out"], new_pdfs, total_pdfs,
        ])
    print(f"added run {memory.run} to {RESULTS.name}")


def selftest():
    """Check the context policies and the memory offline, with made-up histories and temp folders."""
    global WORKSPACE, memory
    saved = (WORKSPACE, memory)

    def pairs_ok(msgs):
        waiting = set()
        for m in msgs:
            if m["role"] == "tool":
                if m["tool_call_id"] not in waiting:
                    return False
                waiting.discard(m["tool_call_id"])
            elif waiting:
                return False
            else:
                waiting = {c["id"] for c in m.get("tool_calls") or []}
        return not waiting

    def ask(*ids):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": i, "type": "function", "function": {"name": "x", "arguments": "{}"}} for i in ids]}

    def page_read(i, step):
        return {"role": "tool", "tool_call_id": i, "content": "page text " * 200, "_tool": "read_page", "_step": step, "_bulky": True, "_url": f"https://site/{step}", "_title": f"page {step}", "_query": None}

    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    history += [ask("s1"), {"role": "tool", "tool_call_id": "s1", "content": "results " * 100, "_tool": "web_search", "_step": 1, "_bulky": True, "_url": "", "_title": "", "_query": "maths 1a"}]
    for step in range(2, 6):
        history += [ask(f"r{step}"), page_read(f"r{step}", step)]
    download = {"role": "tool", "tool_call_id": "d6", "content": "saved workspace/2024/a.pdf (1.1 MB)", "_tool": "download_pdf", "_step": 6}
    history += [ask("d6", "r6"), download, page_read("r6", 6)]
    history += [ask("e7"), {"role": "tool", "tool_call_id": "e7", "content": "error: TimeoutError", "_tool": "click", "_step": 7}]

    checks = {}
    for name, policy in POLICIES.items():
        sent = policy(history)
        checks[f"{name}: tool calls still paired"] = pairs_ok(sent)
        checks[f"{name}: no bookkeeping keys sent"] = not any(k.startswith("_") for m in sent for k in m)
    checks["window: backed up to include the assistant message"] = window(history)[2]["role"] == "assistant"
    contents = [m["content"] for m in stubs(history)]
    checks["stubs: old page stubbed, last two reads kept"] = contents[5].startswith("[read https://site/2") and contents[14] == history[14]["content"]

    fixture = "1K9bPUQkPumCW3pWlw7jc4OUlQlQsZJKQ"
    drive_urls = {
        "/view": f"https://drive.google.com/file/d/{fixture}/view",
        "/preview": f"https://drive.google.com/file/d/{fixture}/preview",
        "open?id=": f"https://drive.google.com/open?id={fixture}",
        "uc?id=": f"https://drive.google.com/uc?id={fixture}&export=download",
        "uc?export&id=": f"https://drive.google.com/uc?export=download&id={fixture}",
        "inside manabadi's DocUrl": "https://www.manabadi.co.in/QP/Question-Papers.aspx?DocTypeId=17362&Title=TS-Inter-1st-Year-Maths-1A-Mar-2024-QP"
                                    f"&SyllabusData=1st%20Year&DocUrl=http://manabadi.co.in/institute/https://drive.google.com/file/d/{fixture}/preview",
        "percent-encoded in a query": f"https://example.com/view?src=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2F{fixture}%2Fpreview",
    }
    for shape, url in drive_urls.items():
        checks[f"drive id from {shape}"] = drive_id(url) == fixture
    checks["drive id: a plain pdf link isn't mistaken for one"] = drive_id("https://www.adda247.com/jobs/wp-content/uploads/x.pdf") is None

    tmp = Path(tempfile.mkdtemp())
    WORKSPACE = tmp / "workspace"
    fake_pdf = b"%PDF-1.4\n" + b"0" * 12_000

    memory = Memory(tmp / "memory")
    checks["memory: empty memory gives no prompt block"] = memory.prompt_block() == ""
    viewer = "https://docs.aglasem.com/view/e16f8fd4?_gl=abc"
    memory.record_refusal(viewer, "web page, not a PDF")
    checks["block: one refusal isn't enough"] = memory.blocked("https://docs.aglasem.com/view/other") is None
    memory.record_refusal(viewer, "web page, not a PDF")
    checks["block: two refusals block the pattern"] = memory.blocked("https://docs.aglasem.com/view/other") is not None
    checks["block: a different path on the same site isn't blocked"] = memory.blocked("https://docs.aglasem.com/files/a.pdf") is None
    for _ in range(2):
        memory.record_refusal("https://example.com/files/page.html", "web page, not a PDF")
    memory.record_download("https://example.com/files/real.pdf", "2022/real.pdf", "2022", "https://example.com/list")
    checks["block: a pattern that once gave a real PDF is never blocked"] = memory.blocked("https://example.com/files/other.pdf") is None
    checks["block: download_pdf refuses a blocked url without fetching"] = download_pdf(viewer, "x.pdf", "2024").startswith("refused without trying")

    (WORKSPACE / "2023").mkdir(parents=True)
    (WORKSPACE / "2023" / "old.pdf").write_bytes(fake_pdf)
    memory.record_download("https://www.adda247.com/jobs/wp-content/old.pdf", "2023/old.pdf", "2023", "https://www.adda247.com/list")
    memory.add_lessons(["adda247 wp-content upload links are direct PDFs"])
    memory.finish_run()
    memory.save()

    memory = Memory(tmp / "memory")
    block = memory.prompt_block()
    checks["memory: run number went up"] = memory.run == 2
    checks["memory: block lists the downloaded year"] = "- 2023: 2023/old.pdf" in block
    checks["memory: block marks the viewer pattern as skipped"] = "docs.aglasem.com/view: refused 2 time(s), download_pdf will skip it" in block
    checks["memory: block has the lesson"] = "adda247 wp-content upload links are direct PDFs" in block
    checks["verify: an old pdf alone doesn't pass"] = "earlier runs" in (verify_downloads(None) or "")
    (WORKSPACE / "2024").mkdir()
    (WORKSPACE / "2024" / "new.pdf").write_bytes(fake_pdf)
    memory.record_download("https://drive.google.com/uc?export=download&id=x", "2024/new.pdf", "2024", "https://www.manabadi.co.in/x")
    checks["verify: a pdf saved this run passes"] = verify_downloads(None) is None
    checks["verify: only this run's pdf counts as new"] = [p.name for p in memory.new_this_run(WORKSPACE)] == ["new.pdf"]

    memory.notes = []
    memory.add_lessons(["Adda247 wp-content upload links are direct PDFs.", "adda247 wp-content upload links are direct PDFs"])
    checks["notes: near-identical lessons collapse"] = len(memory.notes) == 1
    memory.add_lessons(["the 2023 paper is on sakshi", "the 2024 paper is on sakshi"])
    checks["notes: same wording, different year, both kept"] = len(memory.notes) == 3
    memory.add_lessons([f"site {i} has the {1990 + i} paper at /qp/{i * 37}" for i in range(25)])
    checks[f"notes: capped at {MAX_NOTES}, oldest dropped"] = len(memory.notes) == MAX_NOTES and memory.notes[0][1].startswith("site 5 ")

    log = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
           ask("g1"), {"role": "tool", "tool_call_id": "g1", "content": "now at https://schools.aglasem.com/ts-class-11 (papers)", "_tool": "goto", "_step": 1}]
    kept, dropped = grounded(["aglasem.com pages are viewers", "tsbie.cgg.gov.in has year-wise PDFs", "use year-specific searches"], log)
    checks["lessons: one naming a site the run never touched is dropped"] = dropped == ["tsbie.cgg.gov.in has year-wise PDFs"]
    checks["lessons: grounded and site-free lessons are kept"] = kept == ["aglasem.com pages are viewers", "use year-specific searches"]

    checks.update(not_found_checks(memory))
    memory.record_not_found("2022", "https://a.example/y", "page broken")
    memory.record_not_found("2022", "https://a.example/z", "page broken")
    memory.record_not_found("2022", "https://b.example/y", "no 1A")
    checks["unavailable: two sites aren't enough"] = 2022 not in memory.unavailable_years()
    memory.record_not_found("2022", "https://c.example/y", "no 1A")
    checks[f"unavailable: {NOT_FOUND_SOURCES} different sites make it count"] = 2022 in memory.unavailable_years()
    checks["unavailable: memory block tells the next run"] = "2022: https://c.example/y" in memory.prompt_block()

    shutil.rmtree(tmp)
    WORKSPACE, memory = saved
    return checks


def not_found_checks(mem):
    """Serve a few fake paper pages locally and make sure the not-found checker reads them right."""
    import http.server
    import threading

    pages = {
        "/2023": (200, "<h2>TS Inter 1st Year 2023</h2><a href='/qp?Title=TS-Inter-Maths-1B(EM)-2023-Mar-Paper-I-QP'>Maths 1B</a>"),
        "/2024": (200, "<h2>2024</h2><div>TS Inter 1st Year Maths 1A Mar 2024 QP</div>"),
        "/2020": (200, "<h2>2020</h2><a href='/qp?Title=TS-Inter-1st-year-Maths-I%28A%29-2020-March-Exam-Paper'>paper</a>"),
        "/other-year": (200, "<h2>2024 papers</h2><div>Physics</div>"),
        "/broken": (500, "Runtime Error"),
        "/forbidden": (403, "no"),
    }

    class Pages(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = pages.get(self.path.split("?")[0], (404, "nope"))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    checks = {
        "not found: a page listing only Maths 1B confirms 2023": check_not_found("2023", f"{base}/2023")[0],
        "not found: 'Maths 1A' on the page is rejected": not check_not_found("2024", f"{base}/2024")[0],
        "not found: an encoded 'Maths-I(A)' is rejected": not check_not_found("2020", f"{base}/2020")[0],
        "not found: a page that never mentions the year is rejected": not check_not_found("2019", f"{base}/other-year")[0],
        "not found: a 500 page confirms": check_not_found("2022", f"{base}/broken")[0],
        "not found: a 403 can't be checked, so it doesn't count": not check_not_found("2022", f"{base}/forbidden")[0],
        "not found: the tool records a confirmed page": report_not_found("2023", f"{base}/2023", "only 1B").startswith("confirmed")
                                                       and "2023" in mem.facts["unavailable"],
        "not found: the tool tells the model to look again": "Look again" in report_not_found("2024", f"{base}/2024", "nothing here"),
    }
    server.shutdown()
    return checks


def run_once(targets=TARGET_YEARS, max_steps=25, context="stubs", headless=True, quiet_tree=False):
    """One full run of the agent. Returns a dict describing what it did, for the loop to judge."""
    global page, memory
    for d in (last_call, last_read):
        d.clear()
    usage_total.update({"in": 0, "out": 0, "calls": 0})

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    memory = Memory(MEMORY_DIR)
    missing = sorted(set(targets) - memory.have_years(WORKSPACE) - memory.unavailable_years(), reverse=True)
    task = build_task(missing)
    print(f"run {memory.run}\ntask: {task}\nmodel: {os.environ['LLM_MODEL']}\ncontext: {context}\ntools: {', '.join(TOOLS)}")

    block = memory.prompt_block()
    print("\n" + ("what the model is told about earlier runs:\n" + block if block else "memory is empty, so this run starts from nothing."))
    history = [{"role": "system", "content": SYSTEM_PROMPT + ("\n\n" + block if block else "")}, {"role": "user", "content": task}]

    outcome, steps = "crashed", 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=0 if headless else 300)
        page = browser.new_page()
        try:
            outcome, steps = run(history, context, max_steps)
        finally:
            browser.close()
            page = None
            if not quiet_tree:
                print_tree()
            # Facts are saved even if the run crashed, since they only record what really happened.
            memory.finish_run()
            memory.save()

    lessons = []
    if len(history) > 2:
        try:
            lessons = write_lessons(history, outcome)
            memory.add_lessons(lessons)
            memory.save()
            print("lessons for next time:\n" + ("\n".join(f"  - {l}" for l in lessons) if lessons else "  (none)"))
        except (Exception, SystemExit) as e:
            print(f"couldn't get lessons this run ({type(e).__name__}), the facts are saved anyway.")

    print(f"\ncontext: {context}. tokens: {usage_total['in']} in, {usage_total['out']} out over {usage_total['calls']} calls.")
    print(f"memory now holds {memory.fact_count()} facts, {len(memory.notes)} lessons.")
    save_result(context, max_steps, outcome, steps)
    return {
        "run": memory.run,
        "task_years": missing,
        "outcome": outcome,
        "steps": steps,
        "tokens_in": usage_total["in"],
        "tokens_out": usage_total["out"],
        "new_pdfs": [str(p.relative_to(WORKSPACE)) for p in memory.new_this_run(WORKSPACE)],
        "new_unavailable": memory.new_unavailable_this_run(),
        "visited": sorted(memory.visited),
        "events": list(memory.events),
        "lessons": lessons,
    }


def main():
    parser = argparse.ArgumentParser(description="lab 07: one run of the harness (loop.py runs it repeatedly)")
    parser.add_argument("--context", choices=POLICIES, default="stubs", help="what gets sent to the model each step (default stubs)")
    parser.add_argument("--steps", type=int, default=25, help="max agent steps (default 25)")
    parser.add_argument("--watch", action="store_true", help="show the browser window")
    parser.add_argument("--selftest", action="store_true", help="check memory, context handling and the not-found checker offline")
    args = parser.parse_args()
    if args.selftest:
        checks = selftest()
        for check, ok in checks.items():
            print(f"{'ok  ' if ok else 'FAIL'} {check}")
        print(f"\n{sum(checks.values())}/{len(checks)} checks passed")
        sys.exit(0 if all(checks.values()) else 1)

    load_env()
    run_once(max_steps=args.steps, context=args.context, headless=not args.watch)


if __name__ == "__main__":
    main()
