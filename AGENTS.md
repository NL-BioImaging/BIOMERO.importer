# BIOMERO.importer

Import service of the BIOMERO stack: watches shared storage, preprocesses data
and registers it in OMERO in place, with ingestion tracking and Zarr handling.
Published as the `biomero_importer` Python package and a Docker image;
[NL-BIOMERO](https://github.com/NL-BioImaging/NL-BIOMERO) includes this repo as
its `biomero-importer` submodule.

## Read first

1. Shared rules for all component repositories:
   <https://github.com/NL-BioImaging/NL-BIOMERO/blob/master/.agents/skills/nl-biomero-engineering/SKILL.md>
2. Independent PR review procedure:
   <https://github.com/NL-BioImaging/NL-BIOMERO/blob/master/.agents/skills/nl-biomero-review/SKILL.md>
3. This repository's development and test rules:
   [`.agents/skills/biomero-importer-dev/SKILL.md`](.agents/skills/biomero-importer-dev/SKILL.md)

## Layout

- `biomero_importer/`: package source.
- `tests/unittests/`: unit tests. `config/`, `set_up/`: configuration and setup.
- `docs/`, `mkdocs.yml`: MkDocs documentation.
- `Dockerfile`, `pyproject.toml`: image and package definitions.

## Build and check

Python 3.12 in a repo-local `.venv`; install steps are in the dev skill.

- Tests: `python -m pytest tests/unittests/ --cov=biomero_importer --cov-report=term-missing -v`
- Lint: `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics`
- Docs: `python -m pip install -r docs/requirements.txt && python -m mkdocs build --strict`
- CI: `.github/workflows/` (tests and lint, docs, Docker image, PyPI publish).

## Related repositories

biomero (core), biomero-scripts, NL-BIOMERO, OMERO.biomero, BIOMERO.shallower,
biomero-schema, BIOMERO.ZarrViewer, all under <https://github.com/NL-BioImaging>.
Each has its own `AGENTS.md`.
