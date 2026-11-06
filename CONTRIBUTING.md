# Contributing

Thanks for helping. Bug reports, resource types for the scanner and fixes for
services that don't display well are all welcome.

## Set up

```sh
git clone https://github.com/redasgard/awsnav
cd awsnav
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]' ruff
pytest -q                      # about a minute, never touches real AWS
ruff check awsnav tests scripts
```

Tests run against [moto](https://github.com/getmoto/moto) and botocore stubs,
and drive the real Textual app headlessly. Please add a test with every change;
`tests/test_app.py` shows how to script the UI.

## Where things live

| Path | What it does |
| --- | --- |
| `awsnav/catalog.py` | Discovers services and operations from botocore's models; finds the resource list in a response; matches drill-down operations to a resource's fields. |
| `awsnav/session.py` | Credentials, per-region clients, running an operation with pagination. |
| `awsnav/scanner.py` | The multi-region scan and its curated `INVENTORY`. |
| `awsnav/formatting.py` | Table columns and cell rendering. |
| `awsnav/ui/` | Textual screens: services, resources, details, runner, S3, Bedrock, scan. |
| `scripts/` | Regenerates the README images (see `docs/RELEASING.md`). |

## Adding a resource type to the scan

Add one line to `INVENTORY` in `awsnav/scanner.py`:

```python
T("service", "ListThings", "Human label", params={...}, skip_if={"State": ("deleted",)}),
```

- The operation must be read-only and every required parameter must be in
  `params`. `test_inventory_targets_are_valid_api_calls` checks this against
  the real API model.
- Use `skip_if`, `skip_prefix` or `skip_present` to leave out AWS-created
  defaults and resources that are already gone.
- Mark global services with `global_=True` so they are scanned once.

## A service displays badly

Most display logic is generic. Before adding a special case, check whether a
small change to the generic rules (column choice in `formatting.py`, item
extraction or drill-down matching in `catalog.py`) fixes it for every service.
Special cases go in `CURATED_DEFAULT_PARAMS` and `CURATED_ITEM_PATHS` in
`catalog.py`.

## Pull requests

- Keep each pull request to one change, and describe what a user will see.
- CI must be green: ruff and the tests on Python 3.10 to 3.13.
- If the UI changes visibly, regenerate the screenshots
  (`docs/RELEASING.md`, "Refreshing the README images").
