#!/usr/bin/env python
# ingest_tracker.py

import logging
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    DateTime,
    Text,
    Index,
    ForeignKey,
    JSON,
    event,
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import func, text
import json
# from sqlalchemy.dialects.postgresql import UUID  # unused
import psycopg2
import time

logger = logging.getLogger(__name__)


def _mask_url(url: str) -> str:
    """Mask password in a SQLAlchemy URL for safe logging.

    Examples:
    - postgresql+psycopg2://user:pass@host/db -> user:******@host/db
    - leaves URLs without credentials unchanged.
    """
    try:
        if not url or '://' not in url:
            return url
        scheme, rest = url.split('://', 1)
        if '@' not in rest:
            return url
        creds, tail = rest.split('@', 1)
        if ':' not in creds:
            return url
        user, _ = creds.split(':', 1)
        return f"{scheme}://{user}:******@{tail}"
    except Exception:
        return url

    
# --------------------------------------------------
# Stage constants
# --------------------------------------------------
STAGE_IMPORTED = "Import Completed"
STAGE_PREPROCESSING = "Import Preprocessing"
STAGE_NEW_ORDER = "Import Pending"
STAGE_INGEST_STARTED = "Import Started"
STAGE_INGEST_FAILED = "Import Failed"

Base = declarative_base()


# Set to True when Base.metadata.create_all created any tables in this process
CREATED_ANY_TABLES = False


class Preprocessing(Base):
    """Database model for storing preprocessing parameters.

        Migration warning:
            Changing columns or constraints requires a new Alembic migration.
            Generate and apply a revision for BIOMERO.importer (biomero_importer/migrations):

            PowerShell (Windows):
                $env:INGEST_TRACKING_DB_URL = \
                    'postgresql+psycopg2://USER:PASS@HOST:5432/DB'
                cd biomero-importer/biomero_importer
                alembic -c migrations/alembic.ini revision \
                    --autogenerate -m "explain change"

            Bash (Linux/macOS):
                export INGEST_TRACKING_DB_URL=\\
                    postgresql+psycopg2://USER:PASS@HOST:5432/DB
                cd biomero-importer/biomero_importer
                alembic -c migrations/alembic.ini revision \
                    --autogenerate -m "explain change"
                
            When you include the version file in git, it will get packaged
            when the library is installed (also in the Dockerfile) and
            run migration on startup of the BIOMERO.importer.

            Alternatively, manually run
                alembic -c migrations/alembic.ini upgrade head

            Notes:
                - Version table: alembic_version_omeroadi
                - Only BIOMERO.importer tables are included via env.py include_object
                
    """
    __tablename__ = 'imports_preprocessing'

    id = Column(Integer, primary_key=True)
    container = Column(String, nullable=False)
    input_file = Column(String, nullable=False)
    output_folder = Column(String, nullable=False)
    alt_output_folder = Column(String, nullable=True)
    extra_params = Column(JSON, nullable=True)  # Storing dynamic kwargs here


class IngestionTracking(Base):
    """Database model for tracking ingestion steps.

        Migration warning:
            Any schema change (add/remove column, type change, indexes) must be
            captured in a new Alembic revision and upgraded on the database.
            See commands in Preprocessing model docstring above.
    """
    __tablename__ = 'imports'

    id = Column(Integer, primary_key=True)
    group_name = Column(String, nullable=False)
    user_name = Column(String, nullable=False, index=True)
    destination_id = Column(String, nullable=False)
    destination_type = Column(String, nullable=False)
    stage = Column(String, nullable=False)
    uuid = Column(String(36), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), default=func.now())
    _files = Column("files", Text, nullable=False)
    _file_names = Column("file_names", Text, nullable=True)
    _import_options = Column("import_options", Text, nullable=True)
    # Optional human-readable description for the step (e.g., failure reason)
    description = Column(Text, nullable=True)

    preprocessing_id = Column(Integer, ForeignKey(
        'imports_preprocessing.id'), nullable=True)
    preprocessing = relationship("Preprocessing", backref="imports")

    @property
    def files(self):
        return json.loads(self._files)

    @files.setter
    def files(self, files):
        self._files = json.dumps(files)

    @property
    def file_names(self):
        return json.loads(self._file_names) if self._file_names else []

    @file_names.setter
    def file_names(self, file_names):
        self._file_names = json.dumps(file_names)

    @property
    def import_options(self):
        return json.loads(self._import_options) if self._import_options else {}

    @import_options.setter
    def import_options(self, import_options):
        self._import_options = (
            json.dumps(import_options) if import_options else None
        )


# Define the index outside of the class
Index(f"{IngestionTracking.__tablename__}_ix_uuid_timestamp",
      IngestionTracking.uuid, IngestionTracking.timestamp)


