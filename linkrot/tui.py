"""
tui.py — `linkrot-tui`, the interactive terminal interface.

Paste any text containing links (a README, a bookmarks export, loose
notes) or load a file, press Ctrl+R, and watch results stream in live.

It drives the exact same `check_all()` as the CLI. The `on_result`
callback is what makes live updates possible: check_all calls it the
moment each individual link finishes, and we use it to append a table row
and advance the progress bar — so a 500-link check shows steady progress
instead of a frozen screen followed by a wall of output.
"""

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    TextArea,
)

from .checker import Result, Status, check_all
from .extract import extract_urls

_STYLES = {
    Status.OK: "green",
    Status.REDIRECT: "cyan",
    Status.BLOCKED: "yellow",
    Status.BROKEN: "bold red",
    Status.ERROR: "bold red",
}
_SEVERITY = {Status.BROKEN: 0, Status.ERROR: 1, Status.BLOCKED: 2, Status.REDIRECT: 3, Status.OK: 4}


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


class LinkRotApp(App):
    TITLE = "linkrot"
    SUB_TITLE = "find dead links"

    CSS = """
    #input-area { height: 8; margin: 1 2 0 2; }
    #controls { height: 3; margin: 0 2; }
    #file-input { width: 1fr; }
    #controls Button { margin-left: 1; }
    #only-bad { margin: 0 2; }
    #summary { margin: 0 2; color: $text-muted; height: 1; }
    #progress { margin: 0 2; height: 1; }
    #results { margin: 1 2; height: 1fr; }
    """

    BINDINGS = [
        ("ctrl+r", "run_check", "Check links"),
        ("ctrl+l", "clear", "Clear"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[Result] = []
        # NOTE: deliberately NOT called `_running` -- Textual's own App class
        # uses that name internally to track its event loop, and shadowing
        # it silently breaks things. (Learned the hard way while testing.)
        self._checking = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield TextArea(id="input-area", placeholder="Paste text containing links here…")
        with Horizontal(id="controls"):
            yield Input(placeholder="…or type a file path and press Enter to load it", id="file-input")
            yield Button("Check (Ctrl+R)", id="check-btn", variant="primary")
            yield Button("Clear", id="clear-btn")
        yield Checkbox("Show problems only (hide OK)", id="only-bad")
        yield ProgressBar(id="progress", show_eta=False)
        yield Label("Paste some text, then press Ctrl+R.", id="summary")
        yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#results", DataTable).add_columns("Status", "Code", "URL", "Detail")
        self.query_one("#input-area", TextArea).focus()

    # ---- input handling ------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "file-input":
            return
        path = event.value.strip()
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.query_one("#input-area", TextArea).text = f.read()
            self._set_summary(f"Loaded {path}. Press Ctrl+R to check.")
        except OSError as e:
            self._set_summary(f"[red]Can't read {path}: {e.strerror}[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check-btn":
            self.action_run_check()
        elif event.button.id == "clear-btn":
            self.action_clear()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "only-bad" and not self._checking:
            self._render_table(sort=True)

    def action_clear(self) -> None:
        if self._checking:
            return
        self.query_one("#input-area", TextArea).text = ""
        self._results = []
        self.query_one("#results", DataTable).clear()
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self._set_summary("Paste some text, then press Ctrl+R.")

    def action_run_check(self) -> None:
        if self._checking:
            return
        urls = extract_urls(self.query_one("#input-area", TextArea).text)
        if not urls:
            self._set_summary("[red]No URLs found in the text.[/]")
            return
        self._results = []
        self.query_one("#results", DataTable).clear()
        self.query_one("#progress", ProgressBar).update(total=len(urls), progress=0)
        self._checking = True
        self.query_one("#check-btn", Button).disabled = True
        self._set_summary(f"Checking {len(urls)} links…")
        self._run_checks(urls)

    # ---- the actual work -----------------------------------------------

    @work(exclusive=True)
    async def _run_checks(self, urls: list[str]) -> None:
        def on_result(r: Result) -> None:
            self._results.append(r)
            self.query_one("#progress", ProgressBar).advance(1)
            if not self._hide_ok() or r.status != Status.OK:
                self._add_row(r)
            self._set_summary(self._counts_text(checking=len(urls)))

        try:
            await check_all(urls, on_result=on_result)
        finally:
            self._checking = False
            self.query_one("#check-btn", Button).disabled = False

        # Live rows arrived in completion order; now settle into
        # worst-first order so the problems are at the top.
        self._render_table(sort=True)
        self._set_summary(self._counts_text() + "   (done)")

    # ---- rendering helpers ---------------------------------------------

    def _hide_ok(self) -> bool:
        return self.query_one("#only-bad", Checkbox).value

    def _add_row(self, r: Result) -> None:
        detail = r.detail
        if r.final_url:
            detail += f" → {r.final_url}"
        self.query_one("#results", DataTable).add_row(
            Text(r.status.value.upper(), style=_STYLES[r.status]),
            str(r.code) if r.code else "—",
            _shorten(r.url, 70),
            _shorten(detail, 60),
        )

    def _render_table(self, sort: bool) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        rows = sorted(self._results, key=lambda r: _SEVERITY[r.status]) if sort else self._results
        for r in rows:
            if self._hide_ok() and r.status == Status.OK:
                continue
            self._add_row(r)

    def _counts_text(self, checking: int | None = None) -> str:
        counts = {s: sum(1 for r in self._results if r.status == s) for s in Status}
        parts = [f"{n} {s.value}" for s, n in counts.items() if n]
        prefix = f"{len(self._results)}/{checking} checked — " if checking else f"{len(self._results)} links — "
        return prefix + " · ".join(parts)

    def _set_summary(self, text: str) -> None:
        self.query_one("#summary", Label).update(text)


def main() -> None:
    LinkRotApp().run()


if __name__ == "__main__":
    main()
