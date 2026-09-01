---
name: biomero-importer-dev
description: Develop and test BIOMERO.importer, including OMERO in-place registration, preprocessing, ingestion tracking, Zarr handling, and package or Docker dependency updates.
---

# BIOMERO.importer Development

Treat `README.md`, `pyproject.toml`, and `.github/workflows/python-package.yml`
as the authoritative setup and test instructions. Inspect them before changing
dependencies, Python support, CI, or import behavior.

## Development environment

Use Python 3.12 in a repository-local `.venv`; do not silently substitute a
shared workspace environment or a running service container.

Install the platform-specific ZeroC Ice 3.6.5 wheel from Glencoe before the
editable package install. Do not ask pip to build `zeroc-ice` from PyPI. Then
install the project with its test dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install "zeroc-ice @ <Glencoe wheel URL>"
.\.venv\Scripts\python -m pip install -e ".[test]"
```

On Linux or WSL, use the equivalent `.venv/bin/python` commands and the
matching Glencoe Linux wheel. If native Windows hangs while importing OMERO
plugins, use a repository-local WSL/Linux `.venv` matching CI rather than
changing production code or treating a Docker smoke assertion as the test
suite.

## Verification

Run focused tests while iterating, followed by the same unit suite and coverage
command used by CI:

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

## Repository and deployment notes

- The default integration branch is `main`; confirm the remote default before
  interpreting older references to `master`.
- Preserve in-place import and shared `/data` path behavior.
- Importer source changes require an importer service restart after deployment
  because worker processes may cache imported modules.
- Finish an authorized development step with a focused commit. Push only when
  the user has requested branch publication or direct integration.
