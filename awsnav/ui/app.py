"""The Textual application."""

from __future__ import annotations

from functools import partial
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.command import Hit, Hits, Provider

from awsnav.catalog import ItemContext
from awsnav.session import AwsContext, ConnectionSettings
from awsnav.ui.common import HelpModal, PickerModal

CSS = """
Screen { background: $surface; }
.topbar { height: 1; padding: 0 1; background: $primary-background; color: $text; }
.crumbs { height: 1; padding: 0 1; background: $boost; color: $text; }
.status { height: 1; padding: 0 1; color: $text-muted; }
.status.error { color: $error; text-style: bold; }
#filter { height: 3; margin: 0 0; border: tall $primary 40%; }
#filter:focus { border: tall $accent; }
#table { height: 1fr; }
#login { width: 76; height: auto; padding: 1 2; border: round $primary; margin-top: 1; }
#login Input { margin-bottom: 1; }
#login Button { width: 100%; }
.hint { color: $text-muted; margin-bottom: 1; }
.dialog { width: 90; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $panel; }
.dialog.tall { height: 80%; }
.dialog-title { text-style: bold; color: $accent; margin-bottom: 1; }
.buttons { height: auto; margin-top: 1; }
.buttons Button { margin-right: 2; }
ModalScreen { align: center middle; }
#picker-list { height: 1fr; }
#detail { height: 1fr; }
#detail-json { width: 3fr; border: round $primary 40%; padding: 0 1; }
#detail-side { width: 2fr; }
#actions { height: 1fr; border: round $primary 40%; }
#actions:focus { border: round $accent; }
.side-title { text-style: bold; padding: 0 1; }
#runner { height: 1fr; }
#runner-left { width: 1fr; }
#params { height: 1fr; }
#runner-doc { width: 1fr; border: round $primary 40%; padding: 0 1; }
#viewer { height: 1fr; padding: 0 1; }
#chat-model { height: 3; }
#chat-model Label { padding: 1 1 0 1; }
#chat-model Input { width: 1fr; }
#chat-log { height: 1fr; border: round $primary 40%; padding: 0 1; }
.chat-msg { margin-bottom: 1; }
.chat-user { color: $accent; }
.chat-error { color: $error; }
"""


class ServiceCommands(Provider):
    """Command palette entries: jump to any AWS service."""

    async def search(self, query: str) -> Hits:
        app = self.app
        ctx = getattr(app, "ctx", None)
        if ctx is None:
            return
        matcher = self.matcher(query)
        names = ctx.catalog.cached_full_names()
        for service in ctx.catalog.ordered_services():
            label = f"{service} — {names.get(service, '')}".rstrip(" —")
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), partial(app.open_service, service), help="Open service")

    async def discover(self) -> Hits:
        app = self.app
        ctx = getattr(app, "ctx", None)
        if ctx is None:
            return
        for service in ctx.catalog.ordered_services()[:20]:
            yield Hit(1, service, partial(app.open_service, service), help="Open service")


class AwsNavApp(App):
    TITLE = "awsnav"
    CSS = CSS
    COMMANDS = App.COMMANDS | {ServiceCommands}
    BINDINGS = [
        Binding("ctrl+g", "change_region", "Region", priority=True),
        Binding("ctrl+b", "bedrock_chat", "Bedrock chat", priority=True),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", "Help", show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, settings: ConnectionSettings, auto_connect: bool = False) -> None:
        super().__init__()
        self.settings = settings
        self.auto_connect = auto_connect
        self.ctx: AwsContext | None = None

    def on_mount(self) -> None:
        from awsnav.ui.screens import LoginScreen

        self.push_screen(LoginScreen(self.settings, self.auto_connect))

    # -- context ------------------------------------------------------
    def set_context(self, ctx: AwsContext) -> None:
        self.ctx = ctx

    def header_text(self) -> str:
        """Identity / region line shown in every screen's header."""
        if not self.ctx:
            return ""
        mode = "read-only" if not self.ctx.allow_write else "WRITE ENABLED"
        endpoint = f" · {self.ctx.settings.endpoint_url}" if self.ctx.settings.endpoint_url else ""
        return f"{self.ctx.describe_identity()} · {self.ctx.region} · {mode}{endpoint}"

    # -- navigation helpers -------------------------------------------
    def open_service(self, service: str) -> None:
        from awsnav.ui.screens import OperationsScreen

        if self.ctx is None:
            return
        self.push_screen(OperationsScreen(service))

    def open_runner(self, service: str, item_ctx: ItemContext | None = None, item: Any = None, operation: str | None = None) -> None:
        from awsnav.ui.screens import RunnerScreen, operation_picker

        if self.ctx is None:
            return
        inherited = dict(item_ctx.params) if item_ctx else {}

        def chosen(op: str | None) -> None:
            if not op:
                return
            prefill = self.ctx.catalog.prefill_for(service, op, item_ctx, item)
            params = self.ctx.catalog.params_template(service, op, prefill)
            self.push_screen(RunnerScreen(service, op, params, inherited=inherited))

        if operation:
            chosen(operation)
        else:
            self.push_screen(operation_picker(self.ctx, service), chosen)

    # -- actions --------------------------------------------------------
    def action_help(self) -> None:
        if not isinstance(self.screen, HelpModal):
            self.push_screen(HelpModal())

    def action_change_region(self) -> None:
        if self.ctx is None:
            self.notify("Connect first", severity="warning")
            return
        if isinstance(self.screen, PickerModal):
            return
        choices = [(r, r) for r in self.ctx.regions()]

        def chosen(region: str | None) -> None:
            if not region or region == self.ctx.region:
                return
            self.ctx.region = region
            self.notify(f"Region: {region}")
            screen = self.screen
            if hasattr(screen, "refresh_crumbs"):
                screen.refresh_crumbs()
            reload = getattr(screen, "reload_for_region", None)
            if callable(reload):
                reload()

        self.push_screen(PickerModal("Region (type a custom region and press Enter)", choices, current=self.ctx.region, free_text=True), chosen)

    def action_bedrock_chat(self) -> None:
        from awsnav.ui.bedrock import ChatScreen

        if self.ctx is None:
            self.notify("Connect first", severity="warning")
            return
        if not isinstance(self.screen, ChatScreen):
            self.push_screen(ChatScreen())
