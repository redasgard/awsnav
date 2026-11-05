"""Discovery of AWS services and operations from botocore's bundled API models.

Nothing here is hard-coded per service: every service that the installed
botocore knows about (EC2, S3, Bedrock, AgentCore, and anything released
later) is listed, and its browsable "resource" operations are derived from
the API model.  Upgrading boto3/botocore is enough to pick up new services.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import botocore
import botocore.session
from botocore.model import OperationModel, ServiceModel, Shape

# Operations whose name starts with one of these are treated as read-only.
READ_ONLY_PREFIXES = (
    "List",
    "Describe",
    "Get",
    "Search",
    "Lookup",
    "Head",
    "BatchGet",
    "Query",
    "Scan",
    "Filter",
    "BatchDescribe",
)

# Operations that may be offered as top-level "resource types" of a service.
ROOT_PREFIXES = ("List", "Describe", "Get")

# Input/output members that only exist for paging.
PAGINATION_NAMES = {
    "nexttoken",
    "marker",
    "nextmarker",
    "maxresults",
    "maxitems",
    "maxrecords",
    "limit",
    "continuationtoken",
    "nextcontinuationtoken",
    "pagesize",
    "pagetoken",
    "nextpagetoken",
    "paginationtoken",
    "startingtoken",
    "istruncated",
    "truncated",
    "maxkeys",
    "keymarker",
    "nextkeymarker",
    "startafter",
}

# Parameter names that are too generic to auto-fill when they are optional.
GENERIC_PARAM_NAMES = {
    "name",
    "id",
    "arn",
    "type",
    "status",
    "state",
    "description",
    "filter",
    "filters",
    "owner",
    "region",
    "version",
    "tags",
    "key",
    "value",
    "prefix",
    "include",
    "scope",
    "mode",
    "sortby",
    "sortorder",
}

# Services shown at the top of the service list.
POPULAR_SERVICES = [
    "ec2",
    "s3",
    "iam",
    "lambda",
    "bedrock",
    "bedrock-runtime",
    "bedrock-agent",
    "bedrock-agentcore-control",
    "rds",
    "dynamodb",
    "ecs",
    "eks",
    "ecr",
    "cloudformation",
    "cloudwatch",
    "logs",
    "sqs",
    "sns",
    "route53",
    "cloudfront",
    "elbv2",
    "autoscaling",
    "secretsmanager",
    "ssm",
    "kms",
    "sagemaker",
    "apigateway",
    "apigatewayv2",
    "stepfunctions",
    "events",
    "sts",
    "organizations",
]

# Default parameters for root operations that would otherwise return
# enormous public data sets (e.g. every public AMI in the region).
CURATED_DEFAULT_PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("ec2", "DescribeImages"): {"Owners": ["self"]},
    ("ec2", "DescribeSnapshots"): {"OwnerIds": ["self"]},
    ("ec2", "DescribeFpgaImages"): {"Owners": ["self"]},
    ("ssm", "ListDocuments"): {"Filters": [{"Key": "Owner", "Values": ["Self"]}]},
}

# JMESPath expressions that pick the interesting items out of a response
# when the default "first list in the output" rule is not good enough.
CURATED_ITEM_PATHS: dict[tuple[str, str], str] = {
    ("ec2", "DescribeInstances"): "Reservations[].Instances[]",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ITEM_KEY_SUFFIXES = (
    "Summaries",
    "Summary",
    "List",
    "Details",
    "Detail",
    "Infos",
    "Info",
    "Items",
    "Arns",
    "Ids",
    "Names",
    "Urls",
    "Set",
)


def split_camel(name: str) -> str:
    """``DBInstances`` -> ``DB Instances``."""
    return _CAMEL_RE.sub(" ", name)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = _HTML_TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def operation_prefix(op_name: str) -> str:
    for prefix in sorted(READ_ONLY_PREFIXES, key=len, reverse=True):
        if op_name.startswith(prefix):
            return prefix
    m = re.match(r"[A-Z][a-z]*", op_name)
    return m.group(0) if m else ""


def operation_noun(op_name: str) -> str:
    """``ListFoundationModels`` -> ``FoundationModels``."""
    prefix = operation_prefix(op_name)
    noun = op_name[len(prefix) :]
    return noun or op_name


def resource_label(op_name: str) -> str:
    """Human label for a resource-listing operation."""
    return split_camel(operation_noun(op_name))


def is_read_only(op_name: str) -> bool:
    return op_name.startswith(READ_ONLY_PREFIXES)


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def singular_candidates(word: str) -> set[str]:
    out = {word}
    if word.endswith("ies") and len(word) > 3:
        out.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 2:
        out.add(word[:-2])
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        out.add(word[:-1])
    return out


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes)


@dataclass
class ItemContext:
    """Where an item came from, used to find drill-down operations."""

    service: str
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    items_key: str | None = None
    # Region the item lives in; ``None`` means "whatever region is current".
    region: str | None = None

    def nouns(self) -> set[str]:
        """Lower-case nouns that describe the item (``bucket``, ``model``...)."""
        nouns: set[str] = set()

        def add(word: str) -> None:
            if not word:
                return
            for cand in singular_candidates(word):
                nouns.add(cand.lower())
            parts = split_camel(word).split()
            if len(parts) > 1:
                for cand in singular_candidates(parts[-1]):
                    nouns.add(cand.lower())

        add(operation_noun(self.operation))
        if self.items_key:
            last = re.split(r"[.\[\]]+", self.items_key.strip("[]."))
            last_word = [p for p in last if p]
            if last_word:
                word = last_word[-1]
                word = word[0].upper() + word[1:]
                add(word)
                for suffix in _ITEM_KEY_SUFFIXES:
                    if word.endswith(suffix) and len(word) > len(suffix):
                        add(word[: -len(suffix)])
                        break
        return nouns


@dataclass
class RelatedOperation:
    operation: str
    params: dict[str, Any]
    via_required: bool

    @property
    def label(self) -> str:
        args = ", ".join(f"{k}={_short(v)}" for k, v in self.params.items())
        return f"{self.operation}({args})"


def _short(value: Any, limit: int = 40) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


_SUFFIX_PREFERENCES = {
    "identifier": ["id", "arn", "name", "identifier"],
    "arn": ["arn"],
    "id": ["id"],
    "name": ["name"],
    "url": ["url"],
    "": ["name", "arn", "id", "identifier", "url"],
}


def _scalar_map(item: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    return {k.lower(): (k, v) for k, v in item.items() if _is_scalar(v)}


def match_param_value(param: str, item_map: dict[str, tuple[str, Any]], nouns: set[str]) -> Any:
    """Find a value in ``item_map`` for an API parameter called ``param``."""
    pl = param.lower()
    if pl in item_map:
        return item_map[pl][1]
    base, suffix = pl, ""
    for suf in ("identifier", "arn", "id", "name", "url"):
        if pl.endswith(suf) and len(pl) > len(suf):
            base, suffix = pl[: -len(suf)], suf
            break
    prefs = _SUFFIX_PREFERENCES[suffix]
    for suf in prefs:
        if base + suf in item_map:
            return item_map[base + suf][1]
    if base in nouns:
        for suf in prefs:
            if suf in item_map:
                return item_map[suf][1]
    return None


def _fill_param(name: str, shape: Shape, item_map, nouns) -> Any:
    if shape.type_name in ("string", "integer", "long", "boolean"):
        value = match_param_value(name, item_map, nouns)
        if value is None:
            return None
        if shape.type_name == "string" and not isinstance(value, str):
            return None
        if shape.type_name in ("integer", "long") and (isinstance(value, bool) or not isinstance(value, int)):
            return None
        if shape.type_name == "boolean" and not isinstance(value, bool):
            return None
        return value
    if shape.type_name == "list" and shape.member.type_name == "string":
        for cand in singular_candidates(name):
            if cand == name:
                continue
            value = match_param_value(cand, item_map, nouns)
            if isinstance(value, str):
                return [value]
    return None


class Catalog:
    """Cached access to botocore service models."""

    def __init__(self, botocore_session: botocore.session.Session):
        self._bs = botocore_session
        self._models: dict[str, ServiceModel] = {}
        self._lock = threading.Lock()
        self._full_names: dict[str, str] = {}

    # -- services -----------------------------------------------------
    def services(self) -> list[str]:
        return sorted(self._bs.get_available_services())

    def ordered_services(self) -> list[str]:
        available = set(self.services())
        popular = [s for s in POPULAR_SERVICES if s in available]
        rest = sorted(available - set(popular))
        return popular + rest

    def is_popular(self, service: str) -> bool:
        return service in POPULAR_SERVICES

    def model(self, service: str) -> ServiceModel:
        with self._lock:
            model = self._models.get(service)
            if model is None:
                model = self._bs.get_service_model(service)
                self._models[service] = model
            return model

    def full_name(self, service: str) -> str:
        if service in self._full_names:
            return self._full_names[service]
        name = self.model(service).metadata.get("serviceFullName", service)
        self._full_names[service] = name
        return name

    def cached_full_names(self) -> dict[str, str]:
        return dict(self._full_names)

    def load_full_names(self, cache_dir: Path | None = None) -> dict[str, str]:
        """Load every service's full name; slow the first time, then cached on disk."""
        cache_dir = cache_dir or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "awsnav"
        cache_file = cache_dir / f"service-names-{botocore.__version__}.json"
        try:
            names = json.loads(cache_file.read_text())
            if isinstance(names, dict):
                self._full_names.update(names)
        except (OSError, ValueError):
            pass
        missing = [s for s in self.services() if s not in self._full_names]
        if missing:
            for service in missing:
                try:
                    self.full_name(service)
                except Exception:  # noqa: BLE001 - a broken model must not break the list
                    self._full_names[service] = service
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(self._full_names))
            except OSError:
                pass
        return dict(self._full_names)

    # -- operations ---------------------------------------------------
    def operation(self, service: str, op_name: str) -> OperationModel:
        return self.model(service).operation_model(op_name)

    def all_operations(self, service: str) -> list[str]:
        return sorted(self.model(service).operation_names)

    def required_params(self, service: str, op_name: str) -> list[str]:
        shape = self.operation(service, op_name).input_shape
        return list(shape.required_members) if shape is not None else []

    def is_root_operation(self, service: str, op_name: str) -> bool:
        if not op_name.startswith(ROOT_PREFIXES):
            return False
        op = self.operation(service, op_name)
        if op.deprecated or op.has_streaming_output or op.has_event_stream_output:
            return False
        return not self.required_params(service, op_name)

    def root_operations(self, service: str) -> list[str]:
        ops = [o for o in self.all_operations(service) if self.is_root_operation(service, o)]
        order = {"List": 0, "Describe": 1, "Get": 2}
        return sorted(ops, key=lambda o: (order.get(operation_prefix(o), 9), operation_noun(o)))

    def paginator_config(self, service: str, op_name: str) -> dict[str, Any] | None:
        try:
            return self._bs.get_paginator_model(service).get_paginator(op_name)
        except Exception:  # noqa: BLE001 - no paginators for this service/op
            return None

    def default_params(self, service: str, op_name: str) -> dict[str, Any]:
        return json.loads(json.dumps(CURATED_DEFAULT_PARAMS.get((service, op_name), {})))

    def documentation(self, service: str, op_name: str) -> str:
        return strip_html(self.operation(service, op_name).documentation)

    def describe_params(self, service: str, op_name: str) -> list[tuple[str, str, bool, str]]:
        """``(name, type, required, doc)`` for each input member."""
        shape = self.operation(service, op_name).input_shape
        if shape is None:
            return []
        required = set(shape.required_members)
        out = []
        for name, member in shape.members.items():
            out.append((name, _type_label(member), name in required, strip_html(member.documentation)))
        out.sort(key=lambda r: (not r[2], r[0]))
        return out

    def params_template(self, service: str, op_name: str, prefill: dict[str, Any] | None = None) -> dict[str, Any]:
        """Parameters skeleton: required members (prefilled where possible)."""
        prefill = prefill or {}
        shape = self.operation(service, op_name).input_shape
        if shape is None:
            return {}
        out: dict[str, Any] = {}
        for name in shape.required_members:
            out[name] = prefill.get(name, _placeholder(shape.members[name]))
        for name, value in prefill.items():
            if name in shape.members and name not in out:
                out[name] = value
        return out

    # -- results ------------------------------------------------------
    def extract_items(self, service: str, op_name: str, raw: dict[str, Any]) -> tuple[list[Any] | None, str | None]:
        """Return the list of resources in a response, or ``(None, None)`` for a single object."""
        import jmespath

        curated = CURATED_ITEM_PATHS.get((service, op_name))
        if curated:
            return list(jmespath.search(curated, raw) or []), curated

        keys: list[str] = []
        pag = self.paginator_config(service, op_name)
        if pag and pag.get("result_key"):
            rk = pag["result_key"]
            keys = list(rk) if isinstance(rk, list) else [rk]
        else:
            out = self.operation(service, op_name).output_shape
            members = {
                n: s
                for n, s in (out.members.items() if out is not None else [])
                if n.lower() not in PAGINATION_NAMES
            }
            lists = [n for n, s in members.items() if s.type_name == "list"]
            if op_name.startswith("Get") and not (len(members) == 1 and lists):
                lists = []
            keys = lists
            if not keys and len(members) == 1:
                (name, shape), = members.items()
                if shape.type_name == "structure":
                    keys = [f"{name}.{n2}" for n2, s2 in shape.members.items() if s2.type_name == "list"]
        for key in keys:
            value = jmespath.search(key, raw)
            if isinstance(value, list) and value:
                return value, key
        if keys:
            value = jmespath.search(keys[0], raw)
            return (value if isinstance(value, list) else []), keys[0]
        return None, None

    # -- drill down ---------------------------------------------------
    def related_operations(self, ctx: ItemContext, item: dict[str, Any], include_writes: bool = False) -> list[RelatedOperation]:
        """Operations that can be called using values from ``item`` (and its parent context)."""
        if not isinstance(item, dict):
            return []
        nouns = ctx.nouns()
        item_map = _scalar_map(item)
        ctx_map = _scalar_map(ctx.params)
        results: list[RelatedOperation] = []
        for op_name in self.all_operations(ctx.service):
            if op_name == ctx.operation:
                continue
            if not include_writes and not is_read_only(op_name):
                continue
            op = self.operation(ctx.service, op_name)
            if op.deprecated or op.has_streaming_output or op.has_event_stream_output:
                continue
            shape = op.input_shape
            if shape is None:
                continue
            required = list(shape.required_members)
            params: dict[str, Any] = {}
            from_item = False
            ok = True
            for name in required:
                member = shape.members[name]
                value = _fill_param(name, member, item_map, nouns)
                if value is not None:
                    from_item = True
                else:
                    if name.lower() in ctx_map:
                        value = ctx_map[name.lower()][1]
                    else:
                        value = ctx.params.get(name)
                if value is None:
                    ok = False
                    break
                params[name] = value
            if not ok:
                continue
            via_required = from_item
            if not from_item:
                for name, member in shape.members.items():
                    if name in params or name.lower() in GENERIC_PARAM_NAMES or name.lower() in PAGINATION_NAMES:
                        continue
                    value = _fill_param(name, member, item_map, nouns)
                    if value is not None:
                        params[name] = value
                        from_item = True
            if from_item:
                results.append(RelatedOperation(op_name, params, via_required))
        results.sort(key=lambda r: (not r.via_required, operation_prefix(r.operation) != "Get", r.operation))
        return results

    def prefill_for(self, service: str, op_name: str, ctx: ItemContext | None, item: dict[str, Any] | None) -> dict[str, Any]:
        """Best-effort parameter values for any operation from an item."""
        shape = self.operation(service, op_name).input_shape
        if shape is None:
            return {}
        out: dict[str, Any] = {}
        nouns = ctx.nouns() if ctx else set()
        item_map = _scalar_map(item) if isinstance(item, dict) else {}
        ctx_params = ctx.params if ctx else {}
        for name, member in shape.members.items():
            value = _fill_param(name, member, item_map, nouns) if item_map else None
            if value is None and name in ctx_params:
                value = ctx_params[name]
            if value is not None and (name in shape.required_members or name.lower() not in GENERIC_PARAM_NAMES):
                out[name] = value
        return out


