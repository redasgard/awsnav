# Changelog

All notable changes to awsnav are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - first release

### Added

- Terminal UI covering every service in the installed botocore (436 today),
  discovered from the bundled API models with no per-service code.
- Resource tables with filtering, sensible columns, pagination limits and full
  JSON details.
- Drill-down: related read-only operations prefilled from each resource's
  fields.
- Operation runner for any API call, with JSON parameters and inline
  documentation. Write operations need `--allow-write` and a confirmation.
- Multi-region scan for active resources (F2): curated inventory of about 120
  resource types, a Resource Groups Tagging API pass, an optional deep mode,
  summary, failed-call report and JSON/CSV export. Also available headless via
  `awsnav --scan`.
- S3 folder browser with preview and download.
- Bedrock chat playground using the Converse API.
- Region switching, command-palette service jump, help screen.
- Login form for access key, secret and session token; also environment
  variables, profiles and custom endpoints (LocalStack).
- Published on PyPI as `awsnav` (`pipx install awsnav`), uploaded by a
  trusted-publishing workflow from the signed release packages.
