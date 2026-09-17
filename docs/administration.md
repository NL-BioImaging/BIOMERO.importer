# Administration

## Configuration

Configure the system using `config/settings.yml`:

```yaml
# Database connection (can also be set via INGEST_TRACKING_DB_URL environment variable)
ingest_tracking_db: "postgresql://user:password@host:port/database"

# OMERO connection (set via environment variables)
# OMERO_HOST, OMERO_USER, OMERO_PASSWORD, OMERO_PORT

# File system paths (legacy - only base_dir is used in current implementation)
base_dir: /data

# Processing settings
max_workers: 4
log_level: DEBUG
log_file_path: logs/app.logs

# Import optimization
parallel_upload_per_worker: 2
parallel_filesets_per_worker: 2
skip_checksum: false
skip_minmax: false
skip_thumbnails: false
skip_upgrade: false
skip_all: false

use_register_zarr: true

# Annotation namespace for OMERO metadata (default: "biomero.import")
# Can be customized to maintain compatibility with existing systems
annotation_namespace: "biomero.import"
```

**Note**: The `upload_orders_dir_name`, `data_dir_name`, and `failed_uploads_directory_name` settings are **legacy from the old file-based system** and are no longer used in the current database-driven implementation.

## Environment Variables

The system uses these environment variables:

- `INGEST_TRACKING_DB_URL`: Database connection string (overrides config file setting)
- `OMERO_HOST`: OMERO server hostname
- `OMERO_USER`: OMERO root user
- `OMERO_PASSWORD`: OMERO root password
- `OMERO_PORT`: OMERO server port
- `PODMAN_USERNS_MODE`: Set to "keep-id" for Linux user namespace mapping in preprocessing
- `PROCESSED_DATA_FOLDER`: Subfolder name for preprocessing outputs and canonical
  Zarr storage (default: `.processed` when the variable is unset). Read when the
  importer starts. The value is used as supplied: no leading dot is added or
  removed. Use a non-empty relative subfolder name.
- `USE_REGISTER_ZARR`: Set to "true" to enable zarr register script - requires omero-zarr-pixel-buffer (overrides config file setting)
- `BIOMERO_SHALLOW_ZARR`: Opt in to the native `biomero.shallow-zarr`
  lifecycle operation. Existing orders are unchanged when false or absent.
  Enabling it requires installing the importer with its identity extra:
  `pip install "biomero-importer[identity]"`. The NL-BIOMERO importer image
  includes this extra. If the flag is enabled without ISCC-BIO,
  `get_importer_capabilities()` omits the lifecycle operation, reports the
  missing dependency, and rejects shallow import orders with an actionable
  configuration error; ordinary import orders remain available.
- `BIOMERO_SHALLOW_ZARR_WORKERS`: Bounded ISCC-BIO identity workers used by
  the importer service (library fallback `1`; NL-BIOMERO supplies `4`). This is
  deployment configuration, not a client-controlled import option.

### Configuring the processed data folder

`PROCESSED_DATA_FOLDER=processed` writes to a subfolder named `processed`;
`PROCESSED_DATA_FOLDER=.import` writes to `.import`. Leaving the variable unset
keeps the existing `.processed` default.

> [!WARNING]
> **Choose this setting before the first import whenever possible. Changing it
> on an existing deployment requires planning; there is no built-in migration.**
>
> Changing from `.processed` to `.import` leaves existing data in `.processed`
> and directs new processed outputs to `.import`, so both folders can coexist.
> The importer does not move existing data, retarget existing filesystem
> symlinks, rewrite Zarr `ExternalInfo.lsid` values, or migrate canonical-source
> records. Changing this variable alone does not redirect existing pixel reads.
> **Keep the old data accessible at its original paths from OMERO.server.**
> Renaming, moving, or deleting the old folder can break both symlink-based
> imports and registered Zarr images, including their Plate images.
>
> Resubmitting an order with preprocessing runs the converter with the current
> folder setting. CSV annotation lookup checks the input directory and the
> currently configured processed subfolder; it does not search the previous
> processed subfolder. Let active imports finish before changing the setting,
> retain the old folder and its mounts, and plan any data migration separately.

