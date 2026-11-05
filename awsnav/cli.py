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
    scan = p.add_argument_group("headless scan (no UI)")
    scan.add_argument("--scan", action="store_true", help="scan regions for active resources, print or save the result, and exit")
    scan.add_argument(
        "--scan-regions",
        default="all",
        help="'all' (every region enabled for the account), 'current', or a comma-separated list (default: all)",
    )
    scan.add_argument("--deep", action="store_true", help="also call every List/Describe operation of every service (slow)")
    scan.add_argument("--services", help="only scan these services, comma-separated, wildcards allowed (e.g. 'ec2,bedrock*')")
    scan.add_argument("--no-global", action="store_true", help="skip global services (IAM, S3, Route 53, CloudFront)")
    scan.add_argument("--no-tagged", action="store_true", help="skip the Resource Groups Tagging API pass")
    scan.add_argument("--workers", type=int, default=16, help="parallel API calls (default 16)")
    scan.add_argument("--output", "-o", help="write results to a .json or .csv file instead of printing a table")
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


def run_scan(args: argparse.Namespace, settings: ConnectionSettings) -> int:
    """Headless scan; returns the process exit code."""
    import sys

    from awsnav.scanner import Scanner, enabled_regions, format_table
    from awsnav.session import AwsContext

    ctx = AwsContext(settings)
    try:
        ident = ctx.verify()
    except Exception as exc:  # noqa: BLE001
        print(f"awsnav: cannot verify credentials: {exc}", file=sys.stderr)
        return 2
    if args.scan_regions == "all":
        regions = enabled_regions(ctx)
    elif args.scan_regions == "current":
        regions = [ctx.region]
    else:
        regions = [r.strip() for r in args.scan_regions.split(",") if r.strip()]
    scanner = Scanner(
        ctx,
        regions,
        deep=args.deep,
        include_global=not args.no_global,
        include_tagged=not args.no_tagged,
        services=(args.services or "").split(","),
        workers=max(1, min(args.workers, 64)),
    )
    print(f"Scanning account {ident.get('Account')} in {len(regions)} region(s)…", file=sys.stderr)
    interactive = sys.stderr.isatty()

    def progress(done: int, total: int, _found, _errors) -> None:
        if interactive:
            print(f"\r  {done}/{total} calls", end="", file=sys.stderr, flush=True)

    try:
        result = scanner.run(progress)
    except KeyboardInterrupt:
        scanner.cancel()
        return 130
    if interactive:
        print(file=sys.stderr)
    if args.output:
        text = result.to_csv() if args.output.lower().endswith(".csv") else result.to_json()
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    else:
        print(format_table(result))
    s = result.summary()
    errors = ", ".join(f"{v} {k}" for k, v in s["errors"].items()) or "none"
    where = f" -> {args.output}" if args.output else ""
    print(f"{s['resources']} resources, {s['calls']} calls in {s['seconds']}s, failed calls: {errors}{where}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings, auto = settings_from_args(args)
    if args.scan:
        raise SystemExit(run_scan(args, settings))
    from awsnav.ui.app import AwsNavApp

    AwsNavApp(settings, auto_connect=auto).run()


if __name__ == "__main__":
    main()