def wrap_scalar_items(items: Iterable[Any], items_key: str | None) -> list[Any]:
    """Turn ``["a", "b"]`` from ``TableNames`` into ``[{"TableName": "a"}, ...]``."""
    items = list(items)
    if not items or not all(_is_scalar(i) for i in items):
        return items
    key = "Value"
    if items_key:
        parts = [p for p in re.split(r"[.\[\]]+", items_key) if p]
        if parts:
            key = singular(parts[-1])
    return [{key: i} for i in items]


def _type_label(shape: Shape) -> str:
    if shape.type_name == "list":
        return f"list<{_type_label(shape.member)}>"
    if shape.type_name == "map":
        return f"map<{_type_label(shape.key)},{_type_label(shape.value)}>"
    if shape.type_name == "structure":
        return shape.name
    enum = getattr(shape, "enum", None)
    if enum:
        return "enum(" + "|".join(enum[:6]) + ("|…" if len(enum) > 6 else "") + ")"
    return shape.type_name


def _placeholder(shape: Shape, depth: int = 0) -> Any:
    t = shape.type_name
    if t == "string":
        enum = getattr(shape, "enum", None)
        return enum[0] if enum else f"<{shape.name}>"
    if t in ("integer", "long"):
        return 0
    if t in ("float", "double"):
        return 0.0
    if t == "boolean":
        return False
    if t == "list":
        return [] if depth > 1 else [_placeholder(shape.member, depth + 1)]
    if t == "map":
        return {}
    if t == "structure":
        if depth > 1:
            return {}
        return {n: _placeholder(shape.members[n], depth + 1) for n in shape.required_members}
    if t == "timestamp":
        return "2024-01-01T00:00:00Z"
    if t == "blob":
        return ""
    return None


