import datetime as dt

from awsnav.formatting import TAG_NAME_COLUMN, cell_value, choose_columns, format_cell, human_size, row_view, to_json


def test_row_view_uses_name_tag_and_state_name():
    item = {
        "InstanceId": "i-1",
        "State": {"Code": 16, "Name": "running"},
        "Tags": [{"Key": "Name", "Value": "web"}],
        "NetworkInterfaces": [{"x": 1}],
    }
    row = row_view(item)
    assert row[TAG_NAME_COLUMN] == "web"
    assert row["State"] == "running"
    assert "NetworkInterfaces" not in row


def test_choose_columns_prefers_identity_fields():
    rows = [row_view({"InstanceId": "i-1", "InstanceType": "t3.micro", "RootDeviceName": "/dev/xvda", "State": {"Name": "running"}, "Arn": "arn"})]
    cols = choose_columns(rows, nouns={"instance"})
    assert cols[:3] == ["InstanceId", "InstanceType", "State"]
    assert cols[-1] == "Arn"


def test_cell_values():
    assert cell_value(True) == "yes"
    assert cell_value(dt.datetime(2024, 1, 2, 3, 4, 5)) == "2024-01-02 03:04:05"
    assert cell_value(["a", "b"]) == "a, b"
    assert cell_value({"deep": {"x": 1}}) is None
    assert format_cell("x" * 100).endswith("…")


def test_to_json_handles_datetimes_and_bytes():
    out = to_json({"t": dt.datetime(2024, 1, 1), "b": b"hi"}, indent=None)
    assert out == '{"t": "2024-01-01T00:00:00", "b": "hi"}'


def test_human_size():
    assert human_size(10) == "10 B"
    assert human_size(2048) == "2.0 KiB"