@event.listens_for(Base.metadata, 'after_create')
def receive_after_create(target, connection, tables, **kw):
    "listen for the 'after_create' event"
    # When SQLAlchemy actually creates one or more tables via create_all,
    # this event fires with the list of created Table objects.
    # We flip a module-level flag so the migrator can decide to auto-stamp.
    global CREATED_ANY_TABLES
    if tables:
        CREATED_ANY_TABLES = True


class IngestTracker:
    def __init__(self, config):
        if not config or 'ingest_tracking_db' not in config:
            raise ValueError("Configuration must include 'ingest_tracking_db'")

        self.logger = logging.getLogger(__name__)
        self.logger.info("Initializing IngestTracker")
        try:
            # Override with environment variable if available
            import os
            self.database_url = os.getenv(
                'INGEST_TRACKING_DB_URL', config['ingest_tracking_db'])
            self.logger.debug(
                f"Using database URL: {_mask_url(self.database_url)}"
            )

            connect_args = {}
            if self.database_url.startswith('sqlite'):
                connect_args['timeout'] = 5
            else:
                connect_args['connect_timeout'] = 5

            self.engine = create_engine(
                self.database_url,
                connect_args=connect_args,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
            self.Session = sessionmaker(bind=self.engine)

            # Verify connection
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            # Create tables if they do not exist
            self.logger.debug("Creating tables if they do not exist...")
            Base.metadata.create_all(self.engine)
            self.logger.info("Database initialization successful")
        except Exception as e:
            # For initialization, let the caller handle retry logic and messaging
            # Just re-raise for expected connection issues
            error_msg = str(e).lower()
            if any(phrase in error_msg for phrase in [
                'name or service not known',
                'connection refused',
                'could not connect',
                'network is unreachable',
                'timeout expired',
                'server closed the connection'
            ]):
                # Don't log - let the caller handle this
                raise
            else:
                self.logger.error(
                    f"Unexpected error during IngestTracker initialization: {e}",
                    exc_info=True,
                )
                raise

    def dispose(self):
        """Safely dispose of database resources."""
        try:
            if hasattr(self, 'engine'):
                self.engine.dispose()
                self.logger.debug("Database resources disposed")
        except Exception as e:
            self.logger.error(f"Error disposing database resources: {e}")

    def db_log_ingestion_event(
        self, order_info: dict, stage: str, max_retries: int = 3
    ):
        """
        Log an ingestion step to the database.
        Returns the new entry ID or None if logging failed.
        """
        required_fields = ['Group', 'Username', 'UUID', 'DestinationID']
        if not all(field in order_info for field in required_fields):
            self.logger.error(
                f"Missing required fields in order_info: {required_fields}")
            return None

        self.logger.debug(
            "Logging ingestion event - Stage: %s, UUID: %s, Order: %s",
            stage,
            order_info.get('UUID'),
            order_info,
        )
        for attempt in range(max_retries):
            try:
                with self.Session() as session:
                    new_entry = IngestionTracking(
                        group_name=order_info.get('Group'),
                        user_name=order_info.get('Username', 'Unknown'),
                        destination_id=str(
                            order_info.get('DestinationID', '')),
                        destination_type=str(
                            order_info.get('DestinationType', '')),
                        stage=stage,
                        uuid=str(order_info.get('UUID', 'Unknown')),
                        files=order_info.get('Files', []),
                        file_names=order_info.get('FileNames', []),
                        import_options=order_info.get('ImportOptions', {}),
                        description=(
                            order_info.get('Description')
                            or order_info.get('description')
                        )
                    )

                    session.add(new_entry)

                    # Check for _preprocessing_id in order_info
                    preprocessing_id = order_info.get('_preprocessing_id')
                    if preprocessing_id:
                        # Directly set preprocessing_id without querying object
                        new_entry.preprocessing_id = preprocessing_id
                    elif "preprocessing_container" in order_info:
                        # Known hardcoded preprocessing fields
                        hardcoded_fields = {
                            "container": order_info.get(
                                "preprocessing_container"
                            ),
                            "input_file": order_info.get(
                                "preprocessing_inputfile"
                            ),
                            "output_folder": order_info.get(
                                "preprocessing_outputfolder"
                            ),
                            "alt_output_folder": order_info.get(
                                "preprocessing_altoutputfolder"
                            ),
                        }

                        # Handle extra_params from direct field first
                        extra_params = order_info.get("extra_params", {})

                        # Also extract any preprocessing_ fields not hardcoded
                        dynamic_params = {
                            key.replace("preprocessing_", ""): value
                            for key, value in order_info.items()
                            if (
                                key.startswith("preprocessing_")
                                and key not in {
                                    "preprocessing_container",
                                    "preprocessing_inputfile",
                                    "preprocessing_outputfolder",
                                    "preprocessing_altoutputfolder",
                                }
                            )
                        }

                        # Merge both sources - dynamic overrides direct
                        if dynamic_params:
                            extra_params.update(dynamic_params)

                        new_preprocessing = Preprocessing(
                            **hardcoded_fields,
                            extra_params=extra_params if extra_params else None
                        )
                        session.add(new_preprocessing)
                        # Link the new preprocessing row to the ingestion entry
                        new_entry.preprocessing = new_preprocessing

                    session.commit()

                    self.logger.info(
                        f"Ingestion event logged: {stage} | "
                        f"UUID: {new_entry.uuid} | "
                        f"Group: {new_entry.group_name} | "
                        f"User: {new_entry.user_name} | "
                        f"DestinationID: {new_entry.destination_id} | "
                        f"DestinationType: {new_entry.destination_type} | "
                        f"Preprocessing: {new_entry.preprocessing}"
                    )
                    self.logger.debug(
                        f"Created database entry: {new_entry.id}")
                    return new_entry.id
            except (SQLAlchemyError, psycopg2.OperationalError,
                    psycopg2.DatabaseError) as e:
                error_msg = str(e).lower()
                is_connection_issue = any(phrase in error_msg for phrase in [
                    "closed the connection",
                    "name or service not known",
                    "connection refused",
                    "could not connect",
                    "network is unreachable",
                    "timeout expired",
                    "pgres_tuples_ok",
                    "does not return rows",
                    "server closed the connection",
                    "connection already closed",
                    "ssl connection has been closed",
                ])
                
                if is_connection_issue and attempt < max_retries - 1:
                    wait_time = 0.5 * (2 ** attempt)  # Exponential backoff
                    self.logger.warning(
                        f"Database connection issue "
                        f"(attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s: {e}"
                    )
                    time.sleep(wait_time)
                    continue
                elif is_connection_issue:
                    self.logger.warning(
                        f"Database connection persistently failing "
                        f"after {max_retries} attempts: {e}"
                    )
                else:
                    self.logger.error(
                        f"Database error logging ingestion step: {e}",
                        exc_info=True,
                    )
                return None
            except Exception as e:
                self.logger.error(
                    f"Unexpected error logging ingestion step: {e}",
                    exc_info=True,
                )
                return None


# --------------------------------------------------
# Global instance management
# --------------------------------------------------
_ingest_tracker = None


def get_ingest_tracker():
    """Return the current global IngestTracker instance."""
    return _ingest_tracker


def initialize_ingest_tracker(config):
    """Initialize the global IngestTracker instance."""
    global _ingest_tracker
    try:
        _ingest_tracker = IngestTracker(config)
        if not hasattr(_ingest_tracker, 'engine'):
            raise Exception("IngestTracker failed to initialize properly")
        return True
    except Exception as e:
        # For initialization, we want the caller to handle retries
        # Just silently return False for expected connection issues
        error_msg = str(e).lower()
        if any(phrase in error_msg for phrase in [
            'name or service not known',
            'connection refused',
            'could not connect',
            'network is unreachable',
            'timeout expired'
        ]):
            # Don't log anything - let the caller handle retry messaging
            pass
        else:
            logger.error(f"Failed to initialize IngestTracker: {e}", exc_info=True)
        _ingest_tracker = None
        return False


def prepare_ingest_tracker_for_worker(config):
    """Ensure a process-pool worker never uses the parent's DB connections.

    ``ProcessPoolExecutor`` forks the already-initialized tracker on Linux.
    SQLAlchemy engines may be shared between processes, but their pooled DBAPI
    connections may not. Replacing the inherited pool in the child preserves
    the parent's connections while making the child's first checkout create a
    process-local connection.

    On platforms that spawn workers instead of forking them, no tracker is
    inherited, so initialize one from the worker's copy of the configuration.
    """
    tracker = get_ingest_tracker()
    if tracker is not None:
        tracker.engine.dispose(close=False)
        logger.debug("Replaced inherited database pool in worker process")
        return True

    if initialize_ingest_tracker(config):
        logger.debug("Initialized database pool in spawned worker process")
        return True

    raise RuntimeError("Failed to initialize ingest tracker in worker process")


def log_ingestion_step(order_info, stage):
    """Thread-safe function to log ingestion steps."""
    tracker = get_ingest_tracker()
    if tracker is not None:
        return tracker.db_log_ingestion_event(order_info, stage)
    else:
        logger.error(
            "IngestTracker not initialized. Call initialize_ingest_tracker "
            "first."
        )
        return None
