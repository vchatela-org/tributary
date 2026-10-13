# Development and releases

## Running it from source

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
TRIBUTARY_DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

`python -m tests.fakes` serves a fake Audiobookshelf with a few sessions,
to try the whole chain without a real server (`TRIBUTARY_ABS_URL=http://127.0.0.1:13378`,
`TRIBUTARY_ABS_TOKEN=demo-key`).

Writing a connector: [docs/connectors/README.md](connectors/README.md).
Replacing a job that already writes into InfluxDB: [porting](porting.md).

## Privacy

Before pushing, `./scripts/privacy-scan.sh` checks that no personal data
(real identifiers, private addresses, internal hostnames) entered the tree.
Test data is synthetic, and so are the screenshots in `docs/screenshots/`:
the UI's are taken from a local instance with a throwaway `TRIBUTARY_DATA_DIR`
seeded with invented runs (and an InfluxDB URL nothing listens on), the
Grafana one from a throwaway InfluxDB and Grafana fed with invented sessions,
never from a real deployment.

## Releasing

Releases are cut by [release-please](https://github.com/googleapis/release-please) from the
[conventional commits](https://www.conventionalcommits.org/) on `main`. It keeps one release PR
open, `chore(main): release X.Y.Z`, with the next version and its `CHANGELOG.md` entry; every
`feat`, `fix`, `perf`, `revert` or `deps` change merged since the last release joins it, so several
can ship together. Merging it tags `vX.Y.Z`, publishes the GitHub Release (the notes Renovate and
Dependabot show when they bump the image) and builds the image on the tag: GHCR (`X.Y.Z`, `X.Y` and
`latest`) and the owner's Harbor. Don't bump `app/__version__.py` or push a tag by hand.

PRs are **squash-merged**, with the PR title as the commit subject and an empty body (the merge
dialog's prefill: keep it). The title is the PR's one line in the notes and decides the bump: `feat`
makes a minor, the other listed types a patch, a breaking change (`feat!:`) a minor while in 0.x;
`docs`, `chore`, `ci`, `test`, `refactor`, `build` and `style` stay out of the notes. So keep the
title a conventional commit; the "PR title" check says when it is not. Details, and how to force a
version: [shared-workflows](https://github.com/vchatela-org/shared-workflows#reusable-workflow-release-pr-release-please).
