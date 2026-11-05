"""Folder-style S3 browser with object preview and download."""

from __future__ import annotations

import os
from typing import Any

from rich.markup import escape
from textual import work
from textual.binding import Binding

from awsnav.catalog import ItemContext
from awsnav.formatting import format_cell, human_size
from awsnav.session import PREVIEW_BYTES
from awsnav.ui.common import PromptModal, TableScreen, TextViewer, error_text


class S3BrowserScreen(TableScreen):
    BINDINGS = TableScreen.BINDINGS + [
        Binding("r", "reload", "Refresh"),
        Binding("p", "preview", "Preview"),
        Binding("d", "download", "Download"),
        Binding("b", "bucket", "Bucket details"),
    ]

    def __init__(self, bucket: str, prefix: str = "") -> None:
        super().__init__()
        self.bucket = bucket
        self.prefix = prefix
        self.truncated = False

    def crumbs_text(self) -> str:
        return f"[b]s3[/b] › {escape(self.bucket)} › /{escape(self.prefix)}"

    def on_mount(self) -> None:
        super().on_mount()
        self.action_reload()

    def action_reload(self) -> None:
        self.table.loading = True
        self._load()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _load(self) -> None:
        ctx = self.app.ctx
        try:
            paginator = ctx.client("s3").get_paginator("list_objects_v2")
            raw = paginator.paginate(
                Bucket=self.bucket,
                Prefix=self.prefix,
                Delimiter="/",
                PaginationConfig={"MaxItems": ctx.max_items},
            ).build_full_result()
        except Exception as exc:  # noqa: BLE001
            self.from_thread(self._failed, error_text(exc))
            return
        self.from_thread(self._show, raw)

    def _failed(self, message: str) -> None:
        self.table.loading = False
        self.set_rows([], [])
        self.status.error(message)

    def _show(self, raw: dict[str, Any]) -> None:
        self.table.loading = False
        self.truncated = "NextToken" in raw
        rows = []
        for cp in raw.get("CommonPrefixes", []) or []:
            name = cp["Prefix"][len(self.prefix) :]
            rows.append((["▸ " + name, "", "", ""], name, ("dir", cp["Prefix"])))
        for obj in raw.get("Contents", []) or []:
            if obj["Key"] == self.prefix:
                continue
            name = obj["Key"][len(self.prefix) :]
            modified = obj.get("LastModified")
            rows.append(
                (
                    [
                        "  " + name,
                        human_size(obj.get("Size")),
                        modified.strftime("%Y-%m-%d %H:%M:%S") if modified else "",
                        format_cell(obj.get("StorageClass", "")),
                    ],
                    name,
                    ("obj", obj),
                )
            )
        self.set_rows(["Name", "Size", "Last modified", "Storage class"], rows)

    def on_rows_rendered(self, shown: int, total: int) -> None:
        more = " · [yellow]truncated (--max-items)[/yellow]" if self.truncated else ""
        self.status.ok(f"{shown} of {total} entries{more} · Enter: open · p: preview · d: download · b: bucket details")

    def open_payload(self, payload: Any) -> None:
        kind, value = payload
        if kind == "dir":
            self.app.push_screen(S3BrowserScreen(self.bucket, value))
        else:
            from awsnav.ui.screens import DetailScreen

            ctx = ItemContext("s3", "ListObjectsV2", {"Bucket": self.bucket}, "Contents")
            self.app.push_screen(DetailScreen(value, ctx, title=f"{self.bucket}"))

    def _highlighted_object(self) -> dict[str, Any] | None:
        payload = self.highlighted_payload()
        if payload and payload[0] == "obj":
            return payload[1]
        self.notify("Select an object first", severity="warning")
        return None

    def action_preview(self) -> None:
        obj = self._highlighted_object()
        if obj:
            preview_object(self, self.bucket, obj["Key"])

    def action_download(self) -> None:
        obj = self._highlighted_object()
        if obj:
            download_object(self, self.bucket, obj["Key"])

    def action_bucket(self) -> None:
        from awsnav.ui.screens import DetailScreen

        ctx = ItemContext("s3", "ListBuckets", {}, "Buckets")
        self.app.push_screen(DetailScreen({"Name": self.bucket}, ctx, title="Buckets"))


def preview_object(screen, bucket: str, key: str) -> None:
    screen.notify(f"Fetching s3://{bucket}/{key} …")
    _preview_worker(screen, bucket, key)


@work(thread=True, exit_on_error=False)
def _preview_worker(screen, bucket: str, key: str) -> None:
    try:
        resp = screen.app.ctx.client("s3").get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{PREVIEW_BYTES - 1}")
        data = resp["Body"].read()
        total = resp.get("ContentRange", "").rsplit("/", 1)[-1] or str(len(data))
    except Exception as exc:  # noqa: BLE001
        screen.from_thread(screen.notify, error_text(exc), severity="error")
        return
    text = render_bytes(data)
    title = f"s3://{bucket}/{key}  (first {len(data)} of {total} bytes)"
    screen.from_thread(screen.app.push_screen, TextViewer(title, text, filename=key, is_json=False))


def render_bytes(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        lines = []
        for off in range(0, min(len(data), 4096), 16):
            chunk = data[off : off + 16]
            hexs = " ".join(f"{b:02x}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{off:08x}  {hexs:<47}  {asc}")
        return "binary content, hex dump of the first 4 KiB:\n\n" + "\n".join(lines)
    return data.decode("utf-8", errors="replace")


def download_object(screen, bucket: str, key: str) -> None:
    default = os.path.join(os.getcwd(), key.rsplit("/", 1)[-1] or "download")

    def chosen(path: str | None) -> None:
        if path:
            screen.notify(f"Downloading to {path} …")
            _download_worker(screen, bucket, key, os.path.expanduser(path))

    screen.app.push_screen(PromptModal(f"Download s3://{bucket}/{key} to", default), chosen)


@work(thread=True, exit_on_error=False)
def _download_worker(screen, bucket: str, key: str, path: str) -> None:
    try:
        screen.app.ctx.client("s3").download_file(bucket, key, path)
    except Exception as exc:  # noqa: BLE001
        screen.from_thread(screen.notify, error_text(exc), severity="error")
        return
    screen.from_thread(screen.notify, f"Saved {path}")
