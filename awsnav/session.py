"""AWS session, credentials and operation execution."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import boto3
import botocore.session
from botocore import xform_name
from botocore.config import Config
from botocore.response import StreamingBody

from awsnav.catalog import Catalog, ItemContext, wrap_scalar_items

DEFAULT_REGION = "us-east-1"
PREVIEW_BYTES = 64 * 1024


@dataclass
class OperationResult:
    service: str
    operation: str
    params: dict[str, Any]
    raw: dict[str, Any]
    items: list[Any] | None
    items_key: str | None
    truncated: bool = False
    region: str | None = None

    @property
    def is_list(self) -> bool:
        return self.items is not None

    def context(self) -> ItemContext:
        return ItemContext(self.service, self.operation, dict(self.params), self.items_key, region=self.region)


@dataclass
class ConnectionSettings:
    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    profile: str | None = None
    region: str = DEFAULT_REGION
    endpoint_url: str | None = None
    max_items: int = 500
    allow_write: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class AwsContext:
    """Holds credentials, the current region and a cache of clients."""

    def __init__(self, settings: ConnectionSettings):
        self.settings = settings
        self._bs = botocore.session.Session(profile=settings.profile or None)
        if settings.access_key and settings.secret_key:
            self._bs.set_credentials(settings.access_key, settings.secret_key, settings.session_token or None)
        self.session = boto3.Session(botocore_session=self._bs, region_name=settings.region or DEFAULT_REGION)
        self.catalog = Catalog(self._bs)
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self.identity: dict[str, Any] = {}
        self._config = Config(
            retries={"mode": "standard", "max_attempts": 3},
            user_agent_extra="awsnav",
            connect_timeout=10,
            read_timeout=60,
        )

    # -- basics -------------------------------------------------------
    @property
    def region(self) -> str:
        return self.settings.region

    @region.setter
    def region(self, value: str) -> None:
        self.settings.region = value

    @property
    def max_items(self) -> int:
        return self.settings.max_items

    @property
    def allow_write(self) -> bool:
        return self.settings.allow_write

    def client(self, service: str, region: str | None = None):
        region = region or self.region
        key = (service, region)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self.session.client(
                    service,
                    region_name=region,
                    endpoint_url=self.settings.endpoint_url or None,
                    config=self._config,
                )
                self._clients[key] = client
            return client

    def new_client(self, service: str, region: str):
        """A fresh, uncached client (used by the multi-region scanner to bound memory)."""
        with self._lock:  # creating clients from one session is not thread-safe
            return self.session.client(
                service,
                region_name=region,
                endpoint_url=self.settings.endpoint_url or None,
                config=self._config,
            )

    def verify(self) -> dict[str, Any]:
        """Check the credentials with STS and remember who we are."""
        ident = self.client("sts").get_caller_identity()
        ident.pop("ResponseMetadata", None)
        self.identity = ident
        return ident

    def regions(self) -> list[str]:
        regions: set[str] = set()
        for partition in ("aws",):
            try:
                regions.update(self.session.get_available_regions("ec2", partition_name=partition))
            except Exception:  # noqa: BLE001
                pass
        regions.add(self.region)
        return sorted(regions)

    def describe_identity(self) -> str:
        if not self.identity:
            return "not verified"
        return f"{self.identity.get('Account', '?')} · {self.identity.get('Arn', '?')}"

    # -- execution ----------------------------------------------------
    def execute(
        self,
        service: str,
        op_name: str,
        params: dict[str, Any] | None = None,
        max_items: int | None = None,
        region: str | None = None,
    ) -> OperationResult:
        """Call ``op_name`` in ``region`` (default: the current region)."""
        result = self.run_operation(self.client(service, region), service, op_name, params, max_items)
        result.region = region or self.region
        return result

    def run_operation(self, client, service: str, op_name: str, params: dict[str, Any] | None = None, max_items: int | None = None) -> OperationResult:
        params = dict(params or {})
        max_items = max_items or self.max_items
        py_name = xform_name(op_name)
        truncated = False
        if client.can_paginate(py_name):
            paginator = client.get_paginator(py_name)
            raw = paginator.paginate(**params, PaginationConfig={"MaxItems": max_items}).build_full_result()
            truncated = "NextToken" in raw
        else:
            raw = getattr(client, py_name)(**params)
        raw = dict(raw)
        raw.pop("ResponseMetadata", None)
        _materialize_streams(raw)
        items, key = self.catalog.extract_items(service, op_name, raw)
        if items is not None:
            items = wrap_scalar_items(items, key)
        return OperationResult(service, op_name, params, raw, items, key, truncated, client.meta.region_name)


def _materialize_streams(raw: dict[str, Any]) -> None:
    """Replace streaming bodies by a bounded text preview so results are serialisable."""
    for key, value in list(raw.items()):
        if isinstance(value, StreamingBody):
            data = value.read(PREVIEW_BYTES)
            value.close()
            raw[key] = data.decode("utf-8", errors="replace")
        elif hasattr(value, "__iter__") and type(value).__name__ == "EventStream":
            raw[key] = "<event stream: not displayed>"
