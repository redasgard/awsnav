"""Find every active resource in an account across many regions.

Three sources are combined:

* a curated inventory of resource-listing calls for the services people
  care about most, with AWS-created defaults (default VPC, AWS-managed
  keys, service-linked roles, terminated instances…) filtered out;
* the Resource Groups Tagging API, which reports every *tagged* resource
  of any service, catching things the inventory does not know about;
* optionally a "deep" pass that calls every List/Describe operation that
  needs no parameters, in every service botocore knows.  It is slow and
  noisy, but it is the closest thing to "everything".
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any
from collections.abc import Callable, Iterable

import jmespath
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, NoRegionError, ReadTimeoutError

from awsnav.catalog import ItemContext, operation_prefix, resource_label
from awsnav.formatting import cell_value, tag_name, to_json

GLOBAL_REGION = "global"
SCAN_MAX_ITEMS = 1000

# States that mean "this no longer exists".
GONE_STATES = {"terminated", "deleted", "delete_complete", "delete_completed", "deleted_complete"}


@dataclass(frozen=True)
class Target:
    """One resource-listing call."""

    service: str
    operation: str
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    items_path: str | None = None
    # jmespath -> values that mark an item as an AWS default / dead resource
    skip_if: dict[str, tuple] = field(default_factory=dict, hash=False, compare=False)
    # jmespath -> string prefixes that mark an item as AWS-managed
    skip_prefix: dict[str, tuple] = field(default_factory=dict, hash=False, compare=False)
    # jmespath expressions whose presence marks an item as AWS-managed
    skip_present: tuple[str, ...] = ()
    # Keep only items whose OwnerId is the scanned account (snapshots / images shared with you are not yours).
    own_only: bool = False
    global_: bool = False

    @property
    def resource_type(self) -> str:
        return self.label or resource_label(self.operation)


def T(service: str, operation: str, label: str = "", **kw: Any) -> Target:
    return Target(service, operation, label, **kw)


# fmt: off
INVENTORY: list[Target] = [
    # --- compute -----------------------------------------------------------
    T("ec2", "DescribeInstances", "EC2 instance", items_path="Reservations[].Instances[]",
      skip_if={"State.Name": ("terminated", "shutting-down")}),
    T("ec2", "DescribeVolumes", "EBS volume"),
    T("ec2", "DescribeSnapshots", "EBS snapshot", params={"OwnerIds": ["self"]}, own_only=True),
    T("ec2", "DescribeImages", "AMI", params={"Owners": ["self"]}, own_only=True),
    T("ec2", "DescribeAddresses", "Elastic IP"),
    T("ec2", "DescribeKeyPairs", "Key pair"),
    T("ec2", "DescribeLaunchTemplates", "Launch template"),
    T("ec2", "DescribeSpotInstanceRequests", "Spot request", skip_if={"State": ("cancelled", "closed")}),
    T("ec2", "DescribeHosts", "Dedicated host", skip_if={"State": ("released",)}),
    T("ec2", "DescribeCapacityReservations", "Capacity reservation", skip_if={"State": ("cancelled", "expired")}),
    T("ec2", "DescribePlacementGroups", "Placement group"),
    T("autoscaling", "DescribeAutoScalingGroups", "Auto Scaling group"),
    T("lambda", "ListFunctions", "Lambda function"),
    T("lambda", "ListLayers", "Lambda layer"),
    T("ecs", "ListClusters", "ECS cluster"),
    T("eks", "ListClusters", "EKS cluster"),
    T("ecr", "DescribeRepositories", "ECR repository"),
    T("batch", "DescribeComputeEnvironments", "Batch compute environment"),
    T("batch", "DescribeJobQueues", "Batch job queue"),
    T("elasticbeanstalk", "DescribeEnvironments", "Beanstalk environment", skip_if={"Status": ("Terminated",)}),
    T("apprunner", "ListServices", "App Runner service"),
    T("lightsail", "GetInstances", "Lightsail instance"),
    T("workspaces", "DescribeWorkspaces", "WorkSpace"),
    # --- networking --------------------------------------------------------
    T("ec2", "DescribeVpcs", "VPC", skip_if={"IsDefault": (True,)}),
    T("ec2", "DescribeSubnets", "Subnet", skip_if={"DefaultForAz": (True,)}),
    T("ec2", "DescribeSecurityGroups", "Security group", skip_if={"GroupName": ("default",)}),
    T("ec2", "DescribeNatGateways", "NAT gateway", skip_if={"State": ("deleted", "deleting", "failed")}),
    T("ec2", "DescribeNetworkInterfaces", "Network interface"),
    T("ec2", "DescribeVpcEndpoints", "VPC endpoint", skip_if={"State": ("deleted", "Deleted")}),
    T("ec2", "DescribeVpcPeeringConnections", "VPC peering",
      skip_if={"Status.Code": ("deleted", "rejected", "expired", "failed")}),
    T("ec2", "DescribeTransitGateways", "Transit gateway", skip_if={"State": ("deleted",)}),
    T("ec2", "DescribeVpnConnections", "VPN connection", skip_if={"State": ("deleted",)}),
    T("ec2", "DescribeVpnGateways", "VPN gateway", skip_if={"State": ("deleted",)}),
    T("ec2", "DescribeCustomerGateways", "Customer gateway", skip_if={"State": ("deleted",)}),
    T("ec2", "DescribeFlowLogs", "Flow log"),
    T("elbv2", "DescribeLoadBalancers", "Load balancer (ALB/NLB/GWLB)"),
    T("elbv2", "DescribeTargetGroups", "Target group"),
    T("elb", "DescribeLoadBalancers", "Classic load balancer"),
    T("apigateway", "GetRestApis", "API Gateway REST API"),
    T("apigatewayv2", "GetApis", "API Gateway HTTP/WebSocket API"),
    T("directconnect", "DescribeConnections", "Direct Connect connection", skip_if={"connectionState": ("deleted",)}),
    T("acm", "ListCertificates", "ACM certificate"),
    T("wafv2", "ListWebACLs", "WAF web ACL", params={"Scope": "REGIONAL"}),
    # --- storage -------------------------------------------------------------
    T("efs", "DescribeFileSystems", "EFS file system"),
    T("fsx", "DescribeFileSystems", "FSx file system"),
    T("backup", "ListBackupVaults", "Backup vault"),
    T("backup", "ListBackupPlans", "Backup plan"),
    T("transfer", "ListServers", "Transfer Family server"),
    # --- databases -----------------------------------------------------------
    T("rds", "DescribeDBInstances", "RDS instance"),
    T("rds", "DescribeDBClusters", "RDS/Aurora cluster"),
    T("rds", "DescribeDBSnapshots", "RDS snapshot"),
    T("rds", "DescribeDBClusterSnapshots", "Aurora snapshot"),
    T("dynamodb", "ListTables", "DynamoDB table"),
    T("elasticache", "DescribeCacheClusters", "ElastiCache cluster"),
    T("elasticache", "DescribeServerlessCaches", "ElastiCache serverless"),
    T("memorydb", "DescribeClusters", "MemoryDB cluster"),
    T("redshift", "DescribeClusters", "Redshift cluster"),
    T("redshift-serverless", "ListWorkgroups", "Redshift Serverless workgroup"),
    T("opensearch", "ListDomainNames", "OpenSearch domain"),
    T("dms", "DescribeReplicationInstances", "DMS replication instance"),
    T("docdb-elastic", "ListClusters", "DocumentDB elastic cluster"),
    T("timestream-write", "ListDatabases", "Timestream database"),
    # --- analytics & integration ---------------------------------------------
    T("kinesis", "ListStreams", "Kinesis stream", items_path="StreamSummaries"),
    T("firehose", "ListDeliveryStreams", "Firehose stream"),
    T("kafka", "ListClustersV2", "MSK cluster"),
    T("emr", "ListClusters", "EMR cluster",
      params={"ClusterStates": ["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING"]}),
    T("glue", "GetDatabases", "Glue database"),
    T("glue", "ListJobs", "Glue job"),
    T("athena", "ListWorkGroups", "Athena workgroup", skip_if={"Name": ("primary",)}),
    T("sqs", "ListQueues", "SQS queue"),
    T("sns", "ListTopics", "SNS topic"),
    T("mq", "ListBrokers", "Amazon MQ broker"),
    T("events", "ListRules", "EventBridge rule", skip_present=("ManagedBy",)),
    T("events", "ListEventBuses", "EventBridge bus", skip_if={"Name": ("default",)}),
    T("scheduler", "ListSchedules", "EventBridge schedule"),
    T("stepfunctions", "ListStateMachines", "Step Functions state machine"),
    T("appsync", "ListGraphqlApis", "AppSync API"),
    # --- AI / ML -------------------------------------------------------------
    T("bedrock", "ListCustomModels", "Bedrock custom model"),
    T("bedrock", "ListImportedModels", "Bedrock imported model"),
    T("bedrock", "ListProvisionedModelThroughputs", "Bedrock provisioned throughput"),
    T("bedrock", "ListGuardrails", "Bedrock guardrail"),
    T("bedrock", "ListInferenceProfiles", "Bedrock application inference profile",
      params={"typeEquals": "APPLICATION"}),
    T("bedrock", "ListPromptRouters", "Bedrock prompt router", params={"type": "custom"}),
    T("bedrock", "ListCustomModelDeployments", "Bedrock custom model deployment"),
    T("bedrock", "ListMarketplaceModelEndpoints", "Bedrock Marketplace endpoint"),
    T("bedrock-agent", "ListAgents", "Bedrock agent"),
    T("bedrock-agent", "ListKnowledgeBases", "Bedrock knowledge base"),
    T("bedrock-agent", "ListFlows", "Bedrock flow"),
    T("bedrock-agent", "ListPrompts", "Bedrock prompt"),
    T("bedrock-agentcore-control", "ListAgentRuntimes", "AgentCore runtime"),
    T("bedrock-agentcore-control", "ListGateways", "AgentCore gateway"),
    T("bedrock-agentcore-control", "ListMemories", "AgentCore memory"),
    T("bedrock-agentcore-control", "ListBrowsers", "AgentCore browser", params={"type": "CUSTOM"}),
    T("bedrock-agentcore-control", "ListCodeInterpreters", "AgentCore code interpreter", params={"type": "CUSTOM"}),
    T("sagemaker", "ListEndpoints", "SageMaker endpoint"),
    T("sagemaker", "ListNotebookInstances", "SageMaker notebook"),
    T("sagemaker", "ListDomains", "SageMaker domain"),
    T("sagemaker", "ListModels", "SageMaker model"),
    T("comprehend", "ListEndpoints", "Comprehend endpoint"),
    T("kendra", "ListIndices", "Kendra index"),
    # --- security & management -----------------------------------------------
    T("kms", "ListAliases", "KMS key alias", skip_prefix={"AliasName": ("alias/aws/",)}),
    T("secretsmanager", "ListSecrets", "Secret"),
    T("ssm", "DescribeParameters", "SSM parameter"),
    T("cognito-idp", "ListUserPools", "Cognito user pool", params={"MaxResults": 60}),
    T("cognito-identity", "ListIdentityPools", "Cognito identity pool", params={"MaxResults": 60}),
    T("cloudformation", "DescribeStacks", "CloudFormation stack"),
    T("logs", "DescribeLogGroups", "Log group"),
    T("cloudwatch", "DescribeAlarms", "CloudWatch alarm"),
    T("cloudtrail", "DescribeTrails", "CloudTrail trail", params={"includeShadowTrails": False}),
    T("codecommit", "ListRepositories", "CodeCommit repository"),
    T("codebuild", "ListProjects", "CodeBuild project"),
    T("codepipeline", "ListPipelines", "CodePipeline pipeline"),
    T("amplify", "ListApps", "Amplify app"),
    T("iot", "ListThings", "IoT thing"),
    # --- global services (scanned once) --------------------------------------
    T("s3", "ListBuckets", "S3 bucket", global_=True),
    T("iam", "ListUsers", "IAM user", global_=True),
    T("iam", "ListRoles", "IAM role", global_=True, skip_prefix={"Path": ("/aws-service-role/", "/aws-reserved/")}),
    T("iam", "ListGroups", "IAM group", global_=True),
    T("iam", "ListPolicies", "IAM policy (customer managed)", params={"Scope": "Local"}, global_=True),
    T("route53", "ListHostedZones", "Route 53 hosted zone", global_=True),
    T("cloudfront", "ListDistributions", "CloudFront distribution", global_=True),
    T("wafv2", "ListWebACLs", "WAF web ACL (CloudFront)", params={"Scope": "CLOUDFRONT"}, global_=True),
]
# fmt: on

INVENTORY_KEYS = {(t.service, t.operation) for t in INVENTORY}

TAGGING_TARGET = Target("resourcegroupstaggingapi", "GetResources", "Tagged resource", items_path="ResourceTagMappingList")

# Root operations that list catalogues, history or settings rather than resources.
_DEEP_SKIP_RE = re.compile(
    r"(Types|Versions|Offerings|Options|Regions|Zones|Engines|Prices|Pricing|Quotas|Limits|Defaults|"
    r"Categories|Recommendations|Events|History|Metrics|Findings|Jobs|Executions|Reports|Logs|Operations|"
    r"Languages|Voices|Catalogs?|Products|Features|Capabilities|Settings|Configurations?|Attributes|"
    r"Parameters|Images|Snapshots|Status|Statuses|Summaries|Usage|Locations|Partitions|Providers|Runtimes|"
    r"Platforms|Countries|Currencies|Tags|Permissions|Policies|Notifications|Mappings|Resources|Insights|"
    r"Activities|Tasks|Requests|Suggestions|Endpoints?Config)$"
)


@dataclass
class FoundResource:
    region: str
    service: str
    resource_type: str
    name: str
    identifier: str
    state: str
    arn: str
    operation: str
    items_key: str | None
    params: dict[str, Any]
    raw: Any
    source: str = "inventory"  # inventory | tagged | deep
    call_region: str = ""      # region the API call went to

    def item_context(self) -> ItemContext:
        return ItemContext(self.service, self.operation, dict(self.params), self.items_key, region=self.call_region or None)

    def as_dict(self, include_raw: bool = True) -> dict[str, Any]:
        d = {
            "region": self.region,
            "service": self.service,
            "type": self.resource_type,
            "name": self.name,
            "id": self.identifier,
            "state": self.state,
            "arn": self.arn,
            "source": self.source,
            "operation": self.operation,
        }
        if include_raw:
            d["raw"] = self.raw
        return d


@dataclass
class ScanError:
    region: str
    service: str
    operation: str
    kind: str  # denied | unavailable | error
    message: str


@dataclass
class ScanResult:
    regions: list[str]
    mode: str
    resources: list[FoundResource] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    calls: int = 0
    seconds: float = 0.0
    cancelled: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "regions": self.regions,
            "mode": self.mode,
            "resources": len(self.resources),
            "calls": self.calls,
            "seconds": round(self.seconds, 1),
            "cancelled": self.cancelled,
            "by_region": dict(Counter(r.region for r in self.resources).most_common()),
            "by_type": dict(Counter(f"{r.service}: {r.resource_type}" for r in self.resources).most_common()),
            "errors": dict(Counter(e.kind for e in self.errors)),
        }

    def to_json(self) -> str:
        return to_json(
            {
                "summary": self.summary(),
                "resources": [r.as_dict() for r in self.resources],
                "errors": [e.__dict__ for e in self.errors],
            }
        )

    def to_csv(self) -> str:
        buf = io.StringIO()
        fields = ["region", "service", "type", "name", "id", "state", "arn", "source", "operation"]
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        for r in self.resources:
            writer.writerow(r.as_dict(include_raw=False))
        return buf.getvalue()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def classify_error(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if "AccessDenied" in code or code in ("UnauthorizedOperation", "AuthorizationError", "Forbidden", "AccessDeniedException") or status == 403:
            if code in ("UnrecognizedClientException", "InvalidClientTokenId"):
                return "unavailable"
            return "denied"
        if code in (
            "OptInRequired",
            "SubscriptionRequiredException",
            "UnrecognizedClientException",
            "InvalidClientTokenId",
            "UnsupportedOperation",
            "InvalidAction",
            "UnknownOperationException",
            "UnsupportedOperationException",
            "NotSupportedException",
        ) or status == 404:
            return "unavailable"
        return "error"
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, NoRegionError)):
        return "unavailable"
    if isinstance(exc, ReadTimeoutError):
        return "error"
    return "error"


def short_error(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', 'Error')}: {err.get('Message', '')}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


def _first(item: dict[str, Any], keys: Iterable[str]) -> str:
    for k in keys:
        v = item.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v):
            return str(v)
    return ""


# "...Name" fields that describe something other than the resource itself.
_NOT_NAME_WORDS = ("dns", "device", "keyname", "groupname", "zone", "profile", "owner", "type", "family", "class", "engine", "role")


def _tail(value: str) -> str:
    """Last path segment of an ARN or URL, which is usually the resource's name."""
    if value.startswith(("arn:", "http://", "https://")):
        return re.split(r"[/:]", value.rstrip("/"))[-1] or value
    return value


