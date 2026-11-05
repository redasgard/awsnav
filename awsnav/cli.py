"""Command-line entry point."""

from __future__ import annotations

import argparse
import os

from awsnav import __version__
from awsnav.session import DEFAULT_REGION, ConnectionSettings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="awsnav",
        description="Terminal UI for browsing every AWS service (EC2, S3, Bedrock, …) with an access key and secret.",
    )
    p.add_argument("--access-key", help="AWS access key ID (default: $AWS_ACCESS_KEY_ID)")
    p.add_argument("--secret-key", help="AWS secret access key (default: $AWS_SECRET_ACCESS_KEY)")
    p.add_argument("--session-token", help="session token for temporary credentials (default: $AWS_SESSION_TOKEN)")
    p.add_argument("--profile", help="use a named profile from ~/.aws instead of keys")
    p.add_argument("--region", help=f"initial region (default: $AWS_REGION, $AWS_DEFAULT_REGION or {DEFAULT_REGION})")
    p.add_argument("--endpoint-url", help="custom endpoint for every service, e.g. LocalStack http://localhost:4566")
    p.add_argument("--max-items", type=int, default=500, help="maximum items fetched per list call (default 500)")
    p.add_argument(
        "--allow-write",
        action="store_true",
        help="allow Create/Update/Delete/… operations from the operation runner (each one is confirmed)",
    )
    p.add_argument("--no-login", action="store_true", help="connect immediately without showing the login form")
    p.add_argument("--version", action="version", version=f"awsnav {__version__}")
    return p


def settings_from_args(args: argparse.Namespace, env: dict[str, str] | None = None) -> tuple[ConnectionSettings, bool]:
    env = dict(os.environ if env is None else env)
    access = args.access_key or env.get("AWS_ACCESS_KEY_ID")
    secret = args.secret_key or env.get("AWS_SECRET_ACCESS_KEY")
    token = args.session_token or env.get("AWS_SESSION_TOKEN")
    if args.profile:
        # A profile takes precedence over keys from the environment.
        access = args.access_key
        secret = args.secret_key
        token = args.session_token
    region = args.region or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
    settings = ConnectionSettings(
        access_key=access,
        secret_key=secret,
        session_token=token,
        profile=args.profile,
        region=region,
        endpoint_url=args.endpoint_url or env.get("AWS_ENDPOINT_URL"),
        max_items=max(1, args.max_items),
        allow_write=args.allow_write,
    )
    auto = args.no_login or bool(args.profile) or bool(access and secret)
    return settings, auto


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings, auto = settings_from_args(args)
    from awsnav.ui.app import AwsNavApp

    AwsNavApp(settings, auto_connect=auto).run()


if __name__ == "__main__":
    main()
