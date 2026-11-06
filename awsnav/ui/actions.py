"""Hand-made shortcuts layered on top of the generic browser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from awsnav.catalog import ItemContext


@dataclass
class CustomAction:
    label: str
    run: Callable[[Any], None]  # receives the screen that offers the action


def custom_actions(ctx: ItemContext, item: Any) -> list[CustomAction]:
    if not isinstance(item, dict):
        return []
    actions: list[CustomAction] = []
    service = ctx.service

    if service == "s3":
        from awsnav.ui.s3 import S3BrowserScreen, download_object, preview_object

        if ctx.operation in ("ListBuckets", "ListDirectoryBuckets") and isinstance(item.get("Name"), str):
            bucket = item["Name"]
            actions.append(CustomAction("Browse objects", lambda s, b=bucket: s.app.push_screen(S3BrowserScreen(b))))
        bucket = ctx.params.get("Bucket")
        key = item.get("Key")
        if isinstance(bucket, str) and isinstance(key, str) and not key.endswith("/"):
            actions.append(CustomAction("Preview object content", lambda s, b=bucket, k=key: preview_object(s, b, k)))
            actions.append(CustomAction("Download object…", lambda s, b=bucket, k=key: download_object(s, b, k)))

    if service.startswith("bedrock"):
        model = bedrock_model_id(item)
        if model:
            from awsnav.ui.bedrock import ChatScreen

            actions.append(
                CustomAction(
                    f"Chat with {model} (Converse)",
                    lambda s, m=model, r=ctx.region: s.app.push_screen(ChatScreen(m, region=r)),
                )
            )

    return actions


def bedrock_model_id(item: dict[str, Any]) -> str | None:
    """Model / inference-profile identifier usable with the Converse API, if any."""
    modalities = item.get("outputModalities")
    if isinstance(modalities, list) and "TEXT" not in modalities:
        return None
    for key in ("inferenceProfileId", "inferenceProfileArn"):
        if isinstance(item.get(key), str):
            return item[key]
    if isinstance(item.get("provisionedModelArn"), str):
        return item["provisionedModelArn"]
    if isinstance(item.get("modelId"), str):
        # Some models are only reachable through an inference profile; Bedrock explains if so.
        return item["modelId"]
    if isinstance(item.get("modelArn"), str) and ("imported-model" in item["modelArn"] or "custom-model" in item["modelArn"]):
        return item["modelArn"]
    return None