def describe_item(item: Any, nouns: set[str]) -> tuple[str, str, str, str]:
    """``(name, identifier, state, arn)`` for display."""
    if not isinstance(item, dict):
        return str(item), str(item), "", ""
    keys = list(item.keys())
    lower = {k.lower(): k for k in keys}

    def noun_keys(suffixes: tuple[str, ...]) -> list[str]:
        out = []
        for n in sorted(nouns, key=len, reverse=True):
            for suf in suffixes:
                if n + suf in lower:
                    out.append(lower[n + suf])
        return out

    name = tag_name(item) or _first(item, noun_keys(("name",)) + [k for k in keys if k.lower() in ("name", "title", "alias", "key")])
    if not name:
        name = _first(item, [k for k in keys if k.lower().endswith("name") and not any(w in k.lower() for w in _NOT_NAME_WORDS)])
    ident = _first(item, noun_keys(("id", "identifier")) + [k for k in keys if k.lower() in ("id", "identifier")])
    if not ident:
        ident = _first(item, [k for k in keys if k.lower().endswith(("id", "identifier"))])
    arn = ""
    for k in noun_keys(("arn",)) + [k for k in keys if k.lower().endswith("arn")]:
        v = item.get(k)
        if isinstance(v, str) and v.startswith("arn:"):
            arn = v
            break
    if not arn:
        for k in keys:
            v = item.get(k)
            if isinstance(v, str) and v.startswith("arn:"):
                arn = v
                break
    if not ident:
        ident = arn or _first(item, [k for k in keys if k.lower().endswith("url")])
    if not name:
        name = _tail(ident)
    if not ident:
        ident = name
    state = ""
    for k in keys:
        kl = k.lower()
        if kl in ("state", "status") or kl.endswith(("state", "status")):
            v = cell_value(item[k])
            if isinstance(v, (str, int)):
                state = str(v)
                break
    return name, ident, state, arn


