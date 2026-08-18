#!/usr/bin/env python
# main.py

import time
import signal
import datetime
import yaml
import json
import sys
import logging
from concurrent.futures import ProcessPoolExecutor
from threading import Event, Thread
from collections import UserDict

# Local module imports
from .utils.initialize import finalize_dangling_orders_and_get_last_id, initialize_system, test_omero_connection
from .utils.upload_order_manager import UploadOrderManager
from .utils.importer import DataPackageImporter
from .utils.ingest_tracker import (
    log_ingestion_step,
    prepare_ingest_tracker_for_worker,
    STAGE_NEW_ORDER,
    STAGE_INGEST_STARTED,
    STAGE_INGEST_FAILED,
    STAGE_IMPORTED,
    _mask_url,
)
from .utils.ingest_tracker import IngestionTracking, Base, get_ingest_tracker
from .db_migrate import run_migrations_on_startup

# --------------------------------------------------
# Utility function to load settings
# --------------------------------------------------


def load_config(settings_path="config/settings.yml"):
    """Load settings from either a YAML or JSON file."""
    with open(settings_path, 'r') as file:
        if settings_path.endswith(('.yml', '.yaml')):
            return yaml.safe_load(file)
        elif settings_path.endswith('.json'):
            return json.load(file)
        else:
            raise ValueError(f"Unsupported file format: {settings_path}")

# --------------------------------------------------
# ProcessPoolExecutor creation
# --------------------------------------------------


def initialize_worker(config):
    """Initialize logging and process-local database resources in a worker."""
    log_level = config.get('log_level', 'INFO').upper()
    log_file = config.get('log_file_path', 'logs/app.logs')
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format='%(asctime)s - PID:%(process)d - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    # Add specific logger for ezomero
    logging.getLogger('ezomero').setLevel(logging.DEBUG)
    # Reduce noise from OMERO gateway during startup
    logging.getLogger('omero.gateway').setLevel(logging.INFO)
    logging.getLogger('omero.client').setLevel(logging.INFO)
    logging.getLogger('omero.util.Resources').setLevel(logging.INFO)

    prepare_ingest_tracker_for_worker(config)


def create_executor(config):
    """Create a ProcessPoolExecutor with per-worker initialization."""
    return ProcessPoolExecutor(
        max_workers=config.get('max_workers', 4),
        initializer=initialize_worker,
        initargs=(config,),
    )


def log_flag(logger, flag_type):
    line_pattern = "    /\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/\\/"
    if flag_type == 'start':
        logger.info("\n" + line_pattern +
                    "\n           READY TO UPLOAD DATA TO OMERO\n" + line_pattern)
    elif flag_type == 'end':
        logger.info("\n" + line_pattern +
                    "\n           STOPPING AUTOMATIC UPLOAD SERVICE\n" + line_pattern)

# --------------------------------------------------
# DataPackage and IngestionProcess
# --------------------------------------------------


class DataPackage(UserDict):
    """
    A dictionary-like object that represents a data package for OMERO upload orders.

    This class holds information for an upload order including:
    - DestinationID: The target OMERO Dataset/Screen ID where the data is to be uploaded.
    - Unique order identifier (UUID)
    - Group and user information
    - File paths and original file names to be uploaded

    Example order data:
        {
            "Group": "Test Group",
            "Username": "ttest",
            "DestinationID": "1",  # Target Dataset/Screen ID in OMERO
            "UUID": "550e8400-e29b-41d4-a716-446655440000",
            "Files": [
                "/auto-importer/tests/Barbie1.tif",
                "/auto-importer/tests/Barbie2.tif",
                "/auto-importer/tests/Barbie3.tif"
            ],
            "FileNames": [
                "Barbie1.tif",
                "Barbie2.tif",
                "Barbie3.tif"
            ]
        }
    """

    def __init__(self, order_data, base_dir=None, order_identifier=None):
        self.data = order_data
        self.base_dir = base_dir
        self.order_identifier = order_identifier

    def get(self, key, default=None):
        return self.data.get(key, default)


