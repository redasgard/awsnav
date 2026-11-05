"""Widgets and modal dialogs shared by several screens."""

from __future__ import annotations

from typing import Any, Callable

from rich.markup import escape
from rich.syntax import Syntax
from rich.style import Style
from rich.text import Span, Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from awsnav.formatting import to_json


def _highlight(code: str, lexer: str) -> Text:
    """Syntax-highlighted text with token backgrounds removed, so it blends with any theme."""
    text = Syntax(code, lexer, theme="monokai", word_wrap=True).highlight(code)
    text.style = ""
    spans = []
    for span in text.spans:
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        spans.append(Span(span.start, span.end, Style(color=style.color, bold=style.bold, italic=style.italic)))
    text.spans = spans
    return text


def json_renderable(value: Any) -> Text:
    return _highlight(value if isinstance(value, str) else to_json(value), "json")


def text_renderable(text: str, name: str = "") -> Text:
    try:
        lexer = Syntax.guess_lexer(name or "file.txt", text)
    except Exception:  # noqa: BLE001
        lexer = "text"
    return _highlight(text, lexer)


def error_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and "Error" in response:
        err = response["Error"]
        return f"{err.get('Code', type(exc).__name__)}: {err.get('Message', str(exc))}"
    return f"{type(exc).__name__}: {exc}"


class TopBar(Static):
    """Title line: app name, identity, region and read/write mode."""


class Crumbs(Static):
    """One-line breadcrumb shown under the top bar."""


class StatusLine(Static):
    def ok(self, message: str) -> None:
        self.set_classes("status")
        self.update(message)

    def error(self, message: str) -> None:
        self.set_classes("status error")
        self.update(message)


class BaseScreen(Screen):
    """Common chrome: header, breadcrumb, footer and safe thread callbacks."""

    def crumbs_text(self) -> str:
        return ""

    def refresh_crumbs(self) -> None:
        header_text = getattr(self.app, "header_text", None)
        for bar in self.query(TopBar):
            bar.update(f"[b]awsnav[/b]  {escape(header_text())}" if callable(header_text) else "awsnav")
        try:
            crumbs = self.query_one(Crumbs)
        except Exception:  # noqa: BLE001
            return
        crumbs.update(f"[b]{self.display_region()}[/b] › {self.crumbs_text()}")

    def display_region(self) -> str:
        """Region this screen works in: its own pinned region, else the app's current one."""
        pinned = getattr(self, "pinned_region", None)
        if pinned:
            return pinned
        ctx = getattr(self.app, "ctx", None)
        return ctx.region if ctx else "-"

    def on_screen_resume(self) -> None:
        self.refresh_crumbs()

    def from_thread(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run ``fn`` on the UI thread if this screen is still on the stack."""

        def guarded() -> None:
            if self.is_attached:
                fn(*args)

        try:
            self.app.call_from_thread(guarded)
        except RuntimeError:
            pass


class TableScreen(BaseScreen):
    """A filterable table of rows, each carrying an arbitrary payload."""

    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "back", "Back"),
        Binding("down", "focus_table", show=False),
    ]
    can_go_back = True
    focus_filter_first = False

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[tuple[str, list[Any], str, Any]] = []
        self._columns: list[str] = []

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        yield Input(placeholder="Type to filter…  (/ to focus, Esc to clear)", id="filter")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield StatusLine(classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        if self.focus_filter_first:
            self.query_one("#filter", Input).focus()
        else:
            self.query_one(DataTable).focus()

    @property
    def table(self) -> DataTable:
        return self.query_one("#table", DataTable)

    @property
    def status(self) -> StatusLine:
        return self.query_one(StatusLine)

    def set_rows(self, columns: list[str], rows: list[tuple[list[Any], str, Any]]) -> None:
        """``rows`` are ``(cells, search_text, payload)``."""
        self._columns = columns
        self._rows = [(str(i), cells, search.lower(), payload) for i, (cells, search, payload) in enumerate(rows)]
        self._render_rows()
        flt = self.query_one("#filter", Input)
        if not self.focus_filter_first and not flt.value and self.focused in (None, flt):
            # The loading overlay makes the table unfocusable; give focus back once data is in.
            self.table.focus()

    def append_rows(self, rows: list[tuple[list[Any], str, Any]]) -> None:
        """Add rows without redrawing the table (used while a scan streams results)."""
        flt = self.query_one("#filter", Input).value.strip().lower().split()
        table = self.table
        start = len(self._rows)
        for offset, (cells, search, payload) in enumerate(rows):
            key = str(start + offset)
            self._rows.append((key, cells, search.lower(), payload))
            if all(t in search.lower() for t in flt):
                table.add_row(*(Text(str(c)) for c in cells), key=key)

    def _render_rows(self) -> None:
        table = self.table
        query = self.query_one("#filter", Input).value.strip().lower()
        table.clear(columns=True)
        table.add_columns(*self._columns)
        terms = query.split()
        matches = [r for r in self._rows if all(t in r[2] for t in terms)]
        if query:
            # Exact cell matches first, then cells starting with the query, then the rest.
            def rank(row: tuple[str, list[Any], str, Any]) -> int:
                cells = [str(c).strip().lower() for c in row[1]]
                if query in cells:
                    return 0
                if any(c.startswith(query) for c in cells):
                    return 1
                return 2

            matches.sort(key=rank)
        for key, cells, _search, _payload in matches:
            # Text cells are never interpreted as markup, so AWS data containing [brackets] is safe.
            table.add_row(*(Text(str(c)) for c in cells), key=key)
        self.on_rows_rendered(len(matches), len(self._rows))

    def on_rows_rendered(self, shown: int, total: int) -> None:
        pass

    def payload_for(self, key: str | None) -> Any:
        if key is None:
            return None
        idx = int(key)
        return self._rows[idx][3] if 0 <= idx < len(self._rows) else None

    def highlighted_payload(self) -> Any:
        table = self.table
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None
        return self.payload_for(key)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._render_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            payload = self.highlighted_payload()
            if payload is not None:
                self.open_payload(payload)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        payload = self.payload_for(event.row_key.value)
        if payload is not None:
            self.open_payload(payload)

    def open_payload(self, payload: Any) -> None:
        raise NotImplementedError

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_table(self) -> None:
        self.table.focus()

    def action_back(self) -> None:
        flt = self.query_one("#filter", Input)
        if flt.value:
            flt.value = ""
            self.table.focus()
            return
        if self.can_go_back and len(self.app.screen_stack) > 2:
            self.app.pop_screen()


class TextViewer(BaseScreen):
    """Full-screen scrollable text / JSON viewer."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("y", "copy", "Copy"),
        Binding("w", "save", "Save to file"),
    ]

    def __init__(self, title: str, content: Any, *, filename: str = "", is_json: bool = True) -> None:
        super().__init__()
        self._title = title
        self._content = content
        self._filename = filename
        self._is_json = is_json

    def crumbs_text(self) -> str:
        return self._title

    def _text(self) -> str:
        return self._content if isinstance(self._content, str) else to_json(self._content)

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        renderable = json_renderable(self._content) if self._is_json else text_renderable(self._text(), self._filename)
        with VerticalScroll(id="viewer"):
            yield Static(renderable)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        self.query_one("#viewer").focus()

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self._text())
        self.notify("Copied to clipboard (terminal must support OSC 52)")

    def action_save(self) -> None:
        default = self._filename.rsplit("/", 1)[-1] if self._filename else "output.json"
        save_text_dialog(self, self._text(), default)


