# Development and database migrations

This project uses Alembic to manage database schema changes for BIOMERO.importer's tables only. Migrations run automatically on container startup (guarded by a Postgres advisory lock) and are isolated via a per-project version table `alembic_version_omeroadi`.

Below is a practical, copy-paste friendly guide to make and apply a schema change.

### 1) Create a virtual environment (Windows/Linux/macOS)

Windows PowerShell (Python 3.12, no activation required):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
# Install Ice binaries first (follow the blog instructions for your OS)
# https://www.glencoesoftware.com/blog/2023/12/08/ice-binaries-for-omero.html
.\.venv\Scripts\python -m pip install -e .
```

Linux/macOS (Python 3.12):

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
# Install Ice binaries first (follow the blog instructions for your OS)
# https://www.glencoesoftware.com/blog/2023/12/08/ice-binaries-for-omero.html
pip install -e .
```

Notes
- Editable install (-e) ensures your local package, including migrations, is importable.
- On Windows you can always prefix commands with .\.venv\Scripts\python -m ... instead of activating.

### 2) Ensure DB access for Alembic

Alembic autogenerate compares Models vs the live DB, so it must reach the database used by BIOMERO.importer.

Set the connection string as an environment variable:

```powershell
$env:INGEST_TRACKING_DB_URL = "postgresql://user:password@host:port/database"
```

If you are using the dev docker-compose, the Postgres service is typically exposed on a host port (e.g., 55432). Example:

```powershell
$env:INGEST_TRACKING_DB_URL = "postgresql://postgres:postgres@localhost:55432/biomero"
```

### 3) Make your SQLAlchemy model change

Edit the models in `biomero_importer/utils/ingest_tracker.py`. Keep changes minimal and run linters/tests as needed.

### 4) Generate a migration

The Alembic config is embedded under `biomero_importer/migrations/` and reads the URL from `INGEST_TRACKING_DB_URL`. Use python -m to avoid path issues.

```powershell
.\.venv\Scripts\python -m alembic -c biomero_importer\migrations\alembic.ini revision --autogenerate -m "your concise message"
```

Tips
- If Alembic reports “Target database is not up to date”, upgrade first (see next step) and re-run autogenerate.
- If you are adopting Alembic on an existing DB for the first time, see the optional “stamp” step below.

### 5) Apply migrations to your DB

Rebuild and restart your BIOMERO.importer container. The container will apply migrations automatically on startup when `ADI_RUN_MIGRATIONS=1` (default). This is handled by `biomero_importer/db_migrate.py` and uses a Postgres advisory lock to avoid races.

### 6) Commit the migration files

Add the new file(s) under `biomero_importer/migrations/versions/` to source control. These are included in the package so other environments (and the container) can run them.

### Optional: First-time adoption (stamp)

If your DB already has the BIOMERO.importer tables at the desired schema but no version table yet, you can baseline with a stamp so Alembic doesn't try to recreate history.

Two options:
1. Temporarily set `ADI_ALLOW_AUTO_STAMP=1` in the BIOMERO.importer container environment and restart the service once. The startup migration runner will stamp to head and then upgrade.
2. Or, run manually:
  ```powershell
  .\.venv\Scripts\python -m alembic -c biomero_importer\migrations\alembic.ini stamp head
  ```

After stamping, remove/disable the auto-stamp flag. Normal revisions and upgrades should be used going forward.

### How Alembic is scoped here

- Only BIOMERO.importer's tables are included via Alembic's `env.py` `include_object` filter. This prevents changes to other apps' tables in the same database.
- A dedicated version table `alembic_version_omeroadi` isolates BIOMERO.importer's migration history.

### Common issues

- autogenerate finds nothing: Ensure your model changes are in the BIOMERO.importer `Base` metadata and your `INGEST_TRACKING_DB_URL` points to the correct DB.
- autogenerate complains DB not up to date: Run upgrade head, then re-run autogenerate.
- Missing template `script.py.mako`: It's included under `biomero_importer/migrations/`; ensure your editable install points to your working tree.

### Quick reference

```powershell
# Set DB URL for alembic
$env:INGEST_TRACKING_DB_URL = "postgresql://postgres:postgres@localhost:55432/biomero"

# Create venv and install
py -3.12 -m venv .venv
# Install Ice binaries first (follow the blog instructions for your OS)
# https://www.glencoesoftware.com/blog/2023/12/08/ice-binaries-for-omero.html
.\.venv\Scripts\python -m pip install -e .

# Generate and apply migration
.\.venv\Scripts\python -m alembic -c biomero_importer\migrations\alembic.ini revision --autogenerate -m "add new column"
.\.venv\Scripts\python -m alembic -c biomero_importer\migrations\alembic.ini upgrade head
```


## Tests

Use a repository-local Python 3.12 virtual environment. Install the matching
[Glencoe Ice wheel](https://www.glencoesoftware.com/blog/2023/12/08/ice-binaries-for-omero.html)
before installing the test dependencies; do not build Ice from source.

```sh
python -m pip install -e '.[test,identity]'
python -m pip check
python -m pytest tests/unittests/ --cov=biomero_importer --cov-report=term-missing -v
```

GitHub Actions runs the same unit suite and lint checks for pull requests.

## Documentation

```sh
python -m pip install -r docs/requirements.txt
python -m mkdocs serve
python -m mkdocs build --strict
```

Pull requests build the documentation. Updates merged into main publish it to
[GitHub Pages](https://nl-bioimaging.github.io/BIOMERO.importer/).
The repository Pages source must be set to **GitHub Actions**.
