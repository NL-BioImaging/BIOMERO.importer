import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base


PACKAGE_ROOT = Path(__file__).parents[2] / "biomero_importer"


def _load_db_migrate(monkeypatch):
    package = ModuleType("biomero_importer")
    package.__path__ = [str(PACKAGE_ROOT)]
    utilities = ModuleType("biomero_importer.utils")
    utilities.__path__ = [str(PACKAGE_ROOT / "utils")]
    tracker = ModuleType("biomero_importer.utils.ingest_tracker")
    tracker.Base = declarative_base()
    tracker.CREATED_ANY_TABLES = False
    tracker.get_ingest_tracker = lambda: None
    monkeypatch.setitem(sys.modules, "biomero_importer", package)
    monkeypatch.setitem(sys.modules, "biomero_importer.utils", utilities)
    monkeypatch.setitem(
        sys.modules, "biomero_importer.utils.ingest_tracker", tracker
    )
    spec = importlib.util.spec_from_file_location(
        "biomero_importer.db_migrate", PACKAGE_ROOT / "db_migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _config(database_url, db_migrate):
    config = Config()
    config.set_main_option("script_location", db_migrate.MIGRATIONS_DIR)
    config.set_main_option("sqlalchemy.url", database_url)
    config.set_main_option("version_table", db_migrate.VERSION_TABLE)
    return config


def test_existing_import_order_survives_upgrade_to_import_options(
    tmp_path, monkeypatch
):
    db_migrate = _load_db_migrate(monkeypatch)
    database_url = f"sqlite:///{tmp_path / 'legacy-imports.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE imports (id INTEGER PRIMARY KEY, status TEXT)"
        ))

    config = _config(database_url, db_migrate)
    command.stamp(config, "base")
    command.upgrade(config, "78d3d6d84aec")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO imports (id, status, description) "
            "VALUES (1, 'Import Pending', 'legacy order')"
        ))

    monkeypatch.setenv("INGEST_TRACKING_DB_URL", database_url)
    monkeypatch.setenv("ADI_ALLOW_AUTO_STAMP", "0")
    monkeypatch.setenv("ADI_RUN_MIGRATIONS", "1")
    db_migrate.run_migrations_on_startup()

    columns = {column["name"] for column in inspect(engine).get_columns(
        "imports"
    )}
    assert "import_options" in columns
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT id, status, description, import_options "
            "FROM imports WHERE id = 1"
        )).one()
    assert tuple(row) == (1, "Import Pending", "legacy order", None)
