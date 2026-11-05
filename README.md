# awsnav

A keyboard-driven terminal UI for exploring an AWS account with an access key and secret.
It covers **every AWS service that boto3 knows about**: classics like EC2, S3, IAM and RDS, and
newer ones like Bedrock, Bedrock Agents and Bedrock AgentCore. That is more than 400 services today.

```
us-east-1 › ec2 › Instances DescribeInstances()
 Name (tag)  InstanceId           InstanceType  State    ImageId       …
 web-1       i-0a1b2c3d4e5f6a7b8  t3.micro      running  ami-12345678
```

## How "all services" works

awsnav does not hard-code services. At start-up it reads the API models that ship inside
botocore. From those models it works out three things:

- **Resource types.** For each service, it lists every `List*`, `Describe*` and `Get*` call that needs no parameters, such as Instances, Buckets, Foundation Models or Agents.
- **Resource lists.** It finds the list of resources in each response, follows pagination, and picks sensible table columns.
- **Drill-downs.** For any resource, it finds the related read-only calls that its fields can fill in. For example, a bucket leads to `ListObjectsV2(Bucket=…)` and `GetBucketPolicy`. A Bedrock model leads to `GetFoundationModel(modelIdentifier=…)`. An ECS cluster leads to `ListServices(cluster=…)`.

When AWS launches a service, upgrade boto3 and the service appears in awsnav:

```bash
pip install -U boto3
```

Any API call that the automatic views do not cover can be made from the **operation runner**.
It takes JSON parameters and shows the call's documentation alongside.

A few hand-made extras sit on top:

- **S3 browser.** Browse buckets as folders, preview an object's first 64 KiB, and download objects.
- **Bedrock chat playground.** Chat with any text model or inference profile through the Converse API. It is reachable from a model's actions or with Ctrl+B anywhere.
- **Sensible defaults.** EC2 images and snapshots are limited to your own account, so the list does not load every public AMI.

## Install

Python 3.10 or newer is required.

```bash
git clone https://github.com/ilyk/aws-tool.git
cd aws-tool
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Run

```bash
awsnav                                    # login form: access key, secret, region
AWS_ACCESS_KEY_ID=AKIA… AWS_SECRET_ACCESS_KEY=… awsnav --region eu-west-1
awsnav --profile dev                      # use a profile from ~/.aws
awsnav --endpoint-url http://localhost:4566   # LocalStack
python -m awsnav                          # same as awsnav
```

Prefer environment variables or the login form over `--secret-key`. Command-line arguments are
visible to other users of the machine. Credentials are kept in memory only and are never written to disk.

| Option | Meaning |
| --- | --- |
| `--access-key`, `--secret-key`, `--session-token` | Credentials. They default to the standard `AWS_*` environment variables. |
| `--profile` | Use a named profile instead of keys. |
| `--region` | Starting region. Change it at any time with Ctrl+G. |
| `--endpoint-url` | Custom endpoint for every service. |
| `--max-items` | Maximum items fetched per list call. The default is 500. |
| `--allow-write` | Allow write operations from the runner. Each one is confirmed first. |
| `--no-login` | Skip the login form and use the default credential chain. |

### Read-only by default

Browsing only ever calls read-only operations: `List`, `Describe`, `Get`, `Search`, `Head`,
`Query`, `Scan` and similar. The runner refuses `Create`, `Delete`, `Put`, `Update` and other writes
unless awsnav was started with `--allow-write`. Even then, every write needs a confirmation.
Some reads still cost money, such as DynamoDB `Scan` on a large table. Bedrock chat messages are
billed model invocations.

The IAM user or role needs read permissions for whatever you browse. The AWS managed
`ReadOnlyAccess` policy covers almost everything.

## Keys

| Where | Key | Action |
| --- | --- | --- |
| everywhere | Enter / Esc | open / clear filter, then go back |
| | `/` | filter the list; every typed word must match |
| | Ctrl+P | command palette: jump to any service by name |
| | Ctrl+G | change region |
| | Ctrl+B | Bedrock chat playground |
| | F1 or `?` | help |
| | Ctrl+Q | quit |
| service | `a` | toggle between resource types and every API operation |
| | `x` | open the runner for the highlighted operation |
| resource list | `r` / `v` / `e` | refresh / raw JSON / edit parameters and re-run |
| | `x` | run any operation, prefilled from the highlighted resource |
| details | `x` / `y` / `w` / `r` | run any operation / copy JSON / save JSON / reload |
| S3 browser | `p` / `d` / `b` | preview / download / bucket details |
| runner | Ctrl+R / Ctrl+O | run / choose another operation |

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests never touch real AWS. They use [moto](https://github.com/getmoto/moto) and botocore
stubs. They also drive the real Textual app headlessly through EC2, S3, IAM, SQS and Bedrock
flows. One test checks that the runner can describe and template every operation of every
bundled service.

Code layout:

- `awsnav/catalog.py` covers service and operation discovery, response parsing and drill-down matching.
- `awsnav/session.py` covers credentials, clients and running operations.
- `awsnav/formatting.py` covers table columns and JSON rendering.
- `awsnav/ui/` holds the Textual screens.
