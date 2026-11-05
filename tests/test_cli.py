from awsnav.cli import build_parser, settings_from_args


def parse(argv, env):
    return settings_from_args(build_parser().parse_args(argv), env)


def test_keys_from_environment_auto_connect():
    settings, auto = parse([], {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK", "AWS_REGION": "eu-west-1"})
    assert (settings.access_key, settings.secret_key, settings.region) == ("AK", "SK", "eu-west-1")
    assert auto is True


def test_no_credentials_shows_login():
    settings, auto = parse([], {})
    assert settings.access_key is None and auto is False
    assert settings.region == "us-east-1"
    assert settings.allow_write is False


def test_arguments_override_environment():
    settings, _ = parse(
        ["--access-key", "A2", "--secret-key", "S2", "--region", "ap-south-1", "--allow-write", "--max-items", "50"],
        {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK"},
    )
    assert (settings.access_key, settings.region, settings.allow_write, settings.max_items) == ("A2", "ap-south-1", True, 50)


def test_profile_ignores_environment_keys():
    settings, auto = parse(["--profile", "dev"], {"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK"})
    assert settings.profile == "dev" and settings.access_key is None and auto is True
