"""Multi-region resource scan: choose regions, watch results arrive, drill in."""

from __future__ import annotations

import threading
from typing import Any

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, ProgressBar, RadioButton, RadioSet, SelectionList, Static

from awsnav.formatting import format_cell
from awsnav.scanner import FoundResource, ScanError, ScanResult, Scanner, enabled_regions
from awsnav.ui.common import (
    BaseScreen,
    Crumbs,
    PromptModal,
    StatusLine,
    TableScreen,
    TextViewer,
    TopBar,
    error_text,
)

DEFAULT_WORKERS = 16


class ScanSetupScreen(BaseScreen):
    """Pick regions and options for a resource scan."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+r", "start", "Start scan"),
        Binding("f5", "start", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._regions: list[str] = []

    def crumbs_text(self) -> str:
        return "[b]Scan for active resources[/b]"

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        with Horizontal(id="scan-setup"):
            with Vertical(id="scan-regions"):
                yield Label("Regions", classes="side-title")
                with Horizontal(classes="buttons"):
                    yield Button("All", id="all", compact=True)
                    yield Button("None", id="none", compact=True)
                    yield Button("Current", id="current", compact=True)
                yield SelectionList(id="regions")
            with VerticalScroll(id="scan-options"):
                yield Label("What to look for", classes="side-title")
                with RadioSet(id="mode"):
                    yield RadioButton("Standard: about 120 common resource types, plus every tagged resource", value=True, id="standard")
                    yield RadioButton("Deep: also every List/Describe call of every service (slow, noisier)", id="deep")
                yield Checkbox("Include global services (IAM, S3, Route 53, CloudFront)", True, id="global")
                yield Checkbox("Add tagged resources found by the Resource Groups Tagging API", True, id="tagged")
                yield Label("Only these services (optional, comma-separated, wildcards allowed)")
                yield Input(placeholder="e.g. ec2, s3, bedrock*", id="services")
                yield Label("Parallel requests")
                yield Input(value=str(DEFAULT_WORKERS), type="integer", id="workers")
                yield Static(id="estimate", classes="hint")
                yield Button("Start scan (Ctrl+R)", variant="primary", id="start")
        yield StatusLine(classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_crumbs()
        self.query_one(StatusLine).ok("Loading the regions enabled for this account…")
        self._load_regions()

    @work(thread=True, exclusive=True, exit_on_error=False, group="regions")
    def _load_regions(self) -> None:
        regions = enabled_regions(self.app.ctx)
        self.from_thread(self._show_regions, regions)

    def _show_regions(self, regions: list[str]) -> None:
        self._regions = regions
        current = self.app.ctx.region
        sel = self.query_one("#regions", SelectionList)
        sel.clear_options()
        sel.add_options([(r, r, r == current) for r in regions])
        self.query_one(StatusLine).ok(f"{len(regions)} regions enabled · Space toggles a region · Ctrl+R starts")
        self._estimate()

    # -- option changes ------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        sel = self.query_one("#regions", SelectionList)
        if event.button.id == "all":
            sel.select_all()
        elif event.button.id == "none":
            sel.deselect_all()
        elif event.button.id == "current":
            sel.deselect_all()
            if self.app.ctx.region in self._regions:
                sel.select(self.app.ctx.region)
        elif event.button.id == "start":
            self.action_start()

    def on_selection_list_selected_changed(self, _event) -> None:
        self._estimate()

    def on_radio_set_changed(self, _event) -> None:
        self._estimate()

    def on_checkbox_changed(self, _event) -> None:
        self._estimate()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "services":
            self._estimate()

    def build_scanner(self) -> Scanner | None:
        regions = list(self.query_one("#regions", SelectionList).selected)
        try:
            workers = int(self.query_one("#workers", Input).value or DEFAULT_WORKERS)
        except ValueError:
            workers = DEFAULT_WORKERS
        services = [s for s in self.query_one("#services", Input).value.split(",") if s.strip()]
        deep = self.query_one("#mode", RadioSet).pressed_button is not None and self.query_one("#mode", RadioSet).pressed_button.id == "deep"
        include_global = self.query_one("#global", Checkbox).value
        if not regions and not include_global:
            return None
        return Scanner(
            self.app.ctx,
            regions,
            deep=deep,
            include_global=include_global,
            include_tagged=self.query_one("#tagged", Checkbox).value,
            services=services,
            workers=min(max(workers, 1), 64),
        )

    def _estimate(self) -> None:
        scanner = self.build_scanner()
        if scanner is None:
            self.query_one("#estimate", Static).update("Select at least one region.")
            return
        self.query_one("#estimate", Static).update("Estimating…")
        self._compute_estimate(scanner)

    @work(thread=True, exclusive=True, exit_on_error=False, group="estimate")
    def _compute_estimate(self, scanner: Scanner) -> None:
        try:
            plan = scanner.plan()
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self.query_one("#estimate", Static).update, error_text(exc))
            return
        services = len({s for _r, s, _t in plan.groups})
        text = (
            f"About {plan.total_calls:,} read-only API calls across {len(scanner.regions)} region(s) "
            f"and {services} service(s)."
        )
        if scanner.deep:
            text += " Deep scans can take several minutes."
        self.from_thread(self.query_one("#estimate", Static).update, text)

    def action_start(self) -> None:
        scanner = self.build_scanner()
        if scanner is None:
            self.query_one(StatusLine).error("Select at least one region, or include global services.")
            return
        self.app.push_screen(ScanResultsScreen(scanner))


class ScanResultsScreen(TableScreen):
    """Live table of resources found by a :class:`Scanner`."""

    BINDINGS = TableScreen.BINDINGS + [
        Binding("s", "summary", "Summary"),
        Binding("E", "errors", "Errors"),
        Binding("w", "export", "Export"),
        Binding("r", "rescan", "Rescan"),
    ]

    def __init__(self, scanner: Scanner) -> None:
        super().__init__()
        self.scanner = scanner
        self.result: ScanResult | None = None
        self.running = False
        self._pending: list[FoundResource] = []
        self._errors: list[ScanError] = []
        self._found = 0
        self._done = 0
        self._total = 0
        self._lock = threading.Lock()

    def crumbs_text(self) -> str:
        regions = self.scanner.regions
        where = ", ".join(regions) if len(regions) <= 4 else f"{len(regions)} regions"
        mode = "deep scan" if self.scanner.deep else "scan"
        return f"[b]Resource {mode}[/b] › {escape(where)}"

    def display_region(self) -> str:
        return "multi-region"

    def compose(self) -> ComposeResult:
        yield TopBar(classes="topbar")
        yield Crumbs(classes="crumbs")
        yield ProgressBar(id="progress", show_eta=True)
        yield Input(placeholder="Type to filter…  (/ to focus, Esc to clear)", id="filter")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield StatusLine(classes="status")
        yield Footer()

    COLUMNS = ["Region", "Service", "Type", "Name", "ID", "State"]

    def on_mount(self) -> None:
        super().on_mount()
        self.set_rows(self.COLUMNS, [])
        self.set_interval(0.4, self._flush)
        self._start()

    # -- scanning --------------------------------------------------------------
    def _start(self) -> None:
        self.running = True
        self._pending.clear()
        self._errors.clear()
        self._found = self._done = self._total = 0
        # Indeterminate until the worker has planned the scan (planning a deep scan takes seconds).
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self.set_rows(self.COLUMNS, [])
        self._run()

    @work(thread=True, exclusive=True, exit_on_error=False, group="scan")
    def _run(self) -> None:
        def progress(done: int, total: int, found: list[FoundResource], errors: list[ScanError]) -> None:
            with self._lock:
                self._done, self._total = done, total
                self._pending.extend(found)
                self._errors.extend(errors)

        try:
            result = self.scanner.run(progress)
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self._failed, error_text(exc))
            return
        self.from_thread(self._finished, result)

    @staticmethod
    def _row(r: FoundResource) -> tuple[list[Any], str, Any]:
        cells = [r.region, r.service, r.resource_type, format_cell(r.name), format_cell(r.identifier), r.state]
        search = " ".join([r.region, r.service, r.resource_type, r.name, r.identifier, r.state, r.arn])
        return cells, search, r

    def _flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
            done, total, n_err = self._done, self._total, len(self._errors)
        if pending:
            self._found += len(pending)
            self.append_rows([self._row(r) for r in pending])
        if self.running and total:
            self.query_one("#progress", ProgressBar).update(total=total, progress=done)
            self.status.ok(
                f"Scanning… {done:,}/{total:,} calls · {self._found:,} resources · {n_err} calls failed · Esc stops"
            )

    def _failed(self, message: str) -> None:
        self.running = False
        self.status.error(message)

    def _finished(self, result: ScanResult) -> None:
        self._flush()
        self.running = False
        self.result = result
        self.query_one("#progress", ProgressBar).update(total=max(result.calls, 1), progress=max(result.calls, 1))
        # Re-render in sorted order now that everything is in.
        self.set_rows(self.COLUMNS, [self._row(r) for r in result.resources])
        self._found = len(result.resources)
        self._report()

    def _report(self) -> None:
        if not self.result:
            return
        s = self.result.summary()
        errs = s["errors"]
        err_text = ", ".join(f"{v} {k}" for k, v in errs.items()) or "none"
        state = "Stopped" if s["cancelled"] else "Done"
        self.status.ok(
            f"{state}: {s['resources']:,} resources in {s['seconds']}s · failed calls: {err_text} · "
            "Enter: details · s: summary · E: errors · w: export"
        )

    def on_rows_rendered(self, shown: int, total: int) -> None:
        if not self.running and self.result is not None and shown != total:
            self.status.ok(f"{shown:,} of {total:,} resources match the filter")

    # -- actions ---------------------------------------------------------------
    def open_payload(self, payload: Any) -> None:
        from awsnav.ui.screens import DetailScreen

        r: FoundResource = payload
        self.app.push_screen(DetailScreen(r.raw, r.item_context(), title=r.resource_type))

    def action_back(self) -> None:
        if self.running:
            self.scanner.cancel()
            self.status.ok("Stopping after the calls in flight…")
            return
        super().action_back()

    def action_rescan(self) -> None:
        if self.running:
            return
        self.scanner.cancel_event.clear()
        self._start()

    def _snapshot(self) -> ScanResult:
        if self.result is not None:
            return self.result
        with self._lock:
            errors = list(self._errors)
        resources = [p for _k, _c, _s, p in self._rows]
        return ScanResult(self.scanner.regions, "deep" if self.scanner.deep else "standard", resources, errors, self._done)

    def action_summary(self) -> None:
        s = self._snapshot().summary()
        lines = [f"Resources: {s['resources']:,}   API calls: {s['calls']:,}", "", "By region:"]
        lines += [f"  {k:<20} {v:>6}" for k, v in s["by_region"].items()]
        lines += ["", "By type:"]
        lines += [f"  {k:<60} {v:>6}" for k, v in s["by_type"].items()]
        lines += ["", "Failed calls by kind:"]
        lines += [f"  {k:<20} {v:>6}" for k, v in s["errors"].items()] or ["  none"]
        self.app.push_screen(TextViewer("Scan summary", "\n".join(lines), filename="summary.txt", is_json=False))

    def action_errors(self) -> None:
        errors = self._snapshot().errors
        if not errors:
            self.notify("No failed calls")
            return
        order = {"denied": 0, "error": 1, "unavailable": 2}
        lines = [
            f"{e.kind:<11} {e.region:<15} {e.service}:{e.operation}  {e.message}"
            for e in sorted(errors, key=lambda e: (order.get(e.kind, 9), e.region, e.service))
        ]
        header = (
            "denied = missing IAM permission · unavailable = service or feature not offered/enabled in that region · "
            "error = anything else\n\n"
        )
        self.app.push_screen(TextViewer("Failed scan calls", header + "\n".join(lines), filename="errors.txt", is_json=False))

    def action_export(self) -> None:
        def chosen(path: str | None) -> None:
            if not path:
                return
            snap = self._snapshot()
            text = snap.to_csv() if path.lower().endswith(".csv") else snap.to_json()
            try:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
            except OSError as exc:
                self.notify(str(exc), severity="error")
            else:
                self.notify(f"Saved {len(snap.resources):,} resources to {path}")

        self.app.push_screen(PromptModal("Export to file (.json includes full details, .csv is a flat table)", "aws-resources.json"), chosen)