class IngestionProcess:
    """
    Handles the ingestion process for a data package.
    """

    def __init__(self, data_package, config, order_manager):
        """
        :param data_package: DataPackage object.
        :param config: Configuration dictionary.
        :param order_manager: UploadOrderManager object.
        """
        self.data_package = data_package
        self.config = config
        self.order_manager = order_manager
        self.logger = logging.getLogger(__name__)

    def import_data_package(self):
        """
        Import the data package and return the UUID for tracking.
        If the importer returns a failure (or no files are uploaded), log STAGE_INGEST_FAILED.
        """
        try:
            importer = DataPackageImporter(self.config, self.data_package)
            successful_uploads, failed_uploads, import_failed = importer.import_data_package()
            destination_id = self.data_package.get('DestinationID')
            package_uuid = self.data_package.get('UUID')

            if import_failed or failed_uploads or (not successful_uploads and not failed_uploads):
                error_msg = (
                    f"Import failed for data package {package_uuid} targeting OMERO {destination_id}. "
                    f"Failed files: {[f[0] for f in failed_uploads]}"
                )
                self.logger.error(error_msg)
                # include description for failure tracking
                failed_pkg = dict(self.data_package)
                if 'Description' not in failed_pkg:
                    failed_pkg['Description'] = error_msg
                log_ingestion_step(failed_pkg, STAGE_INGEST_FAILED)
            else:
                self.logger.info(
                    f"Successfully imported data package {package_uuid} to OMERO {destination_id}. "
                    f"Imported {len(successful_uploads)} files: {[f[2] for f in successful_uploads]}"
                )
                log_ingestion_step(self.data_package, STAGE_IMPORTED)

            if successful_uploads:
                self.logger.debug(
                    f"Successful uploads details for {package_uuid}:\n" +
                    "\n".join(
                        [f"- {f[2]} -> ID: {f[3]}" for f in successful_uploads])
                )

            return package_uuid

        except Exception as e:
            error_msg = f"Exception during import_data_package: {e}"
            self.logger.error(error_msg, exc_info=True)
            failed_pkg = dict(self.data_package)
            failed_pkg['Description'] = error_msg
            log_ingestion_step(failed_pkg, STAGE_INGEST_FAILED)
            return self.data_package.get('UUID')

# TODO: move the poller to the database logic?
# --------------------------------------------------
# DatabasePoller: Polls the DB for new upload orders
# --------------------------------------------------


class DatabasePoller:
    """
    Polls the database for new upload orders.
    New orders are identified by the Stage STAGE_NEW_ORDER.
    """

    def __init__(self, config, executor, poll_interval=5):
        self.config = config
        self.executor = executor
        self.poll_interval = poll_interval
        self.shutdown_event = Event()
        self.logger = logging.getLogger(f"{__name__}.db_poller")
        # Maintain a set of processed order UUIDs to avoid reprocessing.
        self.processed_uuids = set()

        self.ingest_tracker = get_ingest_tracker()  # global instance
        self.IngestionTracking = IngestionTracking

        # Log poller state without leaking credentials
        safe_state = {
            k: (
                {ck: (_mask_url(cv) if 'db' in ck else cv)
                 for ck, cv in v.items()}
                if isinstance(v, dict) else v
            )
            for k, v in self.__dict__.items()
        }
        self.logger.debug(f"Poller ready: {safe_state}")

        # Ensure tables exist
        Base.metadata.create_all(self.ingest_tracker.engine)

        # Setup for new orders
        self.last_max_id = finalize_dangling_orders_and_get_last_id(
            self.ingest_tracker, self.IngestionTracking, days=10)

    def start(self):
        """Start the polling thread."""
        self.poller_thread = Thread(target=self.poll_database)
        self.poller_thread.start()

    def stop(self):
        """Stop the polling thread."""
        self.shutdown_event.set()
        self.poller_thread.join()

    def poll_database(self) -> None:
        """
        Continuously polls the database for new upload orders and processes each new order.
        Only process an order if its UUID has not been processed before.
        """
        Session = self.ingest_tracker.Session
        consecutive_failures = 0
        max_consecutive_failures = 5
        
        while not self.shutdown_event.is_set():
            with Session() as session:
                try:
                    new_orders = session.query(self.IngestionTracking).filter(
                        self.IngestionTracking.stage == STAGE_NEW_ORDER,
                        self.IngestionTracking.id > self.last_max_id
                    ).order_by(self.IngestionTracking.id.asc()).all()
                    
                    # Reset failure counter on successful query
                    consecutive_failures = 0
                    
                    for order in new_orders:
                        if order.uuid in self.processed_uuids:
                            continue  # Skip if this order has been processed already
                        self.last_max_id = max(self.last_max_id, order.id)
                        # Build a clean dictionary from the model attributes.
                        order_dict = {
                            'Group': order.group_name,
                            'Username': order.user_name,
                            'UUID': order.uuid,
                            'DestinationID': order.destination_id,
                            'DestinationType': order.destination_type,
                            'Files': order.files,
                            'FileNames': order.file_names
                        }

                        # Add preprocessing details if they exist
                        if order.preprocessing:
                            order_dict.update({
                                "preprocessing_container": order.preprocessing.container,
                                "preprocessing_inputfile": order.preprocessing.input_file,
                                "preprocessing_outputfolder": order.preprocessing.output_folder,
                                "preprocessing_altoutputfolder": order.preprocessing.alt_output_folder,
                                "_preprocessing_id": order.preprocessing.id
                            })
                            # Add all extra parameters
                            if order.preprocessing.extra_params:
                                for key, value in order.preprocessing.extra_params.items():
                                    order_dict[f"preprocessing_{key}"] = value
                        self.process_order(order_dict)
                        self.processed_uuids.add(order.uuid)
                except Exception as e:
                    consecutive_failures += 1
                    
                    # Handle database connection issues gracefully without scary stacktraces
                    error_msg = str(e).lower()
                    if any(phrase in error_msg for phrase in [
                        'server closed the connection',
                        'name or service not known',
                        'connection refused',
                        'could not connect',
                        'network is unreachable',
                        'timeout expired'
                    ]):
                        if consecutive_failures <= max_consecutive_failures:
                            self.logger.warning(f"Database temporarily unavailable (failure {consecutive_failures}/{max_consecutive_failures}): {e}")
                        else:
                            self.logger.error(f"Database connection persistently failing after {consecutive_failures} attempts: {e}")
                    else:
                        self.logger.error(f"Error polling database: {e}", exc_info=True)
                        
            # Use exponential backoff for repeated failures
            if consecutive_failures > 0:
                sleep_time = min(self.poll_interval * (2 ** min(consecutive_failures - 1, 5)), 60)
                time.sleep(sleep_time)
            else:
                time.sleep(self.poll_interval)

    def process_order(self, order_dict) -> None:
        """
        Process a single new order by logging, creating a DataPackage, and submitting an ingestion process.
        Uses the provided dictionary (built from the model) to preserve original attributes.
        """
        uuid_val = order_dict.get('UUID')
        self.logger.info(f"Detected new upload order with UUID {uuid_val}")

        # Log the new order using the original attributes.
        # log_ingestion_step(order_dict, STAGE_NEW_ORDER)
        # Mark that ingest has started.
        log_ingestion_step(order_dict, STAGE_INGEST_STARTED)

        try:
            order_manager = UploadOrderManager.from_order_record(
                order_dict, self.config)
            order_dict = order_manager.get_order_info()  # after validation / reforming
            data_package = DataPackage(order_dict, order_identifier=uuid_val)
            self.logger.debug(f"{data_package} || {order_manager}")
            ingestion_process = IngestionProcess(
                data_package, self.config, order_manager)
            future = self.executor.submit(
                ingestion_process.import_data_package)
            future.add_done_callback(self.create_order_callback(uuid_val))
        except Exception as e:
            err = f"Error processing order {order_dict.get('UUID')}: {e}"
            self.logger.error(err, exc_info=True)
            failed = dict(order_dict)
            failed['Description'] = err
            log_ingestion_step(failed, STAGE_INGEST_FAILED)
            raise e

    def create_order_callback(self, uuid_val: str):
        """
        Returns a callback function that logs when the order processing is complete.
        """
        def callback(future) -> None:
            self.logger.info(f"Order {uuid_val} processing complete.")
        return callback