The storage references explain why existing reads retain their paths:

| Import route | Stored reference used for pixel access |
| --- | --- |
| Conventional in-place import | A filesystem symlink in OMERO's ManagedRepository. For preprocessed files, `upload_files()` retargets it to the converter's reported `full_path`, mapped to shared storage. |
| Zarr registration (`USE_REGISTER_ZARR=true`) | `set_external_info()` records the Zarr path in each Image's `ExternalInfo.lsid`, appending the image node for series and Plates. The Zarr pixel buffer reads that stored path. This route does not rely on the ManagedRepository symlink redirection. |
| Shallow Zarr registration | The importer resolves the stored canonical source or label location before registration, then records that physical path in `ExternalInfo.lsid`. Existing canonical `relativePath` values are resolved against their storage root, without substituting the current processed-folder setting. |

See [`upload_files()`](https://github.com/NL-BioImaging/BIOMERO.importer/blob/main/biomero_importer/utils/importer.py),
[`set_external_info()`](https://github.com/NL-BioImaging/BIOMERO.importer/blob/main/biomero_importer/utils/register.py), and
[`resolve_managed_source_path()`](https://github.com/NL-BioImaging/BIOMERO.importer/blob/main/biomero_importer/utils/result_zarr.py).
In the pixel buffer, `ZarrPixelsService.getUri()` reads `ExternalInfo.lsid`,
`asPath()` converts a local value with `Paths.get()`, and
`createOmeNgffPixelBuffer()` opens that location. It does not read
`PROCESSED_DATA_FOLDER` or search for a renamed folder. See the upstream
[0.6.1 implementation](https://github.com/glencoesoftware/omero-zarr-pixel-buffer/blob/v0.6.1/src/main/java/com/glencoesoftware/omero/zarr/ZarrPixelsService.java)
and [path contract](https://github.com/glencoesoftware/omero-zarr-pixel-buffer/blob/v0.6.1/README.md#usage).

The setting is local to each Python process importing the library. Setting it
on the importer container configures that service's preprocessing. Code calling
`CanonicalStore.relative_path_for()` uses the setting in its own process;
setting it only on the importer does not configure separate OMERO script
workers. If those workers must create canonical Zarrs in the same custom folder,
their deployment must also pass the variable through to the script subprocesses.

For example, set a different processed subfolder in the importer container's
Docker Compose environment, then recreate the service:

```yaml
services:
  biomero-importer:
    environment:
      PROCESSED_DATA_FOLDER: processed
```

```bash
docker compose up -d --force-recreate biomero-importer
```

## Running the System

The BIOMERO.importer system is designed to run as a containerized service within the BIOMERO 2.0 ecosystem:

```bash
# Start the service (typically via docker compose)
docker compose up biomero-importer

# Check logs
docker compose logs -f biomero-importer
```

## Monitoring and Debugging

### Log Files

The system generates several log files in `/auto-importer/logs/`:

- `app.logs`: Main application logs with all system activity
- `cli.<UUID>.logs`: OMERO CLI import logs for each upload order
- `cli.<UUID>.errs`: OMERO CLI error logs for each upload order

### Database Queries

Check system status with direct database queries:

```sql
-- View recent orders
SELECT uuid, stage, group_name, user_name, timestamp
FROM imports
ORDER BY timestamp DESC LIMIT 10;

-- Check pending orders
SELECT * FROM imports
WHERE stage = 'Import Pending';

-- View preprocessing jobs
SELECT it.uuid, p.container, p.extra_params
FROM imports it
JOIN imports_preprocessing p ON it.preprocessing_id = p.id
WHERE it.stage = 'Import Started';
```

### Testing the System

Use the system check script to verify setup:

```bash
# Inside the container
python tests/system_check.py
```

This creates a test upload order and verifies the complete ingestion pipeline.

## Error Handling

The system includes comprehensive error handling:

- **Dangling Orders**: Automatically marks stale orders as failed on startup
- **Retry Logic**: Database operations include retry mechanisms
- **Detailed Logging**: All operations are logged with appropriate detail levels
- **Graceful Shutdown**: Proper cleanup of resources and connections
