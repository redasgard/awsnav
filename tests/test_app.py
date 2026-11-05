"""End-to-end UI tests: the real Textual app driven by a pilot against moto."""

import boto3
from botocore.stub import Stubber

from awsnav.session import ConnectionSettings
from awsnav.ui.app import AwsNavApp
from awsnav.ui.bedrock import ChatScreen
from awsnav.ui.common import ConfirmModal, HelpModal, PickerModal, TextViewer
from awsnav.ui.s3 import S3BrowserScreen
from awsnav.ui.screens import DetailScreen, LoginScreen, OperationsScreen, ResultScreen, RunnerScreen, ServicesScreen

SIZE = (160, 45)


async def wait_for(pilot, cond, timeout=15.0):
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if cond():
            return
    status = pilot.app.screen.query("StatusLine")
    detail = f" status: {status.first().render()}" if status else ""
    raise AssertionError(f"condition not met; screen is {type(pilot.app.screen).__name__}{detail}")


def make_app(**kw):
    return AwsNavApp(ConnectionSettings(access_key="testing", secret_key="testing", **kw), auto_connect=True)


async def connected(pilot):
    # Wait until the screen is composed, not merely pushed.
    await wait_for(
        pilot,
        lambda: isinstance(pilot.app.screen, ServicesScreen) and pilot.app.screen.query("TopBar") and pilot.app.screen.table.row_count,
        timeout=45,
    )


async def test_login_form_validates_and_connects(aws):
    app = AwsNavApp(ConnectionSettings(), auto_connect=False)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, LoginScreen))
        app.screen.query_one("#secret_key").value = "only-secret"
        await pilot.click("#connect")
        await pilot.pause(0.1)
        assert "both" in str(app.screen.query_one("StatusLine").render())
        app.screen.query_one("#access_key").value = "AKIATEST"
        app.screen.query_one("#region").value = "eu-central-1"
        app.screen.query_one("#connect").press()
        await connected(pilot)
        assert app.ctx.region == "eu-central-1"
        assert "123456789012" in str(app.screen.query_one("TopBar").render())


async def test_ec2_list_detail_and_drilldown(aws):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    ec2.run_instances(
        ImageId="ami-12345678",
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "web-[1]"}]}],
    )
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        await pilot.press(*"ec2", "enter")
        await wait_for(pilot, lambda: isinstance(app.screen, OperationsScreen))
        await pilot.press(*"describeinstances", "enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.result is not None)
        screen = app.screen
        assert screen.operation == "DescribeInstances"
        assert screen._columns[:2] == ["Name (tag)", "InstanceId"]
        assert screen.table.row_count == 1
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        detail = app.screen
        ops = [getattr(a, "operation", None) for a in detail._actions.values()]
        assert "DescribeInstanceStatus" in ops
        detail.query_one("#actions").highlighted = ops.index("DescribeInstanceStatus")
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.operation == "DescribeInstanceStatus" and app.screen.result)
        assert app.screen.result.params["InstanceIds"]
        await pilot.press("v")
        await wait_for(pilot, lambda: isinstance(app.screen, TextViewer))
        for _ in range(3):
            await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.operation == "DescribeInstances")


async def test_s3_browser_preview_and_object_actions(aws):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="bucket-one")
    s3.put_object(Bucket="bucket-one", Key="docs/readme.txt", Body=b"hello from s3")
    s3.put_object(Bucket="bucket-one", Key="top.json", Body=b"{}")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        app.open_service("s3")
        await wait_for(pilot, lambda: isinstance(app.screen, OperationsScreen))
        await pilot.press("enter")  # ListBuckets is first
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.result is not None)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        await pilot.press("enter")  # "Browse objects" is the first action
        await wait_for(pilot, lambda: isinstance(app.screen, S3BrowserScreen) and app.screen.table.row_count == 2)
        await pilot.press("enter")  # docs/
        await wait_for(pilot, lambda: isinstance(app.screen, S3BrowserScreen) and app.screen.prefix == "docs/" and app.screen.table.row_count == 1)
        await pilot.press("p")
        await wait_for(pilot, lambda: isinstance(app.screen, TextViewer))
        assert "hello from s3" in app.screen._text()
        await pilot.press("escape")
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        labels = [getattr(a, "label", "") for a in app.screen._actions.values()]
        assert "Download object…" in labels
        assert any(getattr(a, "operation", "") == "HeadObject" for a in app.screen._actions.values())


async def test_write_operations_blocked_in_read_only_mode(aws):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        app.open_runner("sqs", operation="CreateQueue")
        await wait_for(pilot, lambda: isinstance(app.screen, RunnerScreen))
        app.screen.query_one("#params").text = '{"QueueName": "nope"}'
        await pilot.press("ctrl+r")
        await pilot.pause(0.2)
        assert "Read-only" in str(app.screen.query_one("StatusLine").render())
    assert boto3.client("sqs", region_name="us-east-1").list_queues().get("QueueUrls") is None


async def test_write_operation_requires_confirmation(aws):
    app = make_app(allow_write=True)
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        app.open_runner("sqs", operation="CreateQueue")
        await wait_for(pilot, lambda: isinstance(app.screen, RunnerScreen))
        app.screen.query_one("#params").text = '{"QueueName": "made-by-awsnav"}'
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.click("#yes")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen))
    urls = boto3.client("sqs", region_name="us-east-1").list_queues()["QueueUrls"]
    assert any(u.endswith("/made-by-awsnav") for u in urls)


