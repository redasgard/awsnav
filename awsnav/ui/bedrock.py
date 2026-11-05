"""A small chat playground for Amazon Bedrock models using the Converse API."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Input, Label, Static

from awsnav.ui.common import BaseScreen, Crumbs, PickerModal, StatusLine, TopBar, error_text


class ChatScreen(BaseScreen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+l", "clear", "New conversation"),
        Binding("ctrl+o", "pick_model", "Pick model"),
    ]

    def __init__(self, model_id: str = "") -> None:
        super().__init__()
        self.model_id = model_id
        self.messages: list[dict[str, Any]] = []
        self.total_in = 0
        self.total_out = 0

    def crumbs_text(self) -> str:
        return "[b]bedrock-runtime[/b] › Converse playground"

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        with Horizontal(id="chat-model"):
            yield Label("Model / inference profile: ")
            yield Input(value=self.model_id, placeholder="e.g. us.anthropic.claude-… (Ctrl+O to pick)", id="model")
        yield Input(placeholder="System prompt (optional)", id="system")
        yield VerticalScroll(id="chat-log")
        yield Input(placeholder="Message… (Enter to send)", id="prompt")
        yield StatusLine(classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        self.query_one("#prompt" if self.model_id else "#model", Input).focus()
        self.query_one(StatusLine).ok(
            "Each message is a billed Bedrock call. Ctrl+O lists models available in this region."
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "model":
            self.query_one("#prompt", Input).focus()
            return
        if event.input.id != "prompt" or not event.value.strip():
            return
        model = self.query_one("#model", Input).value.strip()
        if not model:
            self.query_one(StatusLine).error("Enter a model ID or inference profile first (Ctrl+O to pick).")
            return
        text = event.value.strip()
        event.input.value = ""
        self.messages.append({"role": "user", "content": [{"text": text}]})
        self._append("you", text, "chat-user")
        self.query_one(StatusLine).ok(f"Waiting for {model}…")
        event.input.disabled = True
        self._send(model, self.query_one("#system", Input).value.strip())

    def _append(self, who: str, text: str, cls: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        log.mount(Static(f"[b]{who}[/b]\n{escape(text)}", classes=f"chat-msg {cls}"))
        log.scroll_end(animate=False)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _send(self, model: str, system: str) -> None:
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": self.messages,
            "inferenceConfig": {"maxTokens": 2048},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        try:
            resp = self.app.ctx.client("bedrock-runtime").converse(**kwargs)
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self._failed, error_text(exc))
            return
        self.from_thread(self._received, resp)

    def _failed(self, message: str) -> None:
        # Drop the unanswered user turn so the conversation stays valid.
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        self._append("error", message, "chat-error")
        self.query_one(StatusLine).error(message)
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.focus()

    def _received(self, resp: dict[str, Any]) -> None:
        message = resp.get("output", {}).get("message", {"role": "assistant", "content": []})
        self.messages.append(message)
        parts = []
        for block in message.get("content", []):
            if "text" in block:
                parts.append(block["text"])
            elif "reasoningContent" in block:
                continue
            else:
                parts.append(f"<{next(iter(block), 'content')} block>")
        self._append("assistant", "\n".join(parts) or "(empty response)", "chat-assistant")
        usage = resp.get("usage", {})
        self.total_in += usage.get("inputTokens", 0)
        self.total_out += usage.get("outputTokens", 0)
        latency = resp.get("metrics", {}).get("latencyMs")
        self.query_one(StatusLine).ok(
            f"stop: {resp.get('stopReason', '?')} · tokens in/out this turn {usage.get('inputTokens', '?')}/"
            f"{usage.get('outputTokens', '?')} · session {self.total_in}/{self.total_out}"
            + (f" · {latency} ms" if latency else "")
        )
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.focus()

    def action_clear(self) -> None:
        self.messages.clear()
        self.query_one("#chat-log", VerticalScroll).remove_children()
        self.query_one(StatusLine).ok("Started a new conversation")

    def action_pick_model(self) -> None:
        self.query_one(StatusLine).ok("Loading models and inference profiles…")
        self._load_models()

    @work(thread=True, exclusive=True, exit_on_error=False, group="models")
    def _load_models(self) -> None:
        choices: list[tuple[str, str]] = []
        ctx = self.app.ctx
        errors = []
        try:
            res = ctx.execute("bedrock", "ListInferenceProfiles")
            for p in res.items or []:
                name = escape(p.get("inferenceProfileName", ""))
                choices.append((p["inferenceProfileId"], f"{escape(p['inferenceProfileId'])}  [dim]profile · {name}[/dim]"))
        except Exception as exc:  # noqa: BLE001
            errors.append(error_text(exc))
        try:
            res = ctx.execute("bedrock", "ListFoundationModels", {"byOutputModality": "TEXT"})
            for m in res.items or []:
                kinds = ",".join(m.get("inferenceTypesSupported", []))
                choices.append((m["modelId"], f"{escape(m['modelId'])}  [dim]{escape(m.get('providerName', ''))} · {kinds}[/dim]"))
        except Exception as exc:  # noqa: BLE001
            errors.append(error_text(exc))
        self.from_thread(self._show_models, choices, errors)

    def _show_models(self, choices: list[tuple[str, str]], errors: list[str]) -> None:
        if not choices:
            self.query_one(StatusLine).error("; ".join(errors) or "No text models found in this region.")
            return
        self.query_one(StatusLine).ok(f"{len(choices)} models/profiles")

        def chosen(model: str | None) -> None:
            if model:
                self.query_one("#model", Input).value = model
                self.query_one("#prompt", Input).focus()

        self.app.push_screen(PickerModal("Bedrock model or inference profile", choices, free_text=True), chosen)