def _skip(target: Target, item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    for path, values in target.skip_if.items():
        if jmespath.search(path, item) in values:
            return True
    for path, prefixes in target.skip_prefix.items():
        value = jmespath.search(path, item)
        if isinstance(value, str) and any(value.startswith(p) for p in prefixes):
            return True
    if any(jmespath.search(path, item) for path in target.skip_present):
        return True
    _n, _i, state, _a = describe_item(item, set())
    return state.lower() in GONE_STATES


def _tagged_resource(region: str, item: dict[str, Any]) -> FoundResource | None:
    arn = item.get("ResourceARN", "")
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return None
    service, res_region, resource = parts[2], parts[3] or GLOBAL_REGION, parts[5]
    rtype, _sep, rid = resource.partition("/") if "/" in resource else resource.partition(":")
    name = tag_name(item) or rid or resource
    return FoundResource(
        region=res_region,
        service=service,
        resource_type=f"{service} {rtype}" if rid else service,
        name=name,
        identifier=rid or resource,
        state="",
        arn=arn,
        operation=TAGGING_TARGET.operation,
        items_key=TAGGING_TARGET.items_path,
        params={},
        raw=item,
        source="tagged",
        call_region=region,
    )


def _known_tokens(resources: Iterable[FoundResource]) -> set[str]:
    tokens: set[str] = set()
    for r in resources:
        for v in (r.arn, r.identifier, r.name):
            if v:
                tokens.add(v)
        if isinstance(r.raw, dict):
            for v in r.raw.values():
                if isinstance(v, str) and len(v) > 3:
                    tokens.add(v)
    return tokens


def _tag_covered(tagged: FoundResource, tokens: set[str]) -> bool:
    if tagged.arn in tokens:
        return True
    tail = re.split(r"[/:]", tagged.arn)[-1]
    return bool(tail) and tail in tokens


# ---------------------------------------------------------------------------
# scanner
# ---------------------------------------------------------------------------
@dataclass
class ScanPlan:
    """Calls grouped by (region, service) so each group needs one client."""

    groups: list[tuple[str, str, list[Target]]]
    global_regions: dict[str, str]

    @property
    def total_calls(self) -> int:
        return sum(len(t) for _r, _s, t in self.groups)


class Scanner:
    def __init__(
        self,
        ctx,
        regions: list[str],
        *,
        deep: bool = False,
        include_global: bool = True,
        include_tagged: bool = True,
        services: list[str] | None = None,
        workers: int = 16,
        max_items: int = SCAN_MAX_ITEMS,
    ) -> None:
        self.ctx = ctx
        self.regions = list(dict.fromkeys(regions))
        self.deep = deep
        self.include_global = include_global
        self.include_tagged = include_tagged
        self.service_patterns = [s.strip() for s in (services or []) if s.strip()]
        self.workers = max(1, workers)
        self.max_items = max_items
        self.cancel_event = threading.Event()
        self._endpoints = self._load_endpoints()

    # -- planning ------------------------------------------------------------
    def _load_endpoints(self) -> dict[str, Any]:
        try:
            data = self.ctx.session._session.get_data("endpoints")  # botocore partition data
            return data["partitions"][0]["services"]
        except Exception:  # noqa: BLE001
            return {}

    def _wanted(self, service: str) -> bool:
        if not self.service_patterns:
            return True
        return any(fnmatch(service, p) for p in self.service_patterns)

    def home_region(self, service: str) -> str | None:
        """Region to call a global service in, or ``None`` if the service is regional."""
        info = self._endpoints.get(service, {})
        if info.get("isRegionalized") is False:
            pe = info.get("partitionEndpoint", "")
            scope = info.get("endpoints", {}).get(pe, {}).get("credentialScope", {})
            return scope.get("region", "us-east-1")
        return None

    def offered_in(self, service: str, region: str) -> bool:
        info = self._endpoints.get(service)
        if not info or not info.get("endpoints"):
            return True  # unknown to endpoint data: just try it
        return region in info["endpoints"]

    def deep_targets(self) -> list[Target]:
        catalog = self.ctx.catalog
        out = []
        for service in catalog.services():
            if not self._wanted(service):
                continue
            try:
                ops = catalog.root_operations(service)
            except Exception:  # noqa: BLE001
                continue
            for op in ops:
                if operation_prefix(op) not in ("List", "Describe") or (service, op) in INVENTORY_KEYS:
                    continue
                if _DEEP_SKIP_RE.search(op):
                    continue
                out.append(Target(service, op, global_=self.home_region(service) is not None))
        return out

    def plan(self) -> ScanPlan:
        targets = [t for t in INVENTORY if self._wanted(t.service)]
        if self.deep:
            targets += self.deep_targets()
        groups: dict[tuple[str, str], list[Target]] = {}
        global_regions: dict[str, str] = {}
        for t in targets:
            if t.global_:
                if not self.include_global:
                    continue
                region = self.home_region(t.service) or "us-east-1"
                global_regions[t.service] = region
                groups.setdefault((region, t.service), []).append(t)
                continue
            for region in self.regions:
                if self.offered_in(t.service, region):
                    groups.setdefault((region, t.service), []).append(t)
        if self.include_tagged:
            for region in self.regions:
                groups.setdefault((region, TAGGING_TARGET.service), []).append(TAGGING_TARGET)
        ordered = [(r, s, ts) for (r, s), ts in groups.items()]
        # Biggest groups first keeps the pool busy until the end.
        ordered.sort(key=lambda g: -len(g[2]))
        return ScanPlan(ordered, global_regions)

    # -- running -------------------------------------------------------------
    def cancel(self) -> None:
        self.cancel_event.set()

    def run(
        self,
        on_progress: Callable[[int, int, list[FoundResource], list[ScanError]], None] | None = None,
    ) -> ScanResult:
        """Run the scan.  ``on_progress(done, total, new_resources, new_errors)`` is called from worker threads."""
        plan = self.plan()
        total = plan.total_calls
        result = ScanResult(self.regions, "deep" if self.deep else "standard")
        lock = threading.Lock()
        done = 0
        tagged: list[FoundResource] = []
        filtered_tokens: set[str] = set()  # ids of defaults / dead resources we deliberately dropped
        started = time.monotonic()

        def run_group(region: str, service: str, targets: list[Target]) -> None:
            nonlocal done
            client = None
            for target in targets:
                if self.cancel_event.is_set():
                    return
                found: list[FoundResource] = []
                errors: list[ScanError] = []
                dropped: set[str] = set()
                try:
                    if client is None:
                        client = self.ctx.new_client(service, region)
                    found, dropped = self._run_target(client, region, target)
                except Exception as exc:  # noqa: BLE001
                    errors.append(ScanError(region, service, target.operation, classify_error(exc), short_error(exc)))
                with lock:
                    done += 1
                    filtered_tokens.update(dropped)
                    if target is TAGGING_TARGET:
                        tagged.extend(found)
                        found = []
                    result.resources.extend(found)
                    result.errors.extend(errors)
                    progress = done
                if on_progress:
                    on_progress(progress, total, found, errors)
                if errors and errors[0].kind == "unavailable" and target is not TAGGING_TARGET:
                    # The whole service is unreachable here; skip its other calls.
                    with lock:
                        skipped = len(targets) - targets.index(target) - 1
                        done += skipped
                    if on_progress and skipped:
                        on_progress(done, total, [], [])
                    return

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="awsnav-scan") as pool:
            futures = [pool.submit(run_group, r, s, ts) for r, s, ts in plan.groups]
            for fut in as_completed(futures):
                if not fut.cancelled():
                    fut.result()
                if self.cancel_event.is_set():
                    for f in futures:
                        f.cancel()

        # Tagged resources that the inventory did not already report.
        tokens = _known_tokens(result.resources) | filtered_tokens
        extra = [t for t in tagged if not _tag_covered(t, tokens) and t.region in set(self.regions) | {GLOBAL_REGION}]
        result.resources.extend(extra)
        if on_progress and extra:
            on_progress(done, total, extra, [])

        result.resources.sort(key=lambda r: (r.region, r.service, r.resource_type, r.name))
        result.calls = done
        result.seconds = time.monotonic() - started
        result.cancelled = self.cancel_event.is_set()
        return result

    def _run_target(self, client, region: str, target: Target) -> tuple[list[FoundResource], set[str]]:
        """Resources found by one call, plus identifiers of items filtered out as defaults or dead."""
        res = self.ctx.run_operation(client, target.service, target.operation, dict(target.params), self.max_items)
        if target.items_path:
            items = jmespath.search(target.items_path, res.raw) or []
            items_key = target.items_path
        else:
            items, items_key = res.items, res.items_key
        if not items:
            return [], set()
        if target is TAGGING_TARGET:
            return [r for r in (_tagged_resource(region, i) for i in items) if r], set()
        ctx = ItemContext(target.service, target.operation, dict(target.params), items_key, region=region)
        nouns = ctx.nouns()
        out = []
        dropped: set[str] = set()
        selected = set(self.regions)
        account = (getattr(self.ctx, "identity", None) or {}).get("Account")
        for item in items:
            name, ident, state, arn = describe_item(item, nouns)
            foreign = target.own_only and account and isinstance(item, dict) and item.get("OwnerId") not in (None, account)
            if foreign or _skip(target, item):
                dropped.update(v for v in (ident, arn) if v)
                continue
            item_region = region
            if target.global_:
                item_region = GLOBAL_REGION
                bucket_region = item.get("BucketRegion") if isinstance(item, dict) else None
                if isinstance(bucket_region, str) and bucket_region:
                    if bucket_region not in selected:
                        continue
                    item_region = bucket_region
            out.append(
                FoundResource(
                    region=item_region,
                    service=target.service,
                    resource_type=target.resource_type,
                    name=name,
                    identifier=ident,
                    state=state,
                    arn=arn,
                    operation=target.operation,
                    items_key=items_key,
                    params=dict(target.params),
                    raw=item,
                    source="inventory" if (target.service, target.operation) in INVENTORY_KEYS else "deep",
                    call_region=region,
                )
            )
        return out, dropped


def enabled_regions(ctx) -> list[str]:
    """Regions enabled for the account (opt-in regions only when opted in)."""
    try:
        resp = ctx.client("ec2").describe_regions(AllRegions=False)
        regions = sorted(r["RegionName"] for r in resp.get("Regions", []))
        if regions:
            return regions
    except Exception:  # noqa: BLE001
        pass
    return ctx.regions()


def format_table(result: ScanResult, limit: int | None = None) -> str:
    rows = [("REGION", "SERVICE", "TYPE", "NAME", "ID", "STATE")]
    for r in result.resources[:limit]:
        rows.append((r.region, r.service, r.resource_type, r.name[:50], r.identifier[:60], r.state))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows)


__all__ = [
    "INVENTORY",
    "FoundResource",
    "ScanError",
    "ScanResult",
    "Scanner",
    "Target",
    "enabled_regions",
    "format_table",
]