def save_text_dialog(screen: Screen, text: str, default_name: str) -> None:
    """Ask for a path and write ``text`` to it."""

    def done(path: str | None) -> None:
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            screen.notify(str(exc), severity="error")
        else:
            screen.notify(f"Saved {path}")

    screen.app.push_screen(PromptModal("Save to file", default_name), done)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str, confirm_label: str = "Run") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(self._message)
            with Horizontal(classes="buttons"):
                yield Button(self._confirm, variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class PromptModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, default: str = "", password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._password = password

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Input(value=self._default, password=self._password, id="prompt")
            with Horizontal(classes="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#prompt", Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PickerModal(ModalScreen[str | None]):
    """Filterable list of choices; typing a value not in the list is allowed if ``free_text``."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("down", "focus_list", show=False)]

    def __init__(self, title: str, choices: list[tuple[str, str]], current: str | None = None, free_text: bool = False) -> None:
        super().__init__()
        self._title = title
        self._choices = choices  # (value, label)
        self._current = current
        self._free_text = free_text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog tall"):
            yield Label(self._title, classes="dialog-title")
            yield Input(placeholder="Filter…", id="picker-filter")
            yield OptionList(id="picker-list")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#picker-filter", Input).focus()

    def _fill(self, query: str) -> None:
        olist = self.query_one("#picker-list", OptionList)
        olist.clear_options()
        terms = query.lower().split()
        options = []
        highlight = 0
        for value, label in self._choices:
            hay = f"{value} {label}".lower()
            if terms and not all(t in hay for t in terms):
                continue
            if value == self._current:
                highlight = len(options)
            options.append(Option(label, id=value))
        olist.add_options(options)
        if options:
            olist.highlighted = highlight

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        olist = self.query_one("#picker-list", OptionList)
        values = [v for v, _ in self._choices]
        if self._free_text and event.value.strip() and event.value.strip() not in values and olist.option_count == 0:
            self.dismiss(event.value.strip())
            return
        if olist.highlighted is not None and olist.option_count:
            self.dismiss(olist.get_option_at_index(olist.highlighted).id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_focus_list(self) -> None:
        self.query_one("#picker-list", OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """\
[b]Navigation[/b]
  Enter         open the selected service / resource / action
  Esc           clear the filter, then go back one level
  /             filter the current list (all words must match)
  ↑ ↓ PgUp PgDn move;  Tab moves between panes
  Ctrl+P        command palette: jump to any service by name

[b]Everywhere[/b]
  Ctrl+G        change region
  Ctrl+B        Bedrock chat playground (Converse API)
  F2            scan all / selected regions for every active resource
  F1 / ?        this help
  Ctrl+Q        quit

[b]Operations list (a service)[/b]
  a             show every API operation, not only resource listings
  x             open the operation runner for the highlighted operation

[b]Resource list[/b]
  Enter         details + related operations for the resource
  r             refresh          v   view raw response JSON
  e             edit the request parameters and run again
  x             run any operation, prefilled from the highlighted resource

[b]Details[/b]
  Enter         run the selected related operation / action
  x             run any operation prefilled from this resource
  y             copy JSON        w   save JSON to a file
  r             reload (when the data came from a Get/Describe call)

[b]Scan results[/b]
  Enter         details of the resource (calls run in that resource's region)
  s             summary by region and type     E   failed calls (permissions etc.)
  w             export to .json or .csv        r   scan again
  Esc           stop a running scan, then go back

[b]Operation runner[/b]
  Ctrl+R / F5   run.  Parameters are JSON, as in the AWS API reference.
  Write operations (Create/Delete/Put/…) need --allow-write and confirmation.
"""


class HelpModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("f1", "close", show=False), Binding("question_mark", "close", show=False)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog tall"):
            yield Label("awsnav — keys", classes="dialog-title")
            yield Static(HELP_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)
