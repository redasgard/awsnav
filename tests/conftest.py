import pytest


@pytest.fixture(autouse=True)
def isolated_aws_env(monkeypatch, tmp_path):
    """Never touch real credentials, config or caches."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def aws():
    from moto import mock_aws

    with mock_aws():
        yield


@pytest.fixture
def ctx(aws):
    from awsnav.session import AwsContext, ConnectionSettings

    return AwsContext(ConnectionSettings(access_key="testing", secret_key="testing"))
