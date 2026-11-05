import botocore.session
import pytest

from awsnav.catalog import Catalog, ItemContext, resource_label, singular, wrap_scalar_items


@pytest.fixture(scope="module")
def catalog():
    return Catalog(botocore.session.get_session())


def test_every_bundled_service_is_listed_and_introspectable(catalog):
    services = catalog.services()
    assert len(services) > 300
    for required in ("ec2", "s3", "bedrock", "bedrock-runtime", "bedrock-agent", "bedrock-agentcore-control"):
        assert required in services
    # Every service model must load and yield operations without errors.
    with_resources = 0
    for service in services:
        assert catalog.all_operations(service)
        if catalog.root_operations(service):
            with_resources += 1
    assert with_resources > len(services) * 0.8


def test_runner_docs_and_templates_for_every_operation(catalog):
    """The operation runner must be able to describe and template any API call."""
    count = 0
    for service in catalog.services():
        for op in catalog.all_operations(service):
            catalog.describe_params(service, op)
            template = catalog.params_template(service, op)
            assert set(template) == set(catalog.required_params(service, op))
            count += 1
    assert count > 10000


def test_popular_services_come_first(catalog):
    ordered = catalog.ordered_services()
    assert ordered[:2] == ["ec2", "s3"]
    assert sorted(ordered) == catalog.services()


def test_root_operations_have_no_required_params(catalog):
    ops = catalog.root_operations("bedrock")
    assert "ListFoundationModels" in ops
    assert "GetFoundationModel" not in ops  # needs modelIdentifier
    for op in ops:
        assert not catalog.required_params("bedrock", op)
    assert catalog.root_operations("s3")[0] == "ListBuckets"


def test_resource_label():
    assert resource_label("DescribeDBInstances") == "DB Instances"
    assert resource_label("ListFoundationModels") == "Foundation Models"


def test_singular():
    assert singular("Policies") == "Policy"
    assert singular("Addresses") == "Address"
    assert singular("TableNames") == "TableName"


def test_related_operations_s3_bucket(catalog):
    ctx = ItemContext("s3", "ListBuckets", {}, "Buckets")
    rel = {r.operation: r.params for r in catalog.related_operations(ctx, {"Name": "my-bucket"})}
    assert rel["ListObjectsV2"] == {"Bucket": "my-bucket"}
    assert rel["GetBucketPolicy"] == {"Bucket": "my-bucket"}
    assert "DeleteBucket" not in rel  # writes are never offered as drill-downs


def test_related_operations_s3_object_uses_parent_context(catalog):
    ctx = ItemContext("s3", "ListObjectsV2", {"Bucket": "b"}, "Contents")
    rel = {r.operation: r.params for r in catalog.related_operations(ctx, {"Key": "a.txt", "Size": 3})}
    assert rel["HeadObject"] == {"Bucket": "b", "Key": "a.txt"}
    assert "GetObject" not in rel  # streaming output is handled by the S3 preview action


def test_related_operations_bedrock_model(catalog):
    ctx = ItemContext("bedrock", "ListFoundationModels", {}, "modelSummaries")
    item = {"modelId": "anthropic.claude-v2", "modelArn": "arn:aws:bedrock:::foundation-model/x"}
    rel = {r.operation: r.params for r in catalog.related_operations(ctx, item)}
    assert rel["GetFoundationModel"] == {"modelIdentifier": "anthropic.claude-v2"}


def test_related_operations_bedrock_agent(catalog):
    ctx = ItemContext("bedrock-agent", "ListAgents", {}, "agentSummaries")
    rel = {r.operation: r.params for r in catalog.related_operations(ctx, {"agentId": "A1", "agentName": "x"})}
    assert rel["GetAgent"] == {"agentId": "A1"}
    assert rel["ListAgentVersions"] == {"agentId": "A1"}


def test_related_operations_ecs_cluster_from_string_list(catalog):
    items = wrap_scalar_items(["arn:aws:ecs:us-east-1:1:cluster/c"], "clusterArns")
    assert items == [{"clusterArn": "arn:aws:ecs:us-east-1:1:cluster/c"}]
    ctx = ItemContext("ecs", "ListClusters", {}, "clusterArns")
    rel = {r.operation: r.params for r in catalog.related_operations(ctx, items[0])}
    assert rel["ListServices"] == {"cluster": items[0]["clusterArn"]}
    assert rel["DescribeClusters"] == {"clusters": [items[0]["clusterArn"]]}


def test_related_operations_iam_user(catalog):
    ctx = ItemContext("iam", "ListUsers", {}, "Users")
    rel = {r.operation for r in catalog.related_operations(ctx, {"UserName": "bob", "Arn": "arn:aws:iam::1:user/bob"})}
    assert {"GetUser", "ListAccessKeys", "ListAttachedUserPolicies"} <= rel


def test_extract_items_nested_list(catalog):
    raw = {"DistributionList": {"Items": [{"Id": "E1"}], "Quantity": 1}}
    items, key = catalog.extract_items("cloudfront", "ListDistributions", raw)
    assert items == [{"Id": "E1"}]
    assert key == "DistributionList.Items"


def test_extract_items_single_object(catalog):
    items, key = catalog.extract_items("sts", "GetCallerIdentity", {"Account": "1", "Arn": "a", "UserId": "u"})
    assert items is None and key is None


def test_params_template_and_prefill(catalog):
    ctx = ItemContext("lambda", "ListFunctions", {}, "Functions")
    prefill = catalog.prefill_for("lambda", "Invoke", ctx, {"FunctionName": "f1", "Runtime": "python3.12"})
    assert prefill["FunctionName"] == "f1"
    template = catalog.params_template("lambda", "Invoke", prefill)
    assert template == {"FunctionName": "f1"}
    empty = catalog.params_template("s3", "GetObject")
    assert set(empty) == {"Bucket", "Key"}


def test_curated_defaults_avoid_public_images(catalog):
    assert catalog.default_params("ec2", "DescribeImages") == {"Owners": ["self"]}
    assert catalog.default_params("ec2", "DescribeVpcs") == {}
