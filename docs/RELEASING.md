# Releasing

Releases are built by CI and published by a person. CI stages the wheel, the
sdist and their signed SLSA provenance in a **draft** release; publishing is the
last, deliberate step.

0. CI (`.github/workflows/ci.yml`: ruff, tests on Python 3.10 to 3.13) must be
   green on the commit you are about to tag.
1. Bump the version in `pyproject.toml` **and** `awsnav/__init__.py`, move the
   `Unreleased` notes in `CHANGELOG.md` under the new version, commit and push
   to `main`.
2. Tag and push:

   ```sh
   git tag -a vX.Y.Z -m "awsnav X.Y.Z"
   git push origin vX.Y.Z
   ```

   The workflow refuses a tag that does not match both version strings.
3. Wait for the `release` workflow. It runs the tests, builds the wheel and
   sdist, generates signed SLSA provenance, and creates a **draft** release with
   all of them attached. A tag containing `-` (`v0.2.0-rc1`) becomes a
   pre-release.
4. Open the draft on GitHub, edit the notes, and press **Publish release**.

If the draft is wrong, delete it and the tag, fix, and tag again. That is safe
only **before** publishing; after publishing, use the next version number.

## Verifying a download

```sh
slsa-verifier verify-artifact awsnav-X.Y.Z-py3-none-any.whl \
  --provenance-path awsnav.intoto.jsonl \
  --source-uri github.com/redasgard/awsnav --source-tag vX.Y.Z
```

([slsa-verifier](https://github.com/slsa-framework/slsa-verifier).) Then
install the verified file with `pipx install ./awsnav-X.Y.Z-py3-none-any.whl`.

## Refreshing the README images

The screenshots, the demo GIF and the social preview are generated from the
real app against a mocked demo account:

```sh
pip install -e '.[dev]'
python scripts/screenshots.py build/shots    # drives the app, writes SVGs
node scripts/render.mjs build/shots docs     # PNGs, demo.gif, social-preview.png
```

`render.mjs` needs Playwright with Chromium, the JetBrains Mono font and ffmpeg.
After regenerating, upload `docs/social-preview.png` under the repository's
**Settings > General > Social preview**.
