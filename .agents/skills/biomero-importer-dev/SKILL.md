---
name: biomero-importer-dev
description: Develop and test BIOMERO.importer, including OMERO in-place registration, preprocessing, ingestion tracking, Zarr handling, and package or Docker dependency updates. Also use when drafting component release notes.
---

# BIOMERO.importer Development

Required reading first: the [shared NL-BIOMERO engineering rules](https://github.com/NL-BioImaging/NL-BIOMERO/blob/master/.agents/skills/nl-biomero-engineering/SKILL.md) (branches,
pull requests, merging, agent guidance, local checkouts). This skill adds only
importer-specific rules.

Treat `README.md`, `pyproject.toml`, and `.github/workflows/python-package.yml`
as the authoritative setup and test instructions. Inspect them before changing
dependencies, Python support, CI, or import behavior.

## Development environment

Use Python 3.12 in a repository-local `.venv`; do not silently substitute a
shared workspace environment or a running service container.

Install the platform-specific ZeroC Ice 3.6.5 wheel from Glencoe before the
editable package install. Do not ask pip to build `zeroc-ice` from PyPI. Then
install the project with its test and identity dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install "zeroc-ice @ https://github.com/glencoesoftware/zeroc-ice-py-win-x86_64/releases/download/20240325/zeroc_ice-3.6.5-cp312-cp312-win_amd64.whl"
.\.venv\Scripts\python -m pip install -e ".[test,identity]"
.\.venv\Scripts\python -m pip check
```

On Linux or WSL, use the equivalent `.venv/bin/python` commands and the
matching Glencoe Linux wheel. If native Windows hangs while importing OMERO
plugins, use a repository-local WSL/Linux Python 3.12 `.venv` matching CI. If
WSL lacks Python 3.12 and the matching NL-BIOMERO importer image has already
been built, run the complete suite against the mounted checkout instead:

```powershell
docker run --rm `
  --volume "${PWD}:/work" `
  --workdir /work `
  --entrypoint /opt/conda/bin/conda `
  nl-biomero-biomero-importer:latest `
  run -n auto-import-env python -m pytest tests/unittests -q
```

This is a full unit-suite fallback, not a container smoke assertion. Do not
change production code to accommodate a platform-specific import hang.

## Verification

For a direct default-branch push or release, follow focused tests with the same
unit suite and coverage command used by CI. On a pull-request branch, focused
tests may be followed by a prompt push so GitHub runs this full gate:

```powershell
.\.venv\Scripts\python -m pytest tests/unittests/ --cov=biomero_importer --cov-report=term-missing -v
```

Run the CI lint checks when Python files change:

```powershell
.\.venv\Scripts\python -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
.\.venv\Scripts\python -m flake8 . --count --exit-zero --max-complexity=10 --max-line-length=79 --statistics
```

Add regression coverage at the lowest useful layer, while ensuring the
production call path uses the tested helper. Record any environment-caused
test limitation separately from assertion failures.

## Documentation and compatibility

Write feature guides for current behavior, usage and configuration. Put
version-specific upgrade, restart and deployment instructions in dedicated
upgrade documentation or release notes, linking from feature guides when
needed. Keep investigation history, validation evidence, merge order and
cross-repository release coordination in pull requests. Contributor workflows
belong in development documentation. Review changed documentation for its
intended reader; do not add a deployment narrative merely because a bug was fixed.

Backward compatibility means keeping supported interfaces and workflows working
in existing deployments. Make corrective fixes effective there; do not preserve
a known bug as the default or require a new opt-in solely for compatibility.
New optional behavior may need a configuration gate. Honor explicit user
decisions about defaults and rollout scope.

## Repository and deployment notes

- The default integration branch is `main`; confirm the remote default before
  interpreting older references to `master`.
- Preserve in-place import and shared `/data` path behavior.
- Importer source changes require an importer service restart after deployment
  because worker processes may cache imported modules.
- Follow the shared rules for publication and CI.

## Release notes

Before drafting a stable or beta release body, read the current
[shared component release-note guide](https://github.com/NL-BioImaging/NL-BIOMERO/blob/master/.agents/skills/omero-biomero-expert/references/component-release-notes.md).
Keep the format in that single source; do not copy it into this repository.
