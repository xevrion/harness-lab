# 04: the tool registry

This is the lab that does what got me into all of this: a friend's agent went and collected every past paper he needed, sorted into year folders, while he did something else. Here the default task is the same idea, find TS Inter 1st year Maths 1A past papers and download them.

The harness is lab 03's, with two changes.

## Tools are registered, not hand-wired

In lab 03 every tool was a function plus a hand-written block of JSON schema, and the two could drift apart. Here a tool is just a function with a decorator on it:

```python
@tool(
    "Download a PDF from a URL into the workspace. Checks it's really a PDF before saving. ...",
    url=("string", "the PDF's URL, usually from the pdf links in read_page"),
    filename=("string", "a short descriptive name, e.g. maths-1a-march-2024.pdf"),
    folder=("string", "optional subfolder, e.g. the year"),
)
def download_pdf(url, filename, folder=None):
```

The decorator builds the schema the model sees, and works out which arguments are required from the function's own signature (`folder` has a default, so it's optional). Adding a tool is one decorated function and nothing else.

The part that took me a while to get: the description is prompt text. It's the only thing the model knows about the tool, so writing it is closer to writing instructions for a person than writing a docstring. `goto`'s description says not to use it for PDFs because in testing that's exactly what a model tried first.

## The tool that touches your disk has the most rules

`download_pdf` is the first tool here that writes real files from the internet, so it's where the guardrails live, all of them in plain code:

- only `http` and `https`, so no `file:///etc/passwd`
- the filename and folder get cleaned, and the final path has to be inside `workspace/`, so `../../evil.pdf` just lands as `workspace/evil.pdf`
- the first bytes have to be `%PDF`. Paper sites love serving an HTML page full of ads at a URL ending in `.pdf`, and they lie in the Content-Type header too, so the bytes decide
- 25 MB cap, counted while downloading rather than trusted from the headers (the official Maths 1A model paper is 23 MB, so it's not a made up number)
- if the file's already there it says so instead of downloading it again
- if a site refuses a plain download it retries through the browser, which has that site's cookies
- a Google Drive `/view` link gets turned into Drive's direct download link. The first real run spent its last steps handing the tool Drive viewer pages, which the `%PDF` check rightly refused, so now the harness knows the trick instead of hoping the model does

None of these are in the prompt. The model can't talk its way past any of them, same as `MAX_STEPS` back in lab 01.

There's also a `web_search` tool, using the `ddgs` package. I first tried pointing the browser at search engines and every one of them (Bing, DuckDuckGo, Brave, Startpage, Mojeek) either blocked it or quietly returned junk. The friend whose agent started all this had `search: ddgs` in his status bar, and now I know why.

## Running

Same setup as lab 03 (a `.venv` in the repo root and a `.env`), plus the new dependency:

```
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python 04-tool-registry/harness.py
```

At the end it prints the workspace like a folder listing, marks each file as a real PDF or not, and shows the total tokens used. Custom tasks work too, but the verifier only knows how to check that PDFs were really downloaded, so give it something to download:

```
.venv/bin/python 04-tool-registry/harness.py "download the NCERT class 11 physics part 1 chapter 1 pdf"
```

## Watch the token count

This one is left broken on purpose. Every `read_page` on a paper site dumps a long list of PDF links into the conversation, and since the whole conversation is sent again on every step, the tokens on the way in climb fast. The total at the end is mostly the same old pages being re-sent over and over. Fixing that is context management, and it's the next lab.

## Things to try

1. Run the default task in a visible browser and watch which sites it picks.
2. Make `download_pdf`'s description vague, just "download a file", and see if the model still uses the year folders.
3. Delete the `%PDF` check and point it at a site that serves HTML for its "PDF" links. Count how many junk files you end up with.
4. Set `MAX_PDF_BYTES` to 1 MB and see how the model reacts when a download is refused.
