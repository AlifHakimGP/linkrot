"""
extract.py — pull URLs out of messy text (markdown, HTML, plain notes).

THE REAL PROBLEM HERE
-----------------------
"Find the URLs in this text" sounds trivial and isn't. A URL has no
clear end marker, and the text around it is full of punctuation that
*looks* like part of it:

    See https://example.com/page.            <- trailing "." is a sentence end
    [docs](https://example.com/a_(b))        <- the final ")" closes the markdown link
    https://en.wikipedia.org/wiki/Foo_(bar)  <- but THIS ")" is part of the URL

So we match greedily, then trim: strip trailing punctuation, and only
strip a trailing ")" if it's *unbalanced* (more closing than opening
parentheses in the URL), which handles both the markdown and Wikipedia
cases correctly.
"""

import re

# Stop at whitespace and characters that can never appear raw in a URL
# embedded in text/markup: < > " ' ` and the markdown/bracket closers.
_URL_RE = re.compile(r"""https?://[^\s<>"'`\]\[]+""", re.IGNORECASE)

_TRAILING_PUNCT = ".,;:!?*_~"


def _clean(url: str) -> str:
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
        elif last == ")" and url.count(")") > url.count("("):
            url = url[:-1]  # unbalanced -> belongs to surrounding text
        else:
            break
    return url


def extract_urls(text: str) -> list[str]:
    """Return unique URLs in order of first appearance."""
    seen = set()
    urls = []
    for match in _URL_RE.finditer(text):
        url = _clean(match.group(0))
        # Drop fragments-only differences? No: keep as written, but dedupe
        # exact duplicates so one dead link repeated 20 times is one row.
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls
