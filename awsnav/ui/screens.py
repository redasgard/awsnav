"""Main navigation screens: login → services → resource types → resources → details."""

from __future__ import annotations

import json
from typing import Any

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from awsnav.catalog import ItemContext, is_read_only, operation_prefix, resource_label
from awsnav.formatting import choose_columns, format_cell, row_view, to_json
from awsnav.session import AwsContext, ConnectionSettings, OperationResult
from awsnav.ui.common import (
    BaseScreen,
    ConfirmModal,
    Crumbs,
    PickerModal,
    StatusLine,
    TableScreen,
    TextViewer,
    TopBar,
    error_text,
    json_renderable,
    save_text_dialog,
)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class LoginScreen(BaseScreen):
    BINDINGS = [Binding("ctrl+q", "app.quit", "Quit")]

    def __init__(self, settings: ConnectionSettings, auto_connect: bool = False) -> None:
        super().__init__()
        self.settings = settings
        self.auto_connect = auto_connect

    def compose(self) -> ComposeResult:
        s = self.settings
        yield TopBar(classes="topbar")
        with Center():
            with Vertical(id="login"):
                yield Label("Connect to AWS", classes="dialog-title")
                yield Static(
                    "Enter an IAM access key and secret. Leave both empty to use the default "
                    "credential chain (environment, ~/.aws profiles, SSO, instance role).",
                    classes="hint",
                )
                yield Label("Access key ID")
                yield Input(value=s.access_key or "", placeholder="AKIA…", id="access_key")
                yield Label("Secret access key")
                yield Input(value=s.secret_key or "", password=True, id="secret_key")
                yield Label("Session token (optional, for temporary credentials)")
                yield Input(value=s.session_token or "", password=True, id="session_token")
                yield Label("Region")
                yield Input(value=s.region, id="region")
                yield Label("Endpoint URL (optional, e.g. LocalStack)")
                yield Input(value=s.endpoint_url or "", placeholder="https://…", id="endpoint_url")
                yield Button("Connect", variant="primary", id="connect")
                yield StatusLine(classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        first_empty = next(
            (i for i in self.query(Input) if not i.value and i.id in ("access_key", "secret_key")),
            self.query_one("#access_key", Input),
        )
        first_empty.focus()
        if self.auto_connect:
            self.connect()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect":
            self.connect()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.connect()

    def connect(self) -> None:
        values = {i.id: i.value.strip() for i in self.query(Input)}
        if bool(values["access_key"]) != bool(values["secret_key"]):
            self.query_one(StatusLine).error("Provide both the access key ID and the secret access key.")
            return
        s = self.settings
        s.access_key = values["access_key"] or None
        s.secret_key = values["secret_key"] or None
        s.session_token = values["session_token"] or None
        s.region = values["region"] or "us-east-1"
        s.endpoint_url = values["endpoint_url"] or None
        self.query_one(StatusLine).ok("Verifying credentials with STS…")
        self.query_one("#connect", Button).disabled = True
        self._verify(s)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _verify(self, settings: ConnectionSettings) -> None:
        try:
            ctx = AwsContext(settings)
            ctx.verify()
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self._failed, error_text(exc))
        else:
            self.from_thread(self._connected, ctx)

    def _failed(self, message: str) -> None:
        self.query_one(StatusLine).error(message)
        self.query_one("#connect", Button).disabled = False

    def _connected(self, ctx: AwsContext) -> None:
        self.app.set_context(ctx)
        self.app.switch_screen(ServicesScreen())


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
class ServicesScreen(TableScreen):
    can_go_back = False
    focus_filter_first = True

    def crumbs_text(self) -> str:
        return "Services"

    def on_mount(self) -> None:
        super().on_mount()
        self._populate()
        self._load_names()

    def _populate(self) -> None:
        catalog = self.app.ctx.catalog
        names = catalog.cached_full_names()
        rows = []
        for svc in catalog.ordered_services():
            star = "★" if catalog.is_popular(svc) else ""
            full = names.get(svc, "")
            rows.append(([star, svc, full], f"{svc} {full}", svc))
        self.set_rows(["", "Service", "Name"], rows)

    def on_rows_rendered(self, shown: int, total: int) -> None:
        self.status.ok(f"{shown} of {total} services · Enter to open · F2 scan regions · Ctrl+P to jump · F1 help")

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _load_names(self) -> None:
        try:
            self.app.ctx.catalog.load_full_names()
        except Exception:  # noqa: BLE001
            return
        self.from_thread(self._populate)

    def open_payload(self, payload: Any) -> None:
        self.app.open_service(payload)


# ---------------------------------------------------------------------------
# Resource types (root operations) of a service
# ---------------------------------------------------------------------------
class OperationsScreen(TableScreen):
    BINDINGS = TableScreen.BINDINGS + [
        Binding("a", "toggle_all", "All operations"),
        Binding("x", "runner", "Run…"),
    ]
    focus_filter_first = True

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service
        self.show_all = False

    def crumbs_text(self) -> str:
        catalog = self.app.ctx.catalog
        mode = "all operations" if self.show_all else "resources"
        return f"[b]{self.service}[/b] ({catalog.full_name(self.service)}) › {mode}"

    def on_mount(self) -> None:
        super().on_mount()
        self._populate()

    def _populate(self) -> None:
        catalog = self.app.ctx.catalog
        rows = []
        if self.show_all:
            for op in catalog.all_operations(self.service):
                req = catalog.required_params(self.service, op)
                kind = "read" if is_read_only(op) else "WRITE"
                rows.append(([op, kind, ", ".join(req)], f"{op} {kind}", op))
            self.set_rows(["Operation", "Kind", "Required parameters"], rows)
        else:
            for op in catalog.root_operations(self.service):
                rows.append(([resource_label(op), op], f"{resource_label(op)} {op}", op))
            self.set_rows(["Resource", "Operation"], rows)
        self.refresh_crumbs()

    def on_rows_rendered(self, shown: int, total: int) -> None:
        hint = "a: resources only" if self.show_all else "a: show all API operations"
        self.status.ok(f"{shown} of {total} · Enter to open · x: run with parameters · {hint}")

    def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self._populate()

    def action_runner(self) -> None:
        op = self.highlighted_payload()
        if op:
            self.app.push_screen(RunnerScreen(self.service, op, self.app.ctx.catalog.default_params(self.service, op)))

    def open_payload(self, op: Any) -> None:
        catalog = self.app.ctx.catalog
        if catalog.is_root_operation(self.service, op):
            self.app.push_screen(ResultScreen(self.service, op, catalog.default_params(self.service, op)))
        else:
            params = catalog.params_template(self.service, op)
            self.app.push_screen(RunnerScreen(self.service, op, params))


# ---------------------------------------------------------------------------
# Results of a list operation
# ---------------------------------------------------------------------------
class ResultScreen(TableScreen):
    BINDINGS = TableScreen.BINDINGS + [
        Binding("r", "reload", "Refresh"),
        Binding("v", "raw", "Raw JSON"),
        Binding("e", "edit", "Edit params"),
        Binding("x", "runner", "Run…"),
    ]

    def __init__(
        self,
        service: str,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        inherited: dict[str, Any] | None = None,
        result: OperationResult | None = None,
        region: str | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.operation = operation
        self.params = dict(params or {})
        self.inherited = dict(inherited or {})
        self.result = result
        # A pinned region (e.g. a resource found by the multi-region scan) ignores Ctrl+G.
        self.pinned_region = region

    def crumbs_text(self) -> str:
        args = ", ".join(f"{k}={format_cell(v)}" for k, v in self.params.items())
        return f"[b]{self.service}[/b] › {resource_label(self.operation)} [dim]{self.operation}({escape(args)})[/dim]"

    def item_context(self) -> ItemContext:
        params = {**self.inherited, **self.params}
        key = self.result.items_key if self.result else None
        region = self.pinned_region or (self.result.region if self.result else None)
        return ItemContext(self.service, self.operation, params, key, region=region)

    def on_mount(self) -> None:
        super().on_mount()
        if self.result is not None:
            self._show(self.result)
        else:
            self.action_reload()

    def reload_for_region(self) -> None:
        if self.pinned_region is None:
            self.action_reload()

    def action_reload(self) -> None:
        if not is_read_only(self.operation):
            self.notify("Write operations are not re-run automatically.", severity="warning")
            return
        self.table.loading = True
        self.status.ok(f"Calling {self.operation}…")
        self._load()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _load(self) -> None:
        try:
            result = self.app.ctx.execute(self.service, self.operation, self.params, region=self.pinned_region)
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self._failed, error_text(exc))
        else:
            self.from_thread(self._show, result)

    def _failed(self, message: str) -> None:
        self.table.loading = False
        self.set_rows([], [])
        self.status.error(message)

    def _show(self, result: OperationResult) -> None:
        self.result = result
        self.table.loading = False
        if not result.is_list:
            reload = (self.service, self.operation, self.params) if is_read_only(self.operation) else None
            if self.pinned_region is None and result.region:
                self.pinned_region = result.region
            self.app.switch_screen(
                DetailScreen(result.raw, self.item_context(), title=self.operation, reload=reload)
            )
            return
        views = [row_view(i) for i in result.items]
        columns = choose_columns(views, nouns=self.item_context().nouns())
        rows = []
        for item, view in zip(result.items, views, strict=True):
            cells = [format_cell(view.get(c)) for c in columns]
            rows.append((cells, to_json(item, indent=None), item))
        self.set_rows(columns or ["(empty)"], rows)
        self.refresh_crumbs()

    def on_rows_rendered(self, shown: int, total: int) -> None:
        if self.result is None:
            return
        more = " · [yellow]truncated, raise --max-items to see more[/yellow]" if self.result.truncated else ""
        empty = " · nothing found in this region" if total == 0 else ""
        self.status.ok(f"{shown} of {total} {self.result.items_key or 'items'}{more}{empty} · Enter: details")

    def open_payload(self, item: Any) -> None:
        self.app.push_screen(DetailScreen(item, self.item_context(), title=resource_label(self.operation)))

    def action_raw(self) -> None:
        if self.result:
            self.app.push_screen(TextViewer(f"{self.service} › {self.operation} raw response", self.result.raw))

    def action_edit(self) -> None:
        self.app.push_screen(
            RunnerScreen(self.service, self.operation, self.params, inherited=self.inherited, region=self.item_context().region)
        )

    def action_runner(self) -> None:
        self.app.open_runner(self.service, self.item_context(), self.highlighted_payload())


# ---------------------------------------------------------------------------
# Details of one resource + related operations
# ---------------------------------------------------------------------------
class DetailScreen(BaseScreen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("x", "runner", "Run…"),
        Binding("y", "copy", "Copy JSON"),
        Binding("w", "save", "Save JSON"),
        Binding("r", "reload", "Reload"),
        Binding("tab", "focus_next", show=False),
    ]

    def __init__(self, item: Any, ctx: ItemContext, *, title: str = "", reload: tuple[str, str, dict] | None = None) -> None:
        super().__init__()
        self.item = item
        self.ctx = ctx
        self.title_text = title
        self.reload_spec = reload
        self._actions: dict[str, Any] = {}

    def crumbs_text(self) -> str:
        view = row_view(self.item) if isinstance(self.item, dict) else {}
        label = ""
        for key in ("Name (tag)", "Name", "name", "Key"):
            if key in view:
                label = str(view[key])
                break
        if not label:
            for key, value in view.items():
                if key.lower().endswith(("name", "id", "identifier")):
                    label = str(value)
                    break
        return f"[b]{self.ctx.service}[/b] › {escape(self.title_text)} › {escape(format_cell(label)) or 'details'}"

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        with Horizontal(id="detail"):
            with VerticalScroll(id="detail-json"):
                yield Static(json_renderable(self.item), id="json")
            with Vertical(id="detail-side"):
                yield Label("Actions & related operations", classes="side-title")
                yield OptionList(id="actions")
        yield StatusLine(classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        self.query_one("#actions", OptionList).focus()
        self._compute_actions()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _compute_actions(self) -> None:
        from awsnav.ui.actions import custom_actions

        try:
            custom = custom_actions(self.ctx, self.item)
            related = self.app.ctx.catalog.related_operations(self.ctx, self.item) if isinstance(self.item, dict) else []
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self.query_one(StatusLine).error, error_text(exc))
            return
        self.from_thread(self._show_actions, custom, related)

    def _show_actions(self, custom, related) -> None:
        olist = self.query_one("#actions", OptionList)
        olist.clear_options()
        self._actions = {}
        options = []
        for i, action in enumerate(custom):
            oid = f"c{i}"
            self._actions[oid] = action
            options.append(Option(f"[b]★ {escape(action.label)}[/b]", id=oid))
        for i, rel in enumerate(related):
            oid = f"r{i}"
            self._actions[oid] = rel
            # List/Describe calls lead to more resources (▸); Get calls show one object.
            marker = "▸" if operation_prefix(rel.operation) in ("List", "Describe") else " "
            args = ", ".join(f"{k}={_param_text(v)}" for k, v in rel.params.items())
            options.append(Option(f"{marker} {rel.operation}\n    [dim]{escape(args)}[/dim]", id=oid))
        options.append(Option("  [i]Run any operation of this service… (x)[/i]", id="runner"))
        olist.add_options(options)
        olist.highlighted = 0
        self.query_one(StatusLine).ok(
            f"{len(custom) + len(related)} actions · Enter to run · x: any operation · y: copy JSON"
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if oid == "runner":
            self.action_runner()
            return
        action = self._actions.get(oid)
        if action is None:
            return
        if oid.startswith("c"):
            action.run(self)
        else:
            self.app.push_screen(
                ResultScreen(self.ctx.service, action.operation, action.params, inherited=self.ctx.params, region=self.ctx.region)
            )

    def action_runner(self) -> None:
        self.app.open_runner(self.ctx.service, self.ctx, self.item)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(to_json(self.item))
        self.notify("JSON copied to clipboard (terminal must support OSC 52)")

    def action_save(self) -> None:
        save_text_dialog(self, to_json(self.item), "resource.json")

    def action_reload(self) -> None:
        if not self.reload_spec:
            self.notify("Reload is available for Get/Describe results; go back and press r on the list.")
            return
        service, op, params = self.reload_spec
        self.query_one(StatusLine).ok(f"Calling {op}…")
        self._reload(service, op, params)

    @property
    def pinned_region(self) -> str | None:
        return self.ctx.region

    def reload_for_region(self) -> None:
        if self.reload_spec and self.ctx.region is None:
            self.action_reload()

    @work(thread=True, exclusive=True, exit_on_error=False, group="reload")
    def _reload(self, service: str, op: str, params: dict) -> None:
        try:
            result = self.app.ctx.execute(service, op, params, region=self.ctx.region)
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self.query_one(StatusLine).error, error_text(exc))
            return
        self.from_thread(self._reloaded, result.raw)

    def _reloaded(self, raw: Any) -> None:
        self.item = raw
        self.query_one("#json", Static).update(json_renderable(raw))
        self._compute_actions()