async def test_runner_any_read_operation_with_parameters(aws):
    boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="alpha")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        app.open_service("sqs")
        await wait_for(pilot, lambda: isinstance(app.screen, OperationsScreen))
        app.screen.table.focus()
        await pilot.press("a")  # all operations
        await pilot.press("slash", *"getqueueurl", "enter")
        await wait_for(pilot, lambda: isinstance(app.screen, RunnerScreen))
        app.screen.query_one("#params").text = '{"QueueName": "alpha"}'
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen))
        assert app.screen.item["QueueUrl"].endswith("/alpha")


async def test_region_picker_help_and_error_display(aws):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        await pilot.press("ctrl+g")
        await wait_for(pilot, lambda: isinstance(app.screen, PickerModal))
        await pilot.press(*"ap-southeast-2", "enter")
        await wait_for(pilot, lambda: app.ctx.region == "ap-southeast-2")
        assert "ap-southeast-2" in str(app.screen.query_one("TopBar").render())
        await pilot.press("f1")
        await wait_for(pilot, lambda: isinstance(app.screen, HelpModal))
        await pilot.press("escape")
        app.push_screen(ResultScreen("sts", "GetCallerIdentity"))
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen))
        assert app.screen.item["Account"] == "123456789012"
        app.push_screen(ResultScreen("s3", "ListObjectsV2", {"Bucket": "does-not-exist"}))
        await wait_for(pilot, lambda: "NoSuchBucket" in str(app.screen.query_one("StatusLine").render()))


async def test_bedrock_chat_with_stubbed_runtime(aws):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        client = app.ctx.client("bedrock-runtime")
        stub = Stubber(client)
        stub.add_response(
            "converse",
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "Hello [human]!"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
                "metrics": {"latencyMs": 12},
            },
            {
                "modelId": "us.anthropic.test-model",
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                "inferenceConfig": {"maxTokens": 2048},
            },
        )
        stub.activate()
        await pilot.press("ctrl+b")
        await wait_for(pilot, lambda: isinstance(app.screen, ChatScreen))
        app.screen.query_one("#model").value = "us.anthropic.test-model"
        app.screen.query_one("#prompt").focus()
        await pilot.press("h", "i", "enter")
        await wait_for(pilot, lambda: len(app.screen.messages) == 2)
        assert app.screen.messages[1]["content"][0]["text"] == "Hello [human]!"
        assert "5/3" in str(app.screen.query_one("StatusLine").render())
        stub.assert_no_pending_responses()


async def test_bedrock_foundation_model_offers_chat(aws):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        stub = Stubber(app.ctx.client("bedrock"))
        stub.add_response(
            "list_foundation_models",
            {
                "modelSummaries": [
                    {
                        "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.test",
                        "modelId": "anthropic.test",
                        "modelName": "Test",
                        "providerName": "Anthropic",
                        "outputModalities": ["TEXT"],
                        "inferenceTypesSupported": ["ON_DEMAND"],
                    }
                ]
            },
            {},
        )
        stub.activate()
        app.push_screen(ResultScreen("bedrock", "ListFoundationModels"))
        await wait_for(pilot, lambda: isinstance(app.screen, ResultScreen) and app.screen.result is not None)
        assert app.screen._columns[0] in ("modelName", "modelId")
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        actions = list(app.screen._actions.values())
        assert actions[0].label.startswith("Chat with anthropic.test")
        assert any(getattr(a, "operation", "") == "GetFoundationModel" for a in actions)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ChatScreen))
        assert app.screen.query_one("#model").value == "anthropic.test"


async def test_command_palette_provider_lists_services(aws):
    from awsnav.ui.app import ServiceCommands

    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        await pilot.press("ctrl+p")
        await pilot.pause(0.2)
        await pilot.press(*"bedrock-agent")
        await pilot.pause(0.5)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, OperationsScreen))
        assert app.screen.service.startswith("bedrock-agent")
    assert ServiceCommands


async def test_scan_ui_finds_resources_and_drills_down_in_their_region(aws):
    from awsnav.ui.scan import ScanResultsScreen, ScanSetupScreen

    boto3.client("sqs", region_name="eu-west-1").create_queue(QueueName="far-away")
    boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="nearby")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await connected(pilot)
        await pilot.press("f2")
        await wait_for(pilot, lambda: isinstance(app.screen, ScanSetupScreen))
        setup = app.screen
        await wait_for(pilot, lambda: setup.query_one("#regions").option_count > 0)
        regions = setup.query_one("#regions")
        assert list(regions.selected) == ["us-east-1"]  # current region preselected
        regions.select("eu-west-1")
        setup.query_one("#services").value = "sqs"
        setup.query_one("#global").value = False
        await wait_for(pilot, lambda: "API calls" in str(setup.query_one("#estimate").render()))
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: isinstance(app.screen, ScanResultsScreen) and app.screen.result is not None)
        results = app.screen
        names = sorted((r.region, r.name) for r in results.result.resources)
        assert names == [("eu-west-1", "far-away"), ("us-east-1", "nearby")]
        assert "Done: 2 resources" in str(results.status.render())

        # Open the eu-west-1 queue: follow-up calls must go to eu-west-1 although the app is in us-east-1.
        results.query_one("#filter").value = "far-away"
        await pilot.pause(0.1)
        results.table.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen._actions)
        assert "eu-west-1" in str(app.screen.query_one("Crumbs").render())
        ops = [getattr(a, "operation", None) for a in app.screen._actions.values()]
        app.screen.query_one("#actions").highlighted = ops.index("GetQueueAttributes")
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen.title_text == "GetQueueAttributes")
        assert app.screen.ctx.region == "eu-west-1"
        assert app.ctx.region == "us-east-1"
        for _ in range(2):
            await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ScanResultsScreen))
        await pilot.press("s")
        await wait_for(pilot, lambda: isinstance(app.screen, TextViewer))
        assert "eu-west-1" in app.screen._text()
