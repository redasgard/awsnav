import json

import botocore.session
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from awsnav.catalog import Catalog
from awsnav.scanner import (
    GLOBAL_REGION,
    INVENTORY,
    TAGGING_TARGET,
    FoundResource,
    Scanner,
    Target,
    _tag_covered,
    classify_error,
    describe_item,
)

REGIONS = ["us-east-1", "eu-west-1"]


def test_inventory_targets_are_valid_api_calls():
    catalog = Catalog(botocore.session.get_session())
    services = set(catalog.services())
    for t in INVENTORY + [TAGGING_TARGET]:
        assert t.service in services, t
        assert t.operation in catalog.all_operations(t.service), t
        required = set(catalog.required_params(t.service, t.operation))
        assert required <= set(t.params), (t, required)
        members = catalog.operation(t.service, t.operation).input_shape.members
        assert set(t.params) <= set(members), t
    for svc in ("ec2", "s3", "rds", "lambda", "bedrock", "bedrock-agent", "bedrock-agentcore-control", "sagemaker"):
        assert any(t.service == svc for t in INVENTORY), svc


def seed(ctx):
    e1, e2 = ctx.client("ec2", "us-east-1"), ctx.client("ec2", "eu-west-1")
    r = e1.run_instances(
        ImageId="ami-12345678",
        MinCount=2,
        MaxCount=2,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "web"}]}],
    )
    e1.terminate_instances(InstanceIds=[r["Instances"][1]["InstanceId"]])
    e2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)
    ctx.client("sqs", "eu-west-1").create_queue(QueueName="eu-queue")
    ctx.client("iam").create_user(UserName="alice")
    ctx.client("sns", "us-east-1").create_topic(Name="alerts", Tags=[{"Key": "team", "Value": "ops"}])
    ctx.client("dynamodb", "eu-west-1").create_table(
        TableName="orders",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def by(result, rtype):
    return [r for r in result.resources if r.resource_type == rtype]


def test_full_standard_scan_two_regions(ctx):
    seed(ctx)
    result = Scanner(ctx, REGIONS).run()
    instances = by(result, "EC2 instance")
    assert sorted((r.region, r.name) for r in instances)[1] == ("us-east-1", "web")
    assert {r.region for r in instances} == set(REGIONS)
    assert all(r.state == "running" for r in instances)  # terminated one filtered out
    # The terminated instance must not sneak back in through the tagging API either.
    assert not [r for r in result.resources if r.source == "tagged" and r.service == "ec2"]
    # Default VPCs, default subnets and default security groups are AWS-created, not yours.
    assert not by(result, "VPC") and not by(result, "Subnet") and not by(result, "Security group")
    assert [(r.region, r.name) for r in by(result, "SQS queue")] == [("eu-west-1", "eu-queue")]
    assert [(r.region, r.name) for r in by(result, "DynamoDB table")] == [("eu-west-1", "orders")]
    assert [(r.region, r.name) for r in by(result, "IAM user")] == [(GLOBAL_REGION, "alice")]
    # The SNS topic is found once, by the inventory, even though it is also tagged.
    assert [r.name for r in result.resources if r.service == "sns"] == ["alerts"]
    assert result.calls == Scanner(ctx, REGIONS).plan().total_calls
    assert set(result.summary()["by_region"]) >= set(REGIONS)


def test_service_filter_and_csv_json_export(ctx):
    seed(ctx)
    result = Scanner(ctx, REGIONS, services=["sqs", "dyn*"], include_tagged=False, include_global=False).run()
    assert {r.service for r in result.resources} == {"sqs", "dynamodb"}
    assert not result.errors
    data = json.loads(result.to_json())
    assert data["summary"]["resources"] == 2
    assert data["resources"][0]["raw"]
    csv_text = result.to_csv()
    assert csv_text.splitlines()[0] == "region,service,type,name,id,state,arn,source,operation"
    assert "eu-queue" in csv_text


def test_found_resource_keeps_its_region_for_drilldown(ctx):
    seed(ctx)
    result = Scanner(ctx, REGIONS, services=["sqs"], include_tagged=False, include_global=False).run()
    (queue,) = result.resources
    item_ctx = queue.item_context()
    assert item_ctx.region == "eu-west-1"
    rel = {r.operation: r.params for r in ctx.catalog.related_operations(item_ctx, queue.raw)}
    attrs = ctx.execute("sqs", "GetQueueAttributes", rel["GetQueueAttributes"], region=item_ctx.region)
    assert attrs.region == "eu-west-1"
    assert ctx.region == "us-east-1"  # the app's current region is untouched


def test_s3_buckets_are_attributed_to_their_region(ctx):
    scanner = Scanner(ctx, ["eu-west-1"])
    client = ctx.new_client("s3", "us-east-1")
    stub = Stubber(client)
    stub.add_response(
        "list_buckets",
        {
            "Buckets": [
                {"Name": "west", "BucketRegion": "eu-west-1"},
                {"Name": "tokyo", "BucketRegion": "ap-northeast-1"},
                {"Name": "legacy"},
            ]
        },
        {},
    )
    with stub:
        target = next(t for t in INVENTORY if t.service == "s3")
        found, _dropped = scanner._run_target(client, "us-east-1", target)
    assert [(r.name, r.region) for r in found] == [("west", "eu-west-1"), ("legacy", GLOBAL_REGION)]


def test_aws_managed_items_are_dropped():
    scanner_target = next(t for t in INVENTORY if t.operation == "ListRoles")
    from awsnav.scanner import _skip

    assert _skip(scanner_target, {"RoleName": "AWSServiceRoleForX", "Path": "/aws-service-role/x/"})
    assert not _skip(scanner_target, {"RoleName": "app", "Path": "/"})
    rules = next(t for t in INVENTORY if t.operation == "ListRules")
    assert _skip(rules, {"Name": "x", "ManagedBy": "guardduty.amazonaws.com"})
    assert not _skip(rules, {"Name": "mine"})
    kms = next(t for t in INVENTORY if t.service == "kms")
    assert _skip(kms, {"AliasName": "alias/aws/s3"})
    assert not _skip(kms, {"AliasName": "alias/app"})


def test_plan_global_services_once_and_deep_mode(ctx):
    scanner = Scanner(ctx, REGIONS, services=["iam", "sqs"])
    groups = {(r, s) for r, s, _t in scanner.plan().groups}
    assert ("us-east-1", "iam") in groups and ("eu-west-1", "iam") not in groups
    assert {("us-east-1", "sqs"), ("eu-west-1", "sqs")} <= groups

    deep = Scanner(ctx, ["us-east-1"], deep=True, services=["sqs", "ec2"], include_tagged=False)
    ops = {t.operation for _r, s, ts in deep.plan().groups for t in ts if s == "ec2"}
    assert "DescribeRouteTables" in ops  # not in the curated inventory, picked up by deep mode
    assert "DescribeInstanceTypes" not in ops  # catalogue data, not a resource
    assert "DescribeRegions" not in ops
    assert sum(1 for t in deep.deep_targets() if t.operation == "DescribeInstances") == 0  # curated already


def test_snapshots_owned_by_other_accounts_are_ignored(ctx):
    ctx.verify()
    client = ctx.new_client("ec2", "us-east-1")
    stub = Stubber(client)
    stub.add_response(
        "describe_snapshots",
        {"Snapshots": [{"SnapshotId": "snap-mine", "OwnerId": "123456789012"}, {"SnapshotId": "snap-public", "OwnerId": "999999999999"}]},
        {"OwnerIds": ["self"]},
    )
    target = next(t for t in INVENTORY if t.operation == "DescribeSnapshots")
    with stub:
        found, dropped = Scanner(ctx, ["us-east-1"])._run_target(client, "us-east-1", target)
    assert [r.identifier for r in found] == ["snap-mine"]
    assert "snap-public" in dropped


def test_cancelled_scan_stops_early(ctx):
    scanner = Scanner(ctx, REGIONS, workers=1)
    scanner.cancel()
    result = scanner.run()
    assert result.cancelled and result.calls == 0


def test_classify_errors():
    def ce(code, status=400):
        return ClientError({"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "Op")

    assert classify_error(ce("AccessDeniedException", 403)) == "denied"
    assert classify_error(ce("UnauthorizedOperation")) == "denied"
    assert classify_error(ce("UnrecognizedClientException", 403)) == "unavailable"
    assert classify_error(ce("OptInRequired")) == "unavailable"
    assert classify_error(EndpointConnectionError(endpoint_url="https://x")) == "unavailable"
    assert classify_error(ce("ThrottlingException")) == "error"


def test_describe_item_names():
    name, ident, state, arn = describe_item(
        {"InstanceId": "i-1", "RootDeviceName": "/dev/xvda", "State": {"Name": "running"}}, {"instance"}
    )
    assert (name, ident, state) == ("i-1", "i-1", "running")
    name, ident, _s, arn = describe_item({"TopicArn": "arn:aws:sns:us-east-1:1:alerts"}, {"topic"})
    assert (name, arn) == ("alerts", "arn:aws:sns:us-east-1:1:alerts")


def test_tagged_dedupe():
    tagged = FoundResource("us-east-1", "ec2", "ec2 instance", "x", "i-1", "", "arn:aws:ec2:us-east-1:1:instance/i-1",
                           "GetResources", None, {}, {}, source="tagged")
    assert _tag_covered(tagged, {"i-1"})
    assert _tag_covered(tagged, {"arn:aws:ec2:us-east-1:1:instance/i-1"})
    assert not _tag_covered(tagged, {"i-2"})


def test_headless_cli_scan(aws, tmp_path, monkeypatch, capsys):
    from awsnav.cli import main

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    import boto3

    boto3.client("sqs", region_name="eu-west-1").create_queue(QueueName="cli-queue")
    out = tmp_path / "scan.json"
    with pytest.raises(SystemExit) as exit_info:
        main(["--scan", "--scan-regions", "us-east-1,eu-west-1", "--services", "sqs", "--no-global", "-o", str(out)])
    assert exit_info.value.code == 0
    data = json.loads(out.read_text())
    assert [(r["region"], r["name"]) for r in data["resources"]] == [("eu-west-1", "cli-queue")]
    assert "1 resources" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--scan", "--scan-regions", "eu-west-1", "--services", "sqs", "--no-global", "--no-tagged"])
    table = capsys.readouterr().out
    assert table.splitlines()[0].split() == ["REGION", "SERVICE", "TYPE", "NAME", "ID", "STATE"]
    assert "cli-queue" in table


def test_failing_calls_are_recorded_not_raised(ctx, monkeypatch):
    import awsnav.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "INVENTORY", [Target("sqs", "ListQueues", "SQS queue", params={"BadParam": 1})])
    result = Scanner(ctx, REGIONS, include_tagged=False).run()
    assert result.resources == []
    assert [(e.region, e.kind) for e in sorted(result.errors, key=lambda e: e.region)] == [
        ("eu-west-1", "error"),
        ("us-east-1", "error"),
    ]
    assert "BadParam" in result.errors[0].message
