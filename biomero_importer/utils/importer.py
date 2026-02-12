import os
import csv
import shutil
import logging
import functools
import time
from subprocess import Popen, PIPE, STDOUT
from importlib import import_module
from pathlib import PurePath

import ezomero
from omero.gateway import BlitzGateway
from omero.rtypes import rstring, rlong
from omero.cli import CLI
from omero.plugins.sessions import SessionsControl
from omero.model import DatasetI
from omero.sys import Parameters

from .ingest_tracker import log_ingestion_step, STAGE_PREPROCESSING

# Import the ImportControl from the OMERO plugins (if needed)
ImportControl = import_module("omero.plugins.import").ImportControl

MAX_RETRIES = 5  # Maximum number of retries
RETRY_DELAY = 5  # Delay between retries (in seconds)
IMPORT_MAX_RETRIES = 3  # Maximum retries for transient import errors (e.g. Ice race conditions)
IMPORT_RETRY_DELAY = 10  # Delay between import retries (in seconds)
TMP_OUTPUT_FOLDER = "OMERO_inplace"
PROCESSED_DATA_FOLDER = ".processed"

# Error patterns that indicate a transient/retryable import failure
# (e.g. concurrent Ice session race conditions)
RETRYABLE_IMPORT_ERRORS = [
    'Ice.ObjectNotExistException',
    'INTERNAL_EXCEPTION',
    'Ice.ConnectionLostException',
    'Ice.ConnectionRefusedException',
    'Ice.TimeoutException',
]

# Canonical keys for storing preprocessing artifacts on the data_package
PREPROC_META_KEY = "_preprocessing_metadata"
PREPROC_RESULTS_KEY = "_preprocessing_results"
PREPROC_RENAME_MAP_KEY = "_preprocessing_rename_map"

# Keys inside each item of PREPROC_RESULTS_KEY
PREPROC_RESULT_NAME = "name"
PREPROC_RESULT_LOCAL_ALT = "local_alt_path"
PREPROC_RESULT_LOCAL_FULL = "local_full_path"
PREPROC_RESULT_METADATA = "metadata"


def get_tmp_output_path(data_package):
    """
    Helper function to generate the temporary output folder path.
    """
    return os.path.join("/OMERO", TMP_OUTPUT_FOLDER, data_package.get('UUID'))


def is_retryable_import_error(errs_file, logger=None):
    """
    Check if an import error file contains a retryable (transient) error.

    Reads the .errs file produced by the OMERO CLI import and checks for
    known transient error patterns (e.g. Ice.ObjectNotExistException from
    concurrent import race conditions).

    Returns (True, matched_pattern) if retryable, (False, None) otherwise.
    """
    if not os.path.exists(errs_file):
        if logger:
            logger.warning(
                f"No .errs file found at {errs_file} — import likely crashed "
                f"before writing output. Treating as retryable."
            )
        return True, "no_errs_file"
    try:
        with open(errs_file, 'r') as f:
            content = f.read()
        for pattern in RETRYABLE_IMPORT_ERRORS:
            if pattern in content:
                if logger:
                    logger.warning(
                        f"Retryable error detected in {errs_file}: {pattern}"
                    )
                return True, pattern
    except Exception as e:
        if logger:
            logger.warning(f"Could not read errs file {errs_file}: {e}")
    return False, None


def connection(func):
    """
    A decorator that wraps a function so that it receives an OMERO user connection.
    If a connection is already provided (as the first positional argument after self), it is reused.
    """
    @functools.wraps(func)
    def wrapper_connection(self, *args, **kwargs):
        # If a connection is already provided, simply call the function.
        if args and hasattr(args[0], "keepAlive"):
            return func(self, *args, **kwargs)
        try:
            with BlitzGateway(self.user, self.password, host=self.host, port=self.port, secure=True) as root_conn:
                self.logger.debug("Connected as root to OMERO.")
                if root_conn.connect():
                    # Retrieve order-specific connection details from the data package.
                    intended_username = self.data_package.get('Username')
                    group_name = self.data_package.get('Group')
                    group_id = ezomero.get_group_id(
                        root_conn, group_name)  # grab w/ ezomero
                    self.logger.debug(f"Using TTL: {self.ttl_for_user_conn}")
                    with root_conn.suConn(intended_username, ttl=self.ttl_for_user_conn) as user_conn:
                        user_conn.keepAlive()
                        self.logger.debug(
                            f"Connected as user {intended_username}")
                        user_conn.setGroupForSession(group_id)
                        self.logger.debug(f"Session group set to {group_name}")
                        return func(self, user_conn, *args, **kwargs)
                else:
                    raise ConnectionError(
                        "Could not connect to the OMERO server as root.")
        except Exception as e:
            self.logger.error(
                f"Exception in connection wrapper: {e}", exc_info=True)
            raise
    return wrapper_connection