# --------------------------------------------------
# Application run loop
# --------------------------------------------------
def run_application(config: dict, groups_info, executor) -> None:
    """
    Runs the main application loop.
    """
    logger = logging.getLogger(__name__)
    shutdown_event = Event()
    shutdown_timeout = config.get('shutdown_timeout', 30)

    def graceful_exit(signum, frame):
        logger.info("Graceful shutdown initiated.")
        shutdown_event.set()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    # Start the DatabasePoller
    db_poller = DatabasePoller(config, executor)
    db_poller.start()
    log_flag(logger, 'start')
    start_time = datetime.datetime.now()

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    finally:
        logger.info("Initiating shutdown sequence...")
        log_flag(logger, 'end')
        db_poller.stop()
        executor.shutdown(wait=True)
        end_time = datetime.datetime.now()
        logger.info(
            f"Program completed. Total runtime: {end_time - start_time}")


def main():
    logger = logging.getLogger(__name__)
    try:
        config = load_config()
        log_level = config.get('log_level', 'DEBUG').upper()
        log_file = config['log_file_path']

        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format='%(asctime)s - PID:%(process)d - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        # Add specific logger for ezomero
        logging.getLogger('ezomero').setLevel(logging.DEBUG)
        # Reduce noise from OMERO gateway during startup
        logging.getLogger('omero.gateway').setLevel(logging.INFO)
        logging.getLogger('omero.client').setLevel(logging.INFO)

        logger.info("Starting application...")
        
        logger.info("Testing OMERO connectivity...")
        # Wait for OMERO to be ready before proceeding
        if not test_omero_connection():
            logger.error("Could not establish OMERO connection. Exiting.")
            return

        logger.info("Initializing system (create tables if missing)...")
        # Wait for database to be ready before proceeding
        initialize_system(config)

        logger.info("Checking database migrations...")
        # Run database migrations after tables exist for fresh installs
        run_migrations_on_startup()

        
        logger.info("Creating process executor...")
        executor = create_executor(config)
        groups_info = None
        run_application(config, groups_info, executor)
    except Exception as e:
        logger.error("Fatal error in main: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
