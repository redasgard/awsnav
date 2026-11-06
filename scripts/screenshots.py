"""Capture the README screenshots and demo-GIF frames as SVG files.

Everything runs against moto (a local AWS mock) seeded with a small demo
account; Bedrock calls are stubbed because moto does not implement them.
Nothing touches real AWS.

    pip install -e '.[dev]'
    python scripts/screenshots.py build/shots      # writes *.svg (+ frames/)
    node scripts/render.mjs build/shots docs       # PNGs, demo.gif, social preview

"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
    os.environ.pop(var, None)

import boto3  # noqa: E402
from botocore.stub import Stubber  # noqa: E402
from moto import mock_aws  # noqa: E402

import awsnav.scanner as scanner_mod  # noqa: E402
from awsnav.session import AwsContext, ConnectionSettings  # noqa: E402
from awsnav.ui.app import AwsNavApp  # noqa: E402

SIZE = (150, 40)
REGIONS = ["us-east-1", "eu-west-1", "ap-southeast-2"]
DEMO_IDENTITY = {"UserId": "AIDADEMO000000000001", "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/demo"}


# ---------------------------------------------------------------------------
# demo account
# ---------------------------------------------------------------------------
def tag(name: str, **extra: str) -> list[dict[str, str]]:
    return [{"Key": "Name", "Value": name}] + [{"Key": k, "Value": v} for k, v in extra.items()]


def instances(region: str, specs: list[tuple[str, str]], stopped: tuple[str, ...] = ()) -> None:
    ec2 = boto3.client("ec2", region_name=region)
    for name, itype in specs:
        r = ec2.run_instances(
            ImageId="ami-12345678",
            InstanceType=itype,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[{"ResourceType": "instance", "Tags": tag(name, env="prod", team="platform")}],
        )
        if name in stopped:
            ec2.stop_instances(InstanceIds=[r["Instances"][0]["InstanceId"]])


def seed() -> None:
    instances(
        "us-east-1",
        [("checkout-api-1", "m6i.large"), ("checkout-api-2", "m6i.large"), ("search-indexer", "r6i.xlarge"),
         ("bastion", "t3.micro"), ("legacy-batch", "c5.2xlarge")],
        stopped=("legacy-batch",),
    )
    instances("eu-west-1", [("checkout-api-eu-1", "m6i.large"), ("checkout-api-eu-2", "m6i.large")])
    instances("ap-southeast-2", [("edge-cache-syd", "c6g.large")])

    ec2 = boto3.client("ec2", region_name="us-east-1")
    vpc = ec2.create_vpc(CidrBlock="10.20.0.0/16", TagSpecifications=[{"ResourceType": "vpc", "Tags": tag("prod-vpc")}])["Vpc"]
    for i, az in enumerate(("us-east-1a", "us-east-1b")):
        ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock=f"10.20.{i}.0/24", AvailabilityZone=az,
                          TagSpecifications=[{"ResourceType": "subnet", "Tags": tag(f"prod-private-{az[-1]}")}])
    ec2.create_security_group(GroupName="checkout-api", Description="checkout API", VpcId=vpc["VpcId"])

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="acme-web-assets")
    s3.create_bucket(Bucket="acme-data-lake")
    boto3.client("s3", region_name="eu-west-1").create_bucket(
        Bucket="acme-data-eu", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
    )
    files = {
        "index.html": b"<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>Acme Store</title>\n"
        b"  <link rel=\"stylesheet\" href=\"/css/site.css\">\n</head>\n<body>\n  <main id=\"app\"></main>\n"
        b"  <script type=\"module\" src=\"/js/app.js\"></script>\n</body>\n</html>\n",
        "robots.txt": b"User-agent: *\nAllow: /\n",
        "css/site.css": b"body{font-family:system-ui}" * 40,
        "js/app.js": b"import {render} from './ui.js';\nrender(document.getElementById('app'));\n" * 30,
        "js/ui.js": b"export function render(el){el.textContent='Acme';}\n" * 20,
        "img/hero.webp": b"\x00RIFF" + b"\x01" * 48_000,
        "img/logo.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        "releases/2026-11-04/manifest.json": b"{}",
        "releases/2026-11-05/manifest.json": b"{}",
    }
    for key, body in files.items():
        s3.put_object(Bucket="acme-web-assets", Key=key, Body=body)

    for region, tables in (("us-east-1", ["orders", "carts", "sessions"]), ("eu-west-1", ["orders-eu"])):
        ddb = boto3.client("dynamodb", region_name=region)
        for t in tables:
            ddb.create_table(TableName=t, KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    for region, queues in (("us-east-1", ["order-events", "order-events-dlq", "email-outbox"]), ("eu-west-1", ["order-events-eu"])):
        sqs = boto3.client("sqs", region_name=region)
        for q in queues:
            sqs.create_queue(QueueName=q)
    boto3.client("sns", region_name="us-east-1").create_topic(Name="deploy-alerts")

    iam = boto3.client("iam")
    for u in ("alice", "bob", "ci-deploy"):
        iam.create_user(UserName=u)
    role = iam.create_role(
        RoleName="lambda-runtime",
        AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}',
    )["Role"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("handler.py", "def handler(event, context):\n    return {'ok': True}\n")
    lam = boto3.client("lambda", region_name="us-east-1")
    for fn in ("resize-image", "send-receipt", "nightly-report"):
        lam.create_function(FunctionName=fn, Runtime="python3.12", Role=role["Arn"], Handler="handler.handler",
                            Code={"ZipFile": buf.getvalue()}, MemorySize=512, Timeout=30)
    boto3.client("rds", region_name="us-east-1").create_db_instance(
        DBInstanceIdentifier="orders-db", Engine="postgres", DBInstanceClass="db.r6g.large",
        AllocatedStorage=100, MasterUsername="app", MasterUserPassword="not-a-real-password",
    )
    boto3.client("secretsmanager", region_name="us-east-1").create_secret(Name="prod/payments/api-key", SecretString="demo")
    logs = boto3.client("logs", region_name="us-east-1")
    for fn in ("resize-image", "send-receipt"):
        logs.create_log_group(logGroupName=f"/aws/lambda/{fn}")


FOUNDATION_MODELS = [
    ("amazon.nova-pro-v1:0", "Nova Pro", "Amazon"),
    ("amazon.nova-lite-v1:0", "Nova Lite", "Amazon"),
    ("amazon.titan-text-express-v1", "Titan Text G1 - Express", "Amazon"),
    ("anthropic.claude-3-5-haiku-20241022-v1:0", "Claude 3.5 Haiku", "Anthropic"),
    ("cohere.command-r-plus-v1:0", "Command R+", "Cohere"),
    ("meta.llama3-1-70b-instruct-v1:0", "Llama 3.1 70B Instruct", "Meta"),
    ("mistral.mistral-large-2407-v1:0", "Mistral Large (24.07)", "Mistral AI"),
]


def stub_bedrock(ctx: AwsContext) -> None:
    models = {
        "modelSummaries": [
            {
                "modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{mid}",
                "modelId": mid,
                "modelName": name,
                "providerName": provider,
                "inputModalities": ["TEXT"],
                "outputModalities": ["TEXT"],
                "responseStreamingSupported": True,
                "inferenceTypesSupported": ["ON_DEMAND"],
                "modelLifecycle": {"status": "ACTIVE"},
            }
            for mid, name, provider in FOUNDATION_MODELS
        ]
    }
    bedrock = Stubber(ctx.client("bedrock"))
    for _ in range(4):
        bedrock.add_response("list_foundation_models", models)
    bedrock.activate()
    runtime = Stubber(ctx.client("bedrock-runtime"))
    for text in (
        "Here is a short checklist before you resize the checkout fleet:\n\n"
        "1. Check CPU and memory on checkout-api-1 and -2 over the last 14 days.\n"
        "2. Confirm both instances sit behind the same target group.\n"
        "3. Resize one at a time and watch the 5xx rate between them.",
    ):
        runtime.add_response(
            "converse",
            {
                "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 41, "outputTokens": 74, "totalTokens": 115},
                "metrics": {"latencyMs": 812},
            },
        )
    runtime.activate()


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------
async def wait_for(pilot, cond, timeout: float = 120.0) -> None:
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if cond():
            return
    raise RuntimeError(f"timed out on {type(pilot.app.screen).__name__}")


class Recorder:
    def __init__(self, app: AwsNavApp, out: Path) -> None:
        self.app = app
        self.out = out
        self.frames: list[tuple[str, float]] = []
        (out / "frames").mkdir(parents=True, exist_ok=True)

    def shot(self, name: str) -> None:
        self.app.save_screenshot(f"{name}.svg", path=str(self.out))
        print("  shot", name)

    def frame(self, seconds: float) -> None:
        name = f"f{len(self.frames):03d}"
        self.app.save_screenshot(f"{name}.svg", path=str(self.out / "frames"))
        self.frames.append((name, seconds))

    async def type(self, pilot, text: str, per_char: float = 0.12) -> None:
        for ch in text:
            await pilot.press(ch)
            await pilot.pause(0.05)
            self.frame(per_char)

    def write_timeline(self) -> None:
        lines = [f"{name} {seconds}" for name, seconds in self.frames]
        (self.out / "frames" / "timeline.txt").write_text("\n".join(lines) + "\n")


def moto_supported_inventory(ctx: AwsContext) -> list:
    """Drop inventory calls moto has not implemented, so the demo scan shows no mock-only failures."""
    probe = scanner_mod.Scanner(ctx, ["us-east-1"], include_tagged=False, workers=8).run()
    failing = {(e.service, e.operation) for e in probe.errors}
    return [t for t in scanner_mod.INVENTORY if (t.service, t.operation) not in failing]


async def capture(out: Path) -> None:
    from awsnav.ui.bedrock import ChatScreen
    from awsnav.ui.s3 import S3BrowserScreen
    from awsnav.ui.scan import ScanResultsScreen, ScanSetupScreen
    from awsnav.ui.screens import DetailScreen, OperationsScreen, ResultScreen, RunnerScreen, ServicesScreen

    real_verify = AwsContext.verify

    def demo_verify(self):  # shows a friendlier identity than moto's default user
        real_verify(self)
        self.identity = dict(DEMO_IDENTITY)
        return self.identity

    AwsContext.verify = demo_verify  # type: ignore[method-assign]
    scanner_mod.INVENTORY[:] = moto_supported_inventory(AwsContext(ConnectionSettings()))

    app = AwsNavApp(ConnectionSettings(access_key="testing", secret_key="testing"), auto_connect=True)
    async with app.run_test(size=SIZE) as pilot:
        rec = Recorder(app, out)
        await wait_for(pilot, lambda: isinstance(app.screen, ServicesScreen) and app.screen.table.row_count)
        app.ctx.catalog.load_full_names()
        app.screen._populate()
        await pilot.pause(0.3)
        stub_bedrock(app.ctx)

        # --- demo: services -> ec2 -> instances -> details ----------------
        rec.frame(1.6)
        rec.shot("services")
        await rec.type(pilot, "ec2")
        rec.frame(0.8)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, OperationsScreen))
        rec.frame(1.2)
        await rec.type(pilot, "instances", per_char=0.08)
        rec.frame(0.8)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.result is not None)
        await pilot.pause(0.2)
        rec.frame(2.4)
        rec.shot("resources")
        await pilot.press("down")
        rec.frame(0.6)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        await pilot.pause(0.2)
        rec.frame(2.6)
        rec.shot("detail")
        for _ in range(4):
            await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ServicesScreen))

        # --- demo: scan three regions ---------------------------------------
        await pilot.press("f2")
        await wait_for(pilot, lambda: isinstance(app.screen, ScanSetupScreen) and app.screen.query_one("#regions").option_count)
        setup = app.screen
        setup.query_one("#regions").scroll_to(y=0, animate=False)
        for region in REGIONS[1:]:
            setup.query_one("#regions").select(region)
        await wait_for(pilot, lambda: "API calls" in str(setup.query_one("#estimate").render()))
        await pilot.pause(0.2)
        rec.frame(2.2)
        rec.shot("scan-setup")
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: isinstance(app.screen, ScanResultsScreen))
        results = app.screen
        await wait_for(pilot, lambda: results._found > 8 or results.result is not None)
        rec.frame(1.0)
        await wait_for(pilot, lambda: results.result is not None, timeout=600)
        await pilot.pause(0.3)
        rec.frame(2.8)
        rec.shot("hero")
        await pilot.press("slash")
        await rec.type(pilot, "eu-west-1", per_char=0.07)
        rec.frame(2.4)
        results.query_one("#filter").value = ""
        results.table.focus()
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ScanSetupScreen))
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ServicesScreen))

        # --- S3 browser ----------------------------------------------------
        app.push_screen(S3BrowserScreen("acme-web-assets"))
        await wait_for(pilot, lambda: isinstance(app.screen, S3BrowserScreen) and app.screen.table.row_count)
        await pilot.pause(0.2)
        rec.shot("s3")
        await pilot.press("escape")

        # --- Bedrock -------------------------------------------------------
        app.push_screen(ResultScreen("bedrock", "ListFoundationModels"))
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.result is not None)
        await pilot.pause(0.2)
        rec.frame(1.8)
        await pilot.press("down", "down", "down")
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        await pilot.pause(0.2)
        rec.frame(1.8)
        rec.shot("bedrock-model")
        app.push_screen(ChatScreen("us.amazon.nova-lite-v1:0"))
        await wait_for(pilot, lambda: isinstance(app.screen, ChatScreen))
        chat = app.screen
        chat.query_one("#system").value = "You are a concise AWS operations assistant."
        chat.query_one("#prompt").focus()
        await rec.type(pilot, "We want to resize the checkout fleet. What should we check first?", per_char=0.03)
        await pilot.press("enter")
        await wait_for(pilot, lambda: len(chat.messages) == 2)
        await pilot.pause(0.3)
        rec.frame(3.2)
        rec.shot("bedrock-chat")
        await pilot.press("escape", "escape", "escape")

        # --- operation runner ------------------------------------------------
        app.push_screen(RunnerScreen("lambda", "GetFunctionConfiguration", {"FunctionName": "resize-image"}))
        await wait_for(pilot, lambda: isinstance(app.screen, RunnerScreen))
        await pilot.pause(0.2)
        rec.shot("runner")
        rec.write_timeline()


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/shots")
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("XDG_CACHE_HOME", str(out / "cache"))
    with mock_aws():
        seed()
        asyncio.run(capture(out))
    print(f"SVGs written to {out}")


if __name__ == "__main__":
    main()