def retry_on_connection_issue(func):
    """
    A decorator to retry a function when connection issues occur.
    If an exception with 'connect' in its message is raised, the function is retried.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Assumes the first argument is 'self' with a logger
        logger = args[0].logger
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "connect" in str(e).lower():
                    logger.error(
                        f"Connection issue (attempt {attempt}/{MAX_RETRIES}): {e}")
                    if attempt < MAX_RETRIES:
                        logger.info(f"Retrying in {RETRY_DELAY} seconds...")
                        time.sleep(RETRY_DELAY)
                        continue
                raise
        raise ValueError(
            f"Max retries ({MAX_RETRIES}) reached in {func.__name__}")
    return wrapper


class DataProcessor:
    def __init__(self, data_package, logger=None):
        """Initialize DataProcessor with proper logging."""
        self.data_package = data_package
        self.logger = logger or logging.getLogger(__name__)
        self.logger.debug(
            f"Initializing DataProcessor for package: {data_package.get('UUID', 'Unknown')}")

    def has_preprocessing(self):
        """Check if any preprocessing options are specified in the data package."""
        return any(key.startswith("preprocessing_") for key in self.data_package)

    def get_preprocessing_args(self, file_path):
        """Generate podman command arguments based on preprocessing keys."""
        self.logger.debug(f"Getting preprocessing args for file: {file_path}")
        if not self.has_preprocessing():
            self.logger.info("No preprocessing options found.")
            return None, None, None

        container = self.data_package.get("preprocessing_container")
        if not container:
            self.logger.warning(
                "No 'preprocessing_container' defined in data package.")
            raise ValueError(
                "Missing required 'preprocessing_container' in data package.")

        if not container.startswith("docker.io/"):
            container = "docker.io/" + container

        output_folder = self.data_package.get("preprocessing_outputfolder")
        input_file = self.data_package.get("preprocessing_inputfile")
        if not output_folder or not input_file:
            self.logger.warning(
                f"Missing output or input file: output_folder={output_folder}, input_file={input_file}")
            raise ValueError(
                "Missing required 'preprocessing_outputfolder' or 'preprocessing_inputfile' in data package.")

        kwargs = []
        mount_paths = []
        mount_path = None
        for key, value in self.data_package.items():
            if key.startswith("preprocessing_") and key not in ("preprocessing_container", "preprocessing_outputfolder", "preprocessing_altoutputfolder"):
                self.logger.debug(f"Found {key}:{value}")
                if isinstance(value, str) and "{Files}" in value: # handle inputfile = {Files}
                    self.logger.debug(f"Found Files in {key}:{value}")
                    if file_path:
                        data_file_path = os.path.join(
                            output_folder, os.path.basename(file_path))
                        self.logger.debug(
                            f"Replacing {file_path} ({data_file_path}) for Files in {key}:{value}")
                        value = value.replace("{Files}", data_file_path)
                        mount_path = os.path.dirname(file_path)
                        self.logger.debug(f"Set mount_path to {mount_path}")
                arg_key = key.replace("preprocessing_", "")
                kwargs.append(f"--{arg_key}")
                kwargs.append(value)
        self.logger.debug(f"Found extra preproc kwargs: {kwargs}")


        mount_paths.append((mount_path, output_folder))
        self.logger.debug(
            f"Output folder mount: {mount_path} --> {output_folder}")
        
        # Setup a processed subfolder for the processed outputs
        proc_output_folder = os.path.join(output_folder, PROCESSED_DATA_FOLDER)
        real_proc_output_folder = os.path.join(mount_path, PROCESSED_DATA_FOLDER)
        os.makedirs(real_proc_output_folder, exist_ok=True)
        self.logger.debug(f"Created processed subfolder at {real_proc_output_folder} and giving container --outputfolder {proc_output_folder}")
        kwargs += ["--outputfolder", proc_output_folder]
        
        alt_output_folder = self.data_package.get(
            "preprocessing_altoutputfolder")
        if alt_output_folder:
            kwargs += ["--altoutputfolder", alt_output_folder]
            relative_output_path = get_tmp_output_path(self.data_package)
            mount_paths.append((relative_output_path, alt_output_folder))
            self.logger.debug(
                f"Alt output folder mount: {relative_output_path} --> {alt_output_folder}")
        else:
            self.logger.error("Missing altoutputfolder. Not handled yet.")
            return None, None, None

        return container, kwargs, mount_paths

    def log_subprocess_output(self, pipe):
        for line in iter(pipe.readline, b''):
            self.logger.debug('sub: %r', line)

    def build_podman_command(self, file_path):
        """Construct the full Podman command based on preprocessing parameters.

        Returns (podman_command, mount_paths) so mount_paths can be reused
        as the single source of truth for container<->host mapping.
        """
        container, kwargs, mount_paths = self.get_preprocessing_args(file_path)
        if not container:
            self.logger.warning("No container specified for podman command.")
            return None, None

        podman_settings = ["podman", "run", "--rm"]

        # Check if user namespace mapping is available/desired
        userns_mode = os.getenv("PODMAN_USERNS_MODE", "auto").lower()

        if userns_mode == "keep-id":
            podman_settings.append("--userns=keep-id")
            self.logger.debug("Added --userns=keep-id for user namespace mapping. This allows running non-root containers.")
        else:
            self.logger.debug(f"Using default podman user mapping (userns_mode: {userns_mode}). This means only root containers can be run.")

        for src, dst in mount_paths:
            podman_settings += ["-v", f"{src}:{dst}"]
        podman_command = podman_settings + [container] + kwargs
        self.logger.info(f"Podman command: {' '.join(podman_command)}")
        return podman_command, mount_paths
    
    def run(self, dry_run=False):
        """Execute preprocessing containers and parse their JSON output.

        Contract:
        - Container must emit a single JSON line on stdout as the last line.
        - JSON is a list of items with keys: name, full_path, alt_path,
          and optional keyvalues (list[dict] or dict).
        - Persist structured results and metadata on data_package under
          PREPROC_RESULTS_KEY and PREPROC_META_KEY.

        Return: (success: bool, processed_paths: list[str]) where processed_paths
        are host paths mapped from alt_path using the same mount_paths passed to podman.
        """
        if not self.has_preprocessing():
            self.logger.info("No preprocessing required.")
            return True, []

        file_paths = self.data_package.get("Files", [])
        processed_files = []
        metadata_dict = {}
        preproc_results = []

        for file_path in file_paths:
            self.logger.info(f"Preprocessing file: {file_path}")
            podman_command, mount_paths = self.build_podman_command(file_path)
            if not podman_command:
                self.logger.error("Failed to build podman command.")
                return False, []

            if dry_run:
                self.logger.info(f"Dry run: {' '.join(podman_command)}")
                continue

            process = Popen(podman_command, stdout=PIPE, stderr=STDOUT)
            output_lines = []

            with process.stdout:
                for line in iter(process.stdout.readline, b''):
                    line_str = line.decode().strip()
                    output_lines.append(line_str)
                    self.logger.debug('sub: %r', line)

            if process.wait() != 0:
                self.logger.error("Podman command failed.")
                return False, []

            self.logger.info("Podman command executed successfully.")

            if not output_lines:
                self.logger.error("No output from preprocessor container.")
                return False, []

            try:
                import json
                raw_last = output_lines[-1]
                json_output = json.loads(raw_last)
                if not isinstance(json_output, list):
                    raise TypeError("Expected a list of result items from preprocessor")
                self.logger.debug(f"Found JSON output: {json_output}")

                # Use mount_paths as the single source of truth for mapping
                def to_host_path(container_path: str) -> str | None:
                    if not container_path:
                        return None
                    for host_src, cont_dst in (mount_paths or []):
                        if host_src and cont_dst and container_path.startswith(cont_dst.rstrip('/') + '/'):
                            rel = container_path[len(cont_dst.rstrip('/')) + 1:]
                            return os.path.join(host_src, rel)
                    return None

                if len(json_output) > 1:
                    self.logger.warning(
                        f"Preprocessor returned {len(json_output)} results for a single input. "
                        "Handling multiple outputs per input is not implemented yet: Only first will be processed."
                    )

                for item in json_output:
                    name = item.get('name')
                    full_path = item.get('full_path')
                    alt_path = item.get('alt_path')

                    local_alt = to_host_path(alt_path)
                    local_full = to_host_path(full_path)

                    if local_alt:
                        processed_files.append(local_alt)
                        self.logger.debug(
                            f"Mapped alt_path -> host: {alt_path} -> {local_alt}")
                    else:
                        self.logger.warning(
                            "No alt_path mapping produced a host path; item will be skipped for import")

                    merged_md = {}
                    kvs = item.get('keyvalues')
                    if isinstance(kvs, list):
                        for kv_dict in kvs:
                            if isinstance(kv_dict, dict):
                                merged_md.update(kv_dict)
                    elif isinstance(kvs, dict):
                        merged_md.update(kvs)

                    if merged_md and local_alt:
                        metadata_dict[local_alt] = merged_md
                        self.logger.debug(
                            (
                                f"Collected metadata for {local_alt}: "
                                f"{merged_md}"
                            )
                        )

                    # Rename decision based on output: use the container's full name choice for OMERO
                    if name and name.strip() and (full_path or alt_path) and local_alt:
                        file_for_ext = full_path or alt_path
                        fname = os.path.basename(file_for_ext)
                        desired_name = str(name).strip()
                        
                        # Only rename if the desired name is different from the current filename
                        if desired_name != fname:
                            # Store map keyed by the actual local path used for import
                            # The container has full control over the OMERO name (including extensions)
                            rename_map = (
                                self.data_package.get(
                                    PREPROC_RENAME_MAP_KEY, {}
                                )
                                or {}
                            )
                            rename_map[str(local_alt)] = desired_name
                            self.data_package[PREPROC_RENAME_MAP_KEY] = rename_map
                            self.logger.debug(
                                (
                                    "Rename needed: container specified different name; "
                                    f"original='{fname}', desired='{desired_name}'. "
                                    f"Recorded rename for {local_alt} -> '{desired_name}'"
                                )
                            )

                    result_item = {
                        PREPROC_RESULT_NAME: name,
                        PREPROC_RESULT_LOCAL_ALT: local_alt,
                        PREPROC_RESULT_LOCAL_FULL: local_full,
                        PREPROC_RESULT_METADATA: merged_md or None,
                    }
                    preproc_results.append(result_item)
                    self.logger.debug(
                        f"Stored result item: {result_item}"
                    )

            except Exception as e:
                self.logger.error(
                    "Invalid or missing JSON output from preprocessor: "
                    f"{e}"
                )
                return False, []

        # Persist metadata and structured results on the data_package
        if metadata_dict:
            self.data_package[PREPROC_META_KEY] = metadata_dict
        else:
            self.data_package[PREPROC_META_KEY] = {}
        if preproc_results:
            self.data_package[PREPROC_RESULTS_KEY] = preproc_results
        else:
            self.data_package[PREPROC_RESULTS_KEY] = []

        self.logger.debug(
            f"Persisted preprocessing artifacts: results={self.data_package}")

        return True, processed_files


class DataPackageImporter:
    """
    Handles the import of data packages into OMERO using database-driven order details.
    """

    def __init__(self, config, data_package, ttl_for_user_conn=6000000):
        self.config = config
        self.data_package = data_package
        self.ttl_for_user_conn = ttl_for_user_conn
        self.logger = logging.getLogger(__name__)
        self.logger.debug(
            f"Initializing DataPackageImporter for UUID: {data_package.get('UUID', 'Unknown')}")
        self.host = os.getenv('OMERO_HOST')
        self.password = os.getenv('OMERO_PASSWORD')
        self.user = os.getenv('OMERO_USER')
        self.port = os.getenv('OMERO_PORT')
        self.use_register_zarr = os.getenv('USE_REGISTER_ZARR', config.get('use_register_zarr', True))

        # Validate environment variables
        if not all([self.host, self.password, self.user, self.port]):
            self.logger.error(
                "OMERO connection details missing from environment variables.")
            raise ValueError("Missing OMERO connection environment variables.")

        self.imported = False

    @connection
    def import_to_omero(self, conn, file_path, target_id, target_type, uuid, transfer_type="ln_s", depth=None, log_id=None):
        log_id = log_id or uuid
        self.logger.debug(
            f"Starting import to OMERO for file: {file_path}, Target: {target_id} ({target_type})")
        cli = CLI()
        cli.register('import', ImportControl, '_')
        cli.register('sessions', SessionsControl, '_')
        arguments = [
            'import',
            '-k', conn.getSession().getUuid().val,
            '-s', conn.host,
            '-p', str(conn.port),
            f'--transfer={transfer_type}',
            '--no-upgrade',
            '--file', f"logs/cli.{log_id}.logs",
            '--errs', f"logs/cli.{log_id}.errs",
        ]
        if 'parallel_upload_per_worker' in self.config:
            arguments += ['--parallel-upload',
                          str(self.config['parallel_upload_per_worker'])]
        if 'parallel_filesets_per_worker' in self.config:
            arguments += ['--parallel-fileset',
                          str(self.config['parallel_filesets_per_worker'])]
        if self.config.get('skip_all', False):
            arguments += ['--skip', 'all']
        else:
            if self.config.get('skip_checksum', False):
                arguments += ['--skip', 'checksum']
            if self.config.get('skip_minmax', False):
                arguments += ['--skip', 'minmax']
            if self.config.get('skip_thumbnails', False):
                arguments += ['--skip', 'thumbnails']
            if self.config.get('skip_upgrade', False):
                arguments += ['--skip', 'upgrade']
        if depth:
            arguments += ['--depth', str(depth)]
        if target_type == 'Screen':
            arguments += ['-r', str(target_id)]
        elif target_type == 'Dataset':
            arguments += ['-d', str(target_id)]
        else:
            raise ValueError(
                "Invalid target_type. Must be 'Screen' or 'Dataset'.")
        arguments.append(str(file_path))
        cli.invoke(arguments)
        if cli.rv == 0:
            self.imported = True
            self.logger.info(f'Imported successfully for {str(file_path)}')
            return True
        else:
            self.imported = False
            self.logger.error(f'Import failed for {str(file_path)}')
            return False

    @connection
    def import_zarr(self, conn, uri, target, target_by_name=None, endpoint=None, nosignrequest=False):
        # Using https://github.com/BioNGFF/omero-import-utils/blob/main/metadata/register.py
        # Also using updates from https://github.com/ome/omero-cli-zarr/blob/master/src/omero_zarr/zarr_import.py
        from .register import (load_attrs, register_image, register_plate, link_to_target, validate_endpoint,
                               get_omexml_bytes, full_import, parse_image_metadata, set_rendering_settings,
                               set_external_info)
        import zarr
        from types import SimpleNamespace

        file_title = os.path.splitext(os.path.basename(uri))[0].rstrip('.ome')
        args = SimpleNamespace(uri=uri, endpoint=endpoint, name=file_title,
                               nosignrequest=nosignrequest, target=str(target), target_by_name=target_by_name)

        # --- start copy from register.main() ---

        validate_endpoint(endpoint)
        store = None
        if uri.startswith("/"):
            store = zarr.storage.LocalStore(uri, read_only=True)
        else:
            storage_options = {}
            if nosignrequest:
                storage_options['anon'] = True

            if endpoint:
                storage_options['client_kwargs'] = {'endpoint_url': endpoint}

            store = zarr.storage.FsspecStore.from_url(uri,
                                                      read_only=True,
                                                      storage_options=storage_options
                                                      )

        zattrs = load_attrs(store)
        objs = []
        if "plate" in zattrs:
            print("Registering: Plate")
            objs = [register_plate(conn, store, args, zattrs)]
        else:
            if "bioformats2raw.layout" in zattrs and zattrs["bioformats2raw.layout"] == 3:
                print("Registering: bioformats2raw.layout")
                zarr_name = args.uri.rstrip("/").split("/")[-1]
                if args.name:
                    zarr_name = args.name
                # try to load OME/METADATA.ome.xml
                omexml_bytes = get_omexml_bytes(store)
                if omexml_bytes is not None:
                    print("Importing OME/METADATA.ome.xml")
                    rsp = full_import(conn.c, omexml_bytes, args.wait)
                    for series, p in enumerate(rsp.pixels):
                        # set external info. NB: order of pixels MUST match the series 0, 1, 2...
                        image = conn.getObject("Image", p.image.id.val)
                        image_path = str(series)
                        image_attrs = load_attrs(store, image_path)
                        # pixels_type is only used if we have *incomplete* `omero` metadata
                        sizes, pixel_size, pixels_type = parse_image_metadata(store, image_attrs, image_path)
                        rnd_def = set_rendering_settings(conn, image, image_attrs, pixels_type)
                        if rnd_def is not None:
                            conn.getUpdateService().saveAndReturnObject(rnd_def)
                        set_external_info(image._obj, args, image_path=image_path)
                        # default name is METADATA.ome.xml [series], based on clientPath?
                        new_name = image.name.replace("METADATA.ome.xml", zarr_name)
                        print("Imported Image:", image.id, new_name)
                        image.setName(new_name)
                        image.save()  # save Name and ExternalInfo
                        objs.append(image)
                else:
                    print("OME/METADATA.ome.xml Not Found")
                    series = 0
                    series_exists = True
                    while series_exists:
                        try:
                            print("Checking for series:", series)
                            obj = register_image(conn, store, args, None, image_path=str(series))
                            objs.append(obj)
                        except FileNotFoundError:
                            series_exists = False
                        series += 1
            else:
                print("Registering: Image")
                objs = [register_image(conn, store, args, zattrs)]

        if args.target or args.target_by_name:
            for obj in objs:
                link_to_target(args, conn, obj)

        # --- end copy from register.main() ---

        image_ids = [obj.getId().getValue() for obj in objs]
        is_plate = "plate" in zattrs
        if image_ids:
            self.imported = True
            self.logger.info(f'Import successfully for {uri}')
        else:
            self.imported = False
            self.logger.error(f'Import failed for {uri}')
        return image_ids, is_plate

    @connection
    def get_plate_ids(self, conn, file_path, screen_id):
        if not self.imported:
            self.logger.error(f'File {file_path} was not imported')
            return None
        self.logger.debug("Retrieving Plate IDs")
        q = conn.getQueryService()
        params = Parameters()
        path_query = f"{str(file_path).strip('/')}%"
        self.logger.debug(f"path query: {path_query}. Screen_id: {screen_id}")
        params.map = {
            "cpath": rstring(path_query),
            "screen_id": rlong(screen_id),
        }
        results = q.projection(
            "SELECT DISTINCT p.id, p.details.creationEvent.time, fs.templatePrefix FROM Plate p "
            "JOIN p.wells w "
            "JOIN w.wellSamples ws "
            "JOIN ws.image i "
            "JOIN i.fileset fs "
            "JOIN fs.usedFiles u "
            "JOIN p.screenLinks spl "
            "WHERE u.clientPath LIKE :cpath AND spl.parent.id = :screen_id "
            "ORDER BY p.details.creationEvent.time DESC",
            params,
            conn.SERVICE_OPTS
        )
        self.logger.debug(f"Query results: {results}")
        plate_ids = [r[0].val for r in results]
        # Extract Template Prefixes
        template_prefixes = [r[2].val for r in results]
        self.logger.debug(f"Plate id determined to be {plate_ids} by SQL query")
        return plate_ids, template_prefixes

    @connection
    def get_image_paths(self, conn, file_path, dataset_id):
        if not self.imported:
            self.logger.error(f'File {file_path} was not imported')
            return None
        self.logger.debug("Retrieving Image paths from dataset")
        q = conn.getQueryService()
        params = Parameters()
        path_query = f"{str(file_path).strip('/')}%"
        params.map = {
            "cpath": rstring(path_query),
            "dataset_id": rlong(dataset_id),
        }
        results = q.projection(
            "SELECT DISTINCT fs.templatePrefix FROM Image i "
            "JOIN i.fileset fs "
            "JOIN fs.usedFiles u "
            "JOIN i.datasetLinks dl "
            "WHERE u.clientPath LIKE :cpath AND dl.parent.id = :dataset_id",
            params,
            conn.SERVICE_OPTS
        )
        template_prefixes = [r[0].val for r in results]
        return [], template_prefixes  # Return format consistent with get_plate_ids

    @connection
    def import_dataset(self, conn, target, dataset, transfer="ln_s", depth=None, file_index=None):
        kwargs = {"transfer": transfer}
        if 'parallel_upload_per_worker' in self.config:
            kwargs['parallel-upload'] = str(
                self.config['parallel_upload_per_worker'])
        if 'parallel_filesets_per_worker' in self.config:
            kwargs['parallel-fileset'] = str(
                self.config['parallel_filesets_per_worker'])
        if self.config.get('skip_all', False):
            kwargs['skip'] = 'all'
        if depth:
            kwargs['depth'] = str(depth)
        uuid = self.data_package.get('UUID')
        # Use per-file log IDs to avoid log collisions when importing multiple files
        log_id = f"{uuid}_{file_index}" if file_index is not None else uuid

        for attempt in range(1, IMPORT_MAX_RETRIES + 1):
            # Use attempt-specific log/err files so retries don't overwrite previous evidence
            attempt_log_id = f"{log_id}_attempt{attempt}" if attempt > 1 else log_id
            kwargs['file'] = f"logs/cli.{attempt_log_id}.logs"
            kwargs['errs'] = f"logs/cli.{attempt_log_id}.errs"

            self.logger.debug(f"EZImport (attempt {attempt}/{IMPORT_MAX_RETRIES}): {conn} {target} {int(dataset)} {kwargs}")
            result = ezomero.ezimport(conn=conn, target=target, dataset=int(dataset), **kwargs)

            # Check if import succeeded - ezimport returns None on failure, list (possibly empty) on success
            if result is not None:
                self.imported = True
                self.logger.info(f"Import succeeded, got image IDs: {result}")
                return result

            # Import failed — check if the error is transient/retryable
            errs_file = kwargs['errs']
            retryable, pattern = is_retryable_import_error(errs_file, self.logger)

            if retryable and attempt < IMPORT_MAX_RETRIES:
                delay = IMPORT_RETRY_DELAY * attempt  # increasing backoff
                self.logger.warning(
                    f"Import failed with retryable error ({pattern}). "
                    f"Retrying in {delay}s (attempt {attempt}/{IMPORT_MAX_RETRIES})..."
                )
                time.sleep(delay)
                continue
            else:
                if retryable:
                    self.logger.error(
                        f"Import failed with retryable error ({pattern}) but max retries "
                        f"({IMPORT_MAX_RETRIES}) exhausted."
                    )
                else:
                    self.logger.error(
                        f"Import failed - ezimport returned None (non-retryable error). "
                        f"Check {errs_file} for details."
                    )
                self.imported = False
                return result

    def upload_files(self, conn, file_paths, dataset_id=None, screen_id=None, local_paths=None):
        uuid = self.data_package.get('UUID')
        if dataset_id and screen_id:
            raise ValueError("Cannot specify both dataset_id and screen_id.")
        if not dataset_id and not screen_id:
            raise ValueError(
                "Either dataset_id or screen_id must be specified.")

        successful_uploads = []
        failed_uploads = []
        self.logger.debug(f"Uploading files: {file_paths}")
        upload_target = dataset_id or screen_id
        pre_processing = local_paths is not None

        for i, file_path in enumerate(file_paths):
            self.logger.debug(f"Uploading file: {file_path}")
            zarr_is_plate = False  # Track if ZARR import created a plate
            try:
                if pre_processing:  # pre-processing
                    local_path = local_paths[i]  # TODO: assumes 1:1 local_paths and file_paths
                    is_zarr = 'zar' in os.path.splitext(local_path)[1].lower()
                    if is_zarr and self.use_register_zarr:
                        result_entry = self.data_package[
                            PREPROC_RESULTS_KEY
                        ][i]
                        full_path = result_entry[PREPROC_RESULT_LOCAL_FULL]
                        self.logger.debug(f"Importing {full_path}")
                        image_ids, zarr_is_plate = self.import_zarr(
                            uri=str(full_path),
                            target=upload_target
                        )
                    else:
                        self.logger.debug(f"Importing {local_path}")
                        if screen_id:   # screen
                            # If local_paths, we have done preprocessing
                            # data is now in PROCESSED_DATA_FOLDER subfolder on remote storage
                            # and in local_paths folder on the omero server storage
                            # we will import now in-place from the omero server storage
                            # and then we'll switch the in-place symlinks to the remote storage (subfolder)
                            log_id = f"{uuid}_{i}"
                            imported = self.import_to_omero(
                                file_path=local_path,
                                target_id=screen_id,
                                target_type='Screen',
                                uuid=uuid,
                                depth=10,
                                log_id=log_id
                            )
                            self.logger.debug("Upload done. Retrieving plate id.")
                            image_ids, local_file_dir = self.get_plate_ids(
                                str(local_path), screen_id)

                        else:   # no screen
                            if os.path.isfile(local_path):
                                image_ids = self.import_dataset(
                                    target=local_path,
                                    dataset=dataset_id,
                                    transfer="ln_s",
                                    file_index=i
                                )
                                self.logger.debug(f"EZimport returned ids {image_ids} for {str(file_path)} ({dataset_id})")
                            else:
                                log_id = f"{uuid}_{i}"
                                imported = self.import_to_omero(
                                    file_path=local_path,
                                    target_id=dataset_id,
                                    target_type='Dataset',
                                    uuid=uuid,
                                    depth=10,
                                    log_id=log_id
                                )
                                image_ids = dataset_id

                            # Get the OMERO storage path for datasets
                            _, local_file_dir = self.get_image_paths(str(local_path), dataset_id)
                            
                            # Attempt to rename the imported Image
                            # to match the preprocessor-provided name (datasets only).
                            try:
                                # Only attempt when we have concrete Image IDs (typical for single-file imports)
                                if isinstance(image_ids, list) and image_ids:
                                    image_id_for_rename = max(image_ids)
                                    if self.rename_image_if_needed(
                                        conn, image_id_for_rename, local_path
                                    ):
                                        self.logger.info(
                                            (
                                                "Renamed Image "
                                                f"{image_id_for_rename} based on preprocessing 'name'."
                                            )
                                        )
                            except Exception as e:
                                self.logger.error(f"Post-symlink rename check failed: {e}")

                        # Rest of symlink logic...
                        # Ensure remote_path is the directory itself if file_path is a directory
                        result_entry = self.data_package[
                            PREPROC_RESULTS_KEY
                        ][i]
                        full_path = result_entry[PREPROC_RESULT_LOCAL_FULL]
                        remote_path = full_path if os.path.isdir(
                            full_path) else os.path.dirname(full_path)

                        local_file_dir = local_file_dir[0].rstrip("/") + "/"
                        local_file_dir = "/OMERO/ManagedRepository/" + local_file_dir
                       
                        # 2. Update the symlinks to point to the remote location
                        self.logger.info(
                            f"Now update symlinks in {local_file_dir} to {remote_path}")
                        for root, _, files in os.walk(local_file_dir):
                            for file in files:
                                symlink_path = os.path.join(root, file)
                                # Only process symlinks
                                if os.path.islink(symlink_path):
                                    # Update symlink to point to remote location
                                    # Remove the old symlink
                                    os.unlink(symlink_path)
                                    new_target = os.path.join(
                                        remote_path, file)
                                    # Create the new symlink
                                    os.symlink(new_target, symlink_path)
                                    self.logger.debug(
                                        f"new symlinks {symlink_path} -> {new_target}")
                        # Defer cleanup; handled after all files are processed

                else:   # no pre-processing
                    local_path = None
                    is_zarr = 'zar' in os.path.splitext(file_path)[1].lower()
                    if is_zarr and self.use_register_zarr:
                        image_ids, zarr_is_plate = self.import_zarr(
                            uri=str(file_path),
                            target=upload_target
                        )
                    else:
                        if screen_id:   # screen
                            log_id = f"{uuid}_{i}"
                            imported = self.import_to_omero(
                                file_path=str(file_path),
                                target_id=screen_id,
                                target_type='Screen',
                                uuid=uuid,
                                depth=10,
                                log_id=log_id
                            )
                            image_ids, _ = self.get_plate_ids(
                                str(file_path), screen_id)
                        else:   # no screen
                            if os.path.isfile(file_path):
                                image_ids = self.import_dataset(
                                    target=str(file_path),
                                    dataset=dataset_id,
                                    transfer="ln_s",
                                    file_index=i
                                )
                                self.logger.debug(f"EZimport returned ids {image_ids} for {str(file_path)} ({dataset_id})")
                            elif os.path.isdir(file_path):
                                log_id = f"{uuid}_{i}"
                                imported = self.import_to_omero(
                                    file_path=str(file_path),
                                    target_id=dataset_id,
                                    target_type='Dataset',
                                    uuid=uuid,
                                    depth=10,
                                    log_id=log_id
                                )
                                image_ids = dataset_id
                                self.logger.debug(f"Set ids {image_ids} to the dataset {dataset_id}")
                            else:
                                raise ValueError(
                                    f"{file_path} is not recognized as file or directory.")
                if screen_id:
                    upload_target = screen_id
                else:
                    upload_target = dataset_id

                if image_ids:
                    image_or_plate_id = max(image_ids) if isinstance(
                        image_ids, list) else image_ids
                    # Selecting 1 id, because this is a for-loop over files.
                    # This means we should only be getting back 1 ID per single upload.
                    self.logger.debug(f"Postprocessing ids {image_ids}: max ID = {image_or_plate_id}")
                    try:
                        # Determine if we're dealing with a plate:
                        # Either we imported to a screen, or ZARR created a plate
                        is_plate_object = bool(screen_id) or zarr_is_plate
                        self.add_image_annotations(
                            conn,
                            image_or_plate_id,
                            uuid,
                            file_path,
                            is_screen=is_plate_object,
                            local_path=local_path,
                        )

                        self.logger.info(
                            f"Uploaded file: {file_path} to target: {upload_target} with ID: {image_or_plate_id}")
                    except Exception as annotation_error:
                        self.logger.error(
                            f"Annotation failed for {file_path}: {annotation_error}")
                    successful_uploads.append(
                        (file_path, upload_target, os.path.basename(file_path), image_or_plate_id))
                else:
                    self.logger.error(
                        f"Upload rejected by OMERO for file {file_path}.")
                    failed_uploads.append(
                        (file_path, upload_target, os.path.basename(file_path), None))
            except Exception as e:
                self.logger.error(f"Error uploading file {file_path}: {e}")
                failed_uploads.append(
                    (file_path, upload_target, os.path.basename(file_path), None))
        return successful_uploads, failed_uploads

    @connection
    def rename_image_if_needed(self, conn, image_id, local_path):
        """Rename Image to the full name specified by the preprocessor container.

        The container has full control over the OMERO name, including extensions.
        Looks up PREPROC_RENAME_MAP_KEY by the processed local path used for import.
        Returns True if renamed, else False.
        """
        try:
            rename_map = self.data_package.get(
                PREPROC_RENAME_MAP_KEY, {}
            ) or {}
            target_name = str(
                rename_map.get(str(local_path), "")
            ).strip()
            if not target_name:
                return False  # No mismatch stored
            else:
                self.logger.debug(f"Found target name for Image {image_id}: {target_name}")
            img = conn.getObject("Image", image_id)
            if not img:
                return False
            else:
                self.logger.debug(f"Found Image object for ID {image_id}")
            i = img._obj
            i.setName(rstring(target_name))
            conn.getUpdateService().saveObject(i)
            self.logger.info(f"Renamed Image {image_id} to '{target_name}'")
            return True
        except Exception as e:
            self.logger.error(f"Rename failed for Image {image_id}: {e}")
            return False

    @connection
    def add_image_annotations(self, conn, object_id, uuid, file_path, is_screen=False, local_path=None):
        try:
            annotation_dict = {'UUID': str(uuid), 'Filepath': str(file_path)}
            
            # Add the full order_info for complete traceability
            order_info = self.data_package

            # Core order metadata
            core_fields = ['Group', 'Username', 'DestinationID', 'DestinationType', 'Files', 'FileNames']
            for field in core_fields:
                if field in order_info:
                    if field == 'Files':
                        # Convert file list to string representation
                        annotation_dict[field] = str(order_info[field])
                    elif field == 'FileNames':
                        # Convert filename list to string representation  
                        annotation_dict[field] = str(order_info[field])
                    else:
                        annotation_dict[field] = str(order_info[field])

            local_path = order_info.get(PREPROC_RESULTS_KEY, [{}])[0].get(PREPROC_RESULT_LOCAL_FULL)
            if local_path:
                annotation_dict['Imported_from'] = str(local_path)

            # Add preprocessing metadata if available
            preprocessing_fields = [key for key in order_info.keys() if key.startswith('preprocessing_')]
            for field in preprocessing_fields:
                annotation_dict[field] = str(order_info[field])

            # Add preprocessing ID if available
            if '_preprocessing_id' in order_info:
                annotation_dict['preprocessing_id'] = str(order_info['_preprocessing_id'])
            
            # Add any extra_params from preprocessing
            if 'extra_params' in order_info:
                for key, value in order_info['extra_params'].items():
                    annotation_dict[f'preprocessing_{key}'] = str(value)
            
            # Add preprocessing metadata from processing results
            preprocessing_metadata = order_info.get(PREPROC_META_KEY, {})
            for processed_file_path, metadata in preprocessing_metadata.items():
                if processed_file_path == file_path or processed_file_path == local_path:
                    self.logger.debug(f"Found preprocessing metadata for file: {processed_file_path}")
                    # Prefix preprocessing output metadata to avoid conflicts
                    for key, value in metadata.items():
                        annotation_dict[f'processing_output_{key}'] = str(value)
                    self.logger.debug(f"Added preprocessing output metadata: {metadata}")
            
            # Add timestamp for when annotation was created
            import datetime
            annotation_dict['Import_Timestamp'] = datetime.datetime.now().isoformat()

            # Get configurable annotation namespace with fallback to default
            ns = self.config.get('annotation_namespace', 'biomero.import')
            object_type = "Plate" if is_screen else "Image"
            
            # CSV metadata reading logic ...
            metadata_file = self.data_package.get('metadata_file', 'metadata.csv')
            metadata_unproc_path = os.path.join(os.path.dirname(file_path), metadata_file)
            metadata_processed_path = os.path.join(os.path.dirname(file_path), PROCESSED_DATA_FOLDER, metadata_file)
            
            for metadata_path in [metadata_unproc_path, metadata_processed_path]:
                if os.path.exists(metadata_path):
                    self.logger.info(f"Reading metadata from {metadata_path}")
                    with open(metadata_path, 'r', newline='') as csvfile:
                        reader = csv.reader(csvfile)
                        for row in reader:
                            if len(row) == 2:
                                key, value = row
                                if key:
                                    # Prefix CSV metadata to distinguish from order metadata
                                    annotation_dict[f'csv_{key}'] = value or ''
                            else:
                                self.logger.warning(f"Invalid metadata row: {row}")
                else:
                    self.logger.info(f"No metadata found at {metadata_path}")
            
            self.logger.debug(f"Full annotation dict: {annotation_dict}")
            self.logger.info(f"Adding {len(annotation_dict)} metadata fields to {object_type} {object_id}")
            
            map_ann_id = ezomero.post_map_annotation(
                conn=conn,
                object_type=object_type,
                object_id=object_id,
                kv_dict=annotation_dict,
                ns=ns,
                across_groups=False
            )
            if map_ann_id:
                self.logger.info(
                    f"Annotations added to {object_type} ID: {object_id}. MapAnnotation ID: {map_ann_id}")
            else:
                self.logger.warning(
                    f"Annotations created for {object_type} ID: {object_id}, but no ID returned.")
        except Exception as e:
            self.logger.error(
                f"Failed to add annotations to {object_type} ID: {object_id}: {e}")

    @connection
    def create_new_dataset(self, conn, name="New Dataset", description=""):
        dataset = DatasetI()
        dataset.setName(rstring(name))
        dataset.setDescription(rstring(description))
        dataset = conn.getUpdateService().saveAndReturnObject(dataset)
        dataset_id = dataset.getId().getValue()
        self.logger.info(f"Created new dataset with ID: {dataset_id}")
        return dataset_id

    def import_data_package(self):
        """Import the data package and log the outcome."""
        try:
            # Get DestinationID from DataPackage
            target_id = self.data_package.get('DestinationID')
            if not target_id:
                self.logger.error("No DestinationID provided")
                return [], [], True

            target_type = self.data_package.get('DestinationType')
            if not target_type:
                self.logger.error("No DestinationType provided")
                return [], [], True
            # Determine if the target ID is a Dataset or a Screen
            is_screen = target_type == "Screen"
            self.logger.info(
                f"Target ID {target_id} ({type(target_id)}) identified as {target_type}.")

            intended_username = self.data_package.get('Username')
            group_name = self.data_package.get('Group')

            if not (intended_username and group_name):
                self.logger.error(
                    "Missing required user or group information in data package.")
                return [], [], True
            # TODO: can we use this decorator instead? no?
            retry_count = 0
            while retry_count < MAX_RETRIES:
                try:
                    with BlitzGateway(self.user, self.password, host=self.host, port=self.port, secure=True) as root_conn:
                        if not root_conn.connect():
                            self.logger.error(
                                "Failed to connect to OMERO as root.")
                            return [], [], True
                        self.logger.info("Connected to OMERO as root.")
                        root_conn.keepAlive()
                        group_id = ezomero.get_group_id(
                            root_conn, group_name)  # grab w/ ezomero
                        with root_conn.suConn(intended_username, ttl=self.ttl_for_user_conn) as user_conn:
                            if not user_conn:
                                self.logger.error(
                                    f"Failed to connect as user {intended_username}")
                                return [], [], True
                            user_conn.keepAlive()
                            user_conn.setGroupForSession(group_id)
                            self.logger.info(
                                f"Connected as user {intended_username} in group {group_name}")

                            all_successful_uploads = []
                            all_failed_uploads = []
                            file_paths = self.data_package.get('Files', [])
                            self.logger.debug(f"Files to upload: {file_paths}")

                            processor = DataProcessor(
                                self.data_package, self.logger)
                            if processor.has_preprocessing():
                                # Setup a local tmp folder on the OMERO server itself
                                local_tmp_folder = get_tmp_output_path(
                                    self.data_package)
                                os.makedirs(local_tmp_folder, exist_ok=True)
                                log_ingestion_step(
                                    self.data_package, STAGE_PREPROCESSING)
                                success, processed_files = processor.run(dry_run=False)
                                if not success:
                                    msg = "Preprocessing failed. See container logs for details."
                                    self.logger.error(msg)
                                    # propagate reason for DB description
                                    self.data_package['Description'] = msg
                                    return [], [], True
                                self.logger.info(
                                    "Preprocessing succeeded; proceeding with import.")
                                
                                # Determine local_paths based on whether we got specific files or not
                                if processed_files:
                                    # New JSON-based approach: use specific processed file paths
                                    local_paths = processed_files
                                    self.logger.debug(f"Using JSON-parsed file paths: {local_paths}")
                                else:
                                    # Legacy approach: use temp folder
                                    local_paths = (
                                        [local_tmp_folder]
                                        if processor.has_preprocessing()
                                        else None
                                    )
                                    self.logger.debug(
                                        f"Using legacy folder approach: {local_paths}"
                                    )

                                # Results already persisted by processor.run() under PREPROC_* keys

                                # Pass the target id based on its type; include local paths if preprocessed
                                if is_screen:
                                    successful_uploads, failed_uploads = self.upload_files(
                                        user_conn, file_paths, dataset_id=None, screen_id=target_id, local_paths=local_paths
                                    )
                                else:
                                    successful_uploads, failed_uploads = self.upload_files(
                                        user_conn, file_paths, dataset_id=target_id, screen_id=None, local_paths=local_paths
                                    )
                            else:
                                self.logger.info(
                                    "No preprocessing required; continuing upload.")
                                if is_screen:
                                    successful_uploads, failed_uploads = self.upload_files(
                                        user_conn,
                                        file_paths,
                                        dataset_id=None,
                                        screen_id=target_id
                                    )
                                else:
                                    successful_uploads, failed_uploads = self.upload_files(
                                        user_conn,
                                        file_paths,
                                        dataset_id=target_id,
                                        screen_id=None
                                    )
                            all_successful_uploads.extend(successful_uploads)
                            all_failed_uploads.extend(failed_uploads)

                            # Final cleanup: remove temp output after all files
                            try:
                                tmp_dir = get_tmp_output_path(
                                    self.data_package
                                )
                                if os.path.exists(tmp_dir):
                                    self.logger.debug(
                                        f"Removing temp folder: {tmp_dir}"
                                    )
                                    shutil.rmtree(tmp_dir)
                            except Exception as cleanup_err:
                                self.logger.warning(
                                    f"Temp cleanup failed: {cleanup_err}"
                                )

                            return (
                                all_successful_uploads,
                                all_failed_uploads,
                                False,
                            )

                except TypeError as te:
                    # Handle suConn returning None
                    if (
                        "context manager" in str(te)
                        or "NoneType" in str(te)
                    ):
                        full_msg = (
                            f"suConn failed for user '{intended_username}' "
                            f"in group '{group_name}'. User may not exist or "
                            f"cannot be impersonated."
                        )
                        self.logger.error(f"{full_msg} Error: {te}",
                                          exc_info=True)
                        
                        # Simplified message for data_package description
                        simple_msg = (
                            f"User '{intended_username}' or group "
                            f"'{group_name}' not recognized. Contact admin."
                        )
                        if not self.data_package.get('Description'):
                            self.data_package['Description'] = simple_msg
                        return [], [], True
                    # Not our case, let the generic handler manage it
                    raise

                except Exception as e:
                    # TODO: can we use this decorator instead? no?
                    if "connect" in str(e).lower():
                        retry_count += 1
                        self.logger.error(
                            f"Connection issue (attempt {retry_count}/"
                            f"{MAX_RETRIES}): {e}")
                        if retry_count < MAX_RETRIES:
                            self.logger.info(
                                f"Retrying in {RETRY_DELAY} seconds...")
                            time.sleep(RETRY_DELAY)
                            continue
                    msg = f"Error during import: {e}"
                    self.logger.error(msg, exc_info=True)
                    # Simplified message for DB
                    self.data_package['Description'] = (
                        "Import failed. Please retry; if it repeats, "
                        "contact admin."
                    )
                    return [], [], True

            self.logger.error(
                f"Max retries ({MAX_RETRIES}) reached during import")
            if not self.data_package.get('Description'):
                self.data_package['Description'] = (
                    "OMERO connection problem. Retry later; if it repeats, "
                    "contact admin."
                )
            return [], [], True

        except Exception as e:
            self.logger.error(
                f"Error during import_data_package: {e}", exc_info=True)
            return [], [], True