# ---------------------------------------------------------------------------
# Operation runner: call any API with JSON parameters
# ---------------------------------------------------------------------------
class RunnerScreen(BaseScreen):
    BINDINGS = [
        # Priority: the JSON editor would otherwise swallow Esc.
        Binding("escape", "app.pop_screen", "Back", priority=True),
        Binding("ctrl+r", "run", "Run"),
        Binding("f5", "run", "Run", show=False),
        Binding("ctrl+o", "pick", "Other operation"),
    ]

    def __init__(
        self,
        service: str,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        inherited: dict[str, Any] | None = None,
        region: str | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.operation = operation
        self.params = params or {}
        self.inherited = inherited or {}
        self.pinned_region = region

    def crumbs_text(self) -> str:
        kind = "read-only" if is_read_only(self.operation) else "[red]WRITE[/red]"
        return f"[b]{self.service}[/b] › run [b]{self.operation}[/b] ({kind})"

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        with Horizontal(id="runner"):
            with Vertical(id="runner-left"):
                yield Label("Request parameters (JSON)", classes="side-title")
                yield TextArea.code_editor(json.dumps(self.params, indent=2, default=str), language="json", id="params")
                with Horizontal(classes="buttons"):
                    yield Button("Run (Ctrl+R)", variant="primary", id="run")
                    yield Button("Other operation (Ctrl+O)", id="pick")
            with VerticalScroll(id="runner-doc"):
                yield Static(id="doc")
        yield StatusLine(classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        self._show_doc()
        self.query_one("#params", TextArea).focus()

    def _show_doc(self) -> None:
        catalog = self.app.ctx.catalog
        lines = [
            f"[b]{self.operation}[/b]",
            "",
            escape(catalog.documentation(self.service, self.operation)[:1500]),
            "",
            "[b]Parameters[/b]",
        ]
        params = catalog.describe_params(self.service, self.operation)
        if not params:
            lines.append("  (none)")
        for name, typ, required, doc in params:
            flag = "[red]required[/red]" if required else "[dim]optional[/dim]"
            lines.append(f"• [b]{name}[/b]  {escape(typ)}  {flag}")
            if doc:
                lines.append(f"  [dim]{escape(doc[:300])}[/dim]")
        self.query_one("#doc", Static).update("\n".join(lines))
        if not is_read_only(self.operation):
            msg = "This operation can change resources." + (
                " You will be asked to confirm." if self.app.ctx.allow_write else " Start awsnav with --allow-write to enable it."
            )
            self.query_one(StatusLine).error(msg)
        else:
            self.query_one(StatusLine).ok("Edit the JSON and press Ctrl+R to run")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self.action_run()
        elif event.button.id == "pick":
            self.action_pick()

    def action_pick(self) -> None:
        def chosen(op: str | None) -> None:
            if op:
                params = self.app.ctx.catalog.params_template(self.service, op)
                self.app.switch_screen(RunnerScreen(self.service, op, params, inherited=self.inherited, region=self.pinned_region))

        self.app.push_screen(operation_picker(self.app.ctx, self.service, self.operation), chosen)

    def action_run(self) -> None:
        text = self.query_one("#params", TextArea).text.strip() or "{}"
        try:
            params = json.loads(text)
            if not isinstance(params, dict):
                raise ValueError("parameters must be a JSON object")
        except ValueError as exc:
            self.query_one(StatusLine).error(f"Invalid JSON: {exc}")
            return
        if not is_read_only(self.operation):
            if not self.app.ctx.allow_write:
                self.query_one(StatusLine).error("Read-only mode: restart with --allow-write to call write operations.")
                return

            def confirmed(ok: bool | None) -> None:
                if ok:
                    self._start(params)

            self.app.push_screen(
                ConfirmModal(
                    "Run a write operation?",
                    f"{self.service}:{self.operation} in {self.display_region()}\n\n{json.dumps(params, indent=2)[:800]}",
                ),
                confirmed,
            )
            return
        self._start(params)

    def _start(self, params: dict[str, Any]) -> None:
        self.query_one(StatusLine).ok(f"Calling {self.operation}…")
        self.query_one("#run", Button).disabled = True
        self._execute(params)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _execute(self, params: dict[str, Any]) -> None:
        try:
            result = self.app.ctx.execute(self.service, self.operation, params, region=self.pinned_region)
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self._failed, error_text(exc))
        else:
            self.from_thread(self._done, result)

    def _failed(self, message: str) -> None:
        self.query_one("#run", Button).disabled = False
        self.query_one(StatusLine).error(message)

    def _done(self, result: OperationResult) -> None:
        self.query_one("#run", Button).disabled = False
        self.query_one(StatusLine).ok(f"{self.operation} succeeded")
        if result.is_list:
            self.app.push_screen(
                ResultScreen(
                    self.service, self.operation, result.params, inherited=self.inherited, result=result, region=self.pinned_region
                )
            )
        else:
            ctx = ItemContext(self.service, self.operation, {**self.inherited, **result.params}, None, region=self.pinned_region)
            reload = (self.service, self.operation, result.params) if is_read_only(self.operation) else None
            self.app.push_screen(DetailScreen(result.raw, ctx, title=self.operation, reload=reload))


def _param_text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return format_cell(text)


def operation_picker(ctx: AwsContext, service: str, current: str | None = None) -> PickerModal:
    catalog = ctx.catalog
    choices = []
    for op in catalog.all_operations(service):
        kind = "" if is_read_only(op) else "  [red](write)[/red]"
        choices.append((op, f"{op}{kind}"))
    return PickerModal(f"{service}: choose an operation", choices, current=current)
