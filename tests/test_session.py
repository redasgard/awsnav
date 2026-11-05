from awsnav.session import AwsContext, ConnectionSettings


def test_verify_identity(ctx):
    ident = ctx.verify()
    assert ident["Account"] == "123456789012"
    assert "123456789012" in ctx.describe_identity()


def test_execute_list_s3(ctx):
    ctx.client("s3").create_bucket(Bucket="bucket-one")
    result = ctx.execute("s3", "ListBuckets")
    assert result.is_list
    assert [b["Name"] for b in result.items] == ["bucket-one"]


def test_execute_flattens_ec2_instances(ctx):
    ctx.client("ec2").run_instances(ImageId="ami-12345678", MinCount=3, MaxCount=3)
    result = ctx.execute("ec2", "DescribeInstances")
    assert len(result.items) == 3
    assert all("InstanceId" in i for i in result.items)


def test_execute_wraps_string_lists(ctx):
    ctx.client("dynamodb").create_table(
        TableName="orders",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    result = ctx.execute("dynamodb", "ListTables")
    assert result.items == [{"TableName": "orders"}]


def test_execute_single_object(ctx):
    result = ctx.execute("sts", "GetCallerIdentity")
    assert not result.is_list
    assert result.raw["Account"] == "123456789012"
    assert "ResponseMetadata" not in result.raw


def test_max_items_truncates(aws):
    ctx = AwsContext(ConnectionSettings(access_key="t", secret_key="t", max_items=2))
    sqs = ctx.client("sqs")
    for i in range(5):
        sqs.create_queue(QueueName=f"q{i}")
    result = ctx.execute("sqs", "ListQueues")
    assert len(result.items) == 2
    assert result.truncated


def test_region_switch_uses_new_clients(ctx):
    ctx.client("sqs").create_queue(QueueName="east")
    ctx.region = "eu-west-1"
    assert ctx.execute("sqs", "ListQueues").items == []
