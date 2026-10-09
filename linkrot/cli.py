"""
cli.py — `linkrot`, the command-line interface.

    linkrot README.md                    # check every link in a file
    linkrot notes.md bookmarks.html      # several files at once
    linkrot https://example.com/a        # check URLs given directly
    cat links.txt | linkrot              # read from stdin
    linkrot README.md --only-bad --json  # machine-readable, problems only

EXIT CODES (so it can gate a CI pipeline)
-------------------------------------------
    0  no broken links
    1  at least one BROKEN or ERROR link
    2  bad usage / unreadable input
With --strict, REDIRECT and BLOCKED results also count as failures.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict

from .checker import Result, Status, check_all
from .extract import extract_urls

_COLORS = {
    Status.OK: "\033[32m",        # green
    Status.REDIRECT: "\033[36m",  # cyan
    Status.BLOCKED: "\033[33m",   # yellow
    Status.BROKEN: "\033[31m",    # red
    Status.ERROR: "\033[31m",     # red
}
_RESET = "\033[0m"

# Worst first, so problems are at the top of the report.
_SEVERITY = {Status.BROKEN: 0, Status.ERROR: 1, Status.BLOCKED: 2, Status.REDIRECT: 3, Status.OK: 4}


def _gather_urls(sources: list[str]) -> list[str]:
    """Each source is a URL, a file path, or '-' for stdin."""
    urls: list[str] = []
    for src in sources:
        if src.lower().startswith(("http://", "https://")):
            urls.append(src)
        elif src == "-":
            urls.extend(extract_urls(sys.stdin.read()))
        else:
            try:
                with open(src, encoding="utf-8", errors="replace") as f:
                    urls.extend(extract_urls(f.read()))
            except OSError as e:
                print(f"linkrot: can't read {src}: {e.strerror}", file=sys.stderr)
                raise SystemExit(2)
    # Dedupe across all sources, preserving order.
    return list(dict.fromkeys(urls))


def _format_row(r: Result, color: bool) -> str:
    label = r.status.value.upper()
    code = str(r.code) if r.code else "---"
    if color:
        label = f"{_COLORS[r.status]}{label:<8}{_RESET}"
    else:
        label = f"{label:<8}"
    line = f"{label} {code:>3}  {r.url}"
    if r.detail:
        line += f"  ({r.detail})"
    if r.final_url:
        line += f"\n{'':14}-> {r.final_url}"
    return line


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="linkrot",
        description="Find dead, moved, and blocked links in files or lists of URLs.",
    )
    p.add_argument("sources", nargs="*", help="files, URLs, or '-' for stdin")
    p.add_argument("--timeout", type=float, default=10.0, help="seconds per request (default 10)")
    p.add_argument("--concurrency", type=int, default=20, help="max simultaneous requests (default 20)")
    p.add_argument("--per-host", type=int, default=3, help="max simultaneous requests per site (default 3)")
    p.add_argument("--only-bad", action="store_true", help="hide OK results")
    p.add_argument("--json", action="store_true", help="output JSON instead of a table")
    p.add_argument("--strict", action="store_true", help="exit 1 on REDIRECT/BLOCKED too")
    p.add_argument("--no-color", action="store_true", help="disable colored output")
    return p


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    sources = args.sources
    if not sources:
        if sys.stdin.isatty():
            build_parser().print_usage(sys.stderr)
            print("linkrot: give me files/URLs, or pipe text in", file=sys.stderr)
            return 2
        sources = ["-"]

    urls = _gather_urls(sources)
    if not urls:
        print("linkrot: no URLs found", file=sys.stderr)
        return 2

    color = (
        not args.no_color
        and not args.json
        and sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
    )

    # Live progress on stderr (so it never pollutes piped/JSON stdout).
    done = 0
    show_progress = sys.stderr.isatty() and not args.json

    def on_result(_r: Result) -> None:
        nonlocal done
        done += 1
        if show_progress:
            print(f"\rchecked {done}/{len(urls)}", end="", file=sys.stderr, flush=True)

    try:
        results = asyncio.run(
            check_all(
                urls,
                concurrency=args.concurrency,
                per_host=args.per_host,
                timeout=args.timeout,
                on_result=on_result,
            )
        )
    except KeyboardInterrupt:
        print("\nlinkrot: interrupted", file=sys.stderr)
        return 130
    if show_progress:
        print("\r" + " " * 30 + "\r", end="", file=sys.stderr)

    results.sort(key=lambda r: _SEVERITY[r.status])
    shown = [r for r in results if not (args.only_bad and r.status == Status.OK)]

    if args.json:
        print(json.dumps([{**asdict(r), "status": r.status.value} for r in shown], indent=2))
    else:
        for r in shown:
            print(_format_row(r, color))
        counts = {s: sum(1 for r in results if r.status == s) for s in Status}
        summary = ", ".join(f"{n} {s.value}" for s, n in counts.items() if n)
        print(f"\n{len(results)} links checked: {summary}")

    failing = {Status.BROKEN, Status.ERROR}
    if args.strict:
        failing |= {Status.REDIRECT, Status.BLOCKED}
    return 1 if any(r.status in failing for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except BrokenPipeError:
        # `linkrot ... | head` closes the pipe early. That's the reader's
        # choice, not an error: silence Python's shutdown flush complaint
        # by pointing stdout at /dev/null, then exit quietly.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
