# Security

awsnav handles AWS credentials, so its security model is worth stating plainly.

## What awsnav does with your credentials

- Keys typed into the login form, passed as arguments or read from the
  environment are held **in memory only** for the session. awsnav never writes
  them to disk, to logs, or anywhere else.
- Requests go straight from your machine to AWS (or to the `--endpoint-url`
  you set) through boto3. There is no awsnav server, telemetry or analytics.
- The only file awsnav writes on its own is a cache of service display names
  in `~/.cache/awsnav/`. It writes other files only when you ask, through
  *save*, *export* or *download*.

## What awsnav calls

- Browsing, drill-downs and the region scan call **read-only** operations only:
  names starting with `List`, `Describe`, `Get`, `Search`, `Lookup`, `Head`,
  `BatchGet`, `Query`, `Scan`, `Filter` or `BatchDescribe`.
- The operation runner refuses every other operation unless awsnav was started
  with `--allow-write`. Even then, each write call shows its parameters and
  waits for confirmation.
- Some read operations reveal sensitive data by design, for example
  `GetSecretValue` or `GetParametersByPath` with decryption. They are only
  called when you choose them.
- Some reads cost money, such as DynamoDB `Scan` on a large table, and each
  Bedrock chat message is a billed model invocation.

## Recommended setup

- Use a dedicated IAM identity with the AWS managed `ReadOnlyAccess` policy, or
  narrower, and short-lived credentials (`--session-token`, SSO profiles).
- Prefer environment variables, profiles or the login form over
  `--secret-key`: command-line arguments are visible to other local users.

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private
[security advisory form](https://github.com/redasgard/awsnav/security/advisories/new)
for this repository. Include what you found, how to reproduce it, and the
version you tested. You will get an acknowledgement and updates in the advisory.
