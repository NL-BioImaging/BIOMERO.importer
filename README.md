# BIOMERO.importer - Automated Data Import System

> 🚀 **This package is part of BIOMERO 2.0** — For complete deployment and FAIR infrastructure setup, start with the [**NL-BIOMERO Documentation**](https://nl-bioimaging.github.io/NL-BIOMERO/) 📖

The BIOMERO.importer system enables automated uploading of image data from microscope workstations to an OMERO server. BIOMERO.importer is a database-driven system that polls a PostgreSQL database for new import orders and processes them automatically, including the option of running preprocessing containers for e.g. file conversion or pyramid creation.

## System Overview

The BIOMERO.importer system consists of:

1. **Database-driven order management**: Upload orders are stored in a PostgreSQL database with full tracking and preprocessing support
2. **Automated polling**: The system continuously polls the database for new orders to process
3. **Ingestion pipeline**: Handles file validation, optional preprocessing, and OMERO import with comprehensive logging
4. **Event sourcing**: All import steps are tracked in the database for full auditability

## Architecture

The system uses SQLAlchemy models to manage:

- **Upload Orders**: Stored in `imports` table with stages from "Import Pending" to "Import Completed"
- **Preprocessing**: Optional containerized preprocessing steps stored in `imports_preprocessing` table
- **Progress Tracking**: Complete audit trail of all import operations

![Flow Diagram of BIOMERO.importer process](flow_diagram_ADI_import.png)

### Key Components

- **DatabasePoller**: Continuously polls for new orders with `STAGE_NEW_ORDER` status
- **UploadOrderManager**: Validates and processes order data from database records
- **DataPackageImporter**: Handles the actual OMERO import process with optional preprocessing
- **IngestTracker**: Manages database logging and progress tracking

## Database Schema

The system uses two main tables:

### `imports` (IngestionTracking)
- Stores all import orders and their progress
- Tracks stages: "Import Pending" → "Import Started" → "Import Completed"/"Import Failed"
- Includes full metadata: user, group, destination, files, timestamps

### `imports_preprocessing` 
- Stores preprocessing configuration for containerized workflows
- Links to imports records via foreign key
- Supports dynamic parameters via JSON field

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
- `USE_REGISTER_ZARR`: Set to "true" to enable zarr register script - requires omero-zarr-pixel-buffer (overrides config file setting)

## Creating Upload Orders

Upload orders are typically created through a user interface, such as the OMERO.biomero plugin (Importer tab) at `/omero_biomero/biomero/`, an OMERO.web extension. However, orders can also be created programmatically using the database API. 

You can use the provided test scripts shown below as examples. 
You can also configure some more settings for them: 
```yaml
# Preprocessing settings
preprocessing: true  # Enable containerized preprocessing
sample_image: /auto-importer/tests/Barbie.tif
sample_group: "Demo"
sample_user: "researcher"
sample_parent_id: "151"
sample_parent_type: "Dataset"  # or "Screen"
```

### Using the System Check Script

```bash
# Inside the container
python tests/system_check.py
```

This script creates a test upload order and verifies the complete ingestion pipeline.

### Using the Test Main Script

```bash
# Inside the container  
python tests/t_main.py
```

This creates upload orders for multiple groups based on your configuration.

### Manual Database Insertion

```python
from biomero_importer.utils.ingest_tracker import IngestionTracking, Preprocessing, STAGE_NEW_ORDER
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

# Create database connection
engine = create_engine("postgresql://user:password@host:port/database")
Session = sessionmaker(bind=engine)
session = Session()

# Create basic upload order
order = IngestionTracking(
    group_name="Demo",
    user_name="researcher",
    destination_id="151",
    destination_type="Dataset", 
    stage=STAGE_NEW_ORDER,
    uuid=str(uuid.uuid4()),
    files=["/data/group/image1.tif", "/data/group/image2.tif"]
)

# Optional: Add preprocessing
preprocessing = Preprocessing(
    container="cellularimagingcf/converter:latest",
    input_file="{Files}",
    output_folder="/data",
    alt_output_folder="/out",
    extra_params={"saveoption": "single"}
)
order.preprocessing = preprocessing

session.add(order)
session.commit()
session.close()
```

## Preprocessing Support

The system supports containerized preprocessing workflows using **Podman-in-Docker/Podman**:

### Container Requirements

Preprocessing containers should follow these conventions:

1. **Input Parameters**: Accept `--inputfile` and `--outputfolder` parameters
2. **File Processing**: Process the input file and generate outputs in the specified folder
3. **JSON Output**: Optionally output structured JSON on the last line for file tracking
4. **Metadata Support**: Include keyvalue pairs for annotation metadata

### Example Container Structure

See [ConvertLeica-Docker](https://github.com/Cellular-Imaging-Amsterdam-UMC/ConvertLeica-Docker) for a complete example.

```dockerfile
FROM python:3.9-slim

# Install your processing tools
RUN pip install your-processing-library

# Copy your processing script
COPY convert_script.py /app/
WORKDIR /app

# Entry point that accepts standard parameters
ENTRYPOINT ["python", "convert_script.py"]
```

Note: We suggest to keep the `user` in the Dockerfile as `ROOT` because non-root users might get into permission issues with the mounted I/O folders, especially on Windows. On Linux, we have the env option with `PODMAN_USERNS_MODE: keep-id` so that we can run also as non-root, but this doesn't work on Docker for Windows.

See the [security overview](./security_overview_privileged.md) for more details on the podman-in-podman or podman-in-docker setups, requirements, and issues.

### Podman Configuration

The system runs containers using Podman with these settings:

```yaml
# In docker-compose.yml
biomero-importer:
  privileged: true
  devices:
    - "/dev/fuse:/dev/fuse"
  security_opt:
    - "label=disable"
  environment:
    PODMAN_USERNS_MODE: keep-id  # For Linux user namespace mapping
```

### Preprocessing Parameters

Configure preprocessing in your database order:

```python
preprocessing = Preprocessing(
    container="cellularimagingcf/converter:latest",
    input_file="{Files}",  # Replaced by BIOMERO.importer with actual file path
    output_folder="/data",  # Mount point in container
    alt_output_folder="/out",  # Alternative output location
    extra_params={
        "saveoption": "single",
        "format": "tiff",
        "compression": "lzw"
    }
)
```

### JSON Output Format

For advanced file tracking, containers can output JSON on the last line:

```json
[
  {
    "name": "Image Name",   
    "full_path": "File Path relative to the docker data volume (i.e. inputfile path)",
    "alt_path": "/out/processed_image.tif",
    "keyvalues": [
      {"processing_method": "conversion"},
      {"original_format": "lsm"},
      {"compression": "lzw"}
    ]
  }
]
```

## Running the System

The BIOMERO.importer system is designed to run as a containerized service within the BIOMERO 2.0 ecosystem:

```bash
# Start the service (typically via docker-compose)
docker-compose up biomero-importer

# Check logs
docker-compose logs -f biomero-importer
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

## Integration with BIOMERO

The BIOMERO.importer system is designed to work seamlessly with the BIOMERO 2.0 environment:

- Shares the same PostgreSQL database (BIOMERO.db) for order coordination with BIOMERO.analyzer
- Integrates with BIOMERO's OMERO.biomero web plugin for a unified interface
- Provides audit trails for FAIR provenance

## Future Development

The current implementation is focused on:

1. **Enhanced Preprocessing**: Expanding containerized workflow support
2. **Performance Optimization**: Improved database polling and processing efficiency  
3. **Advanced Monitoring**: Better observability and alerting capabilities
4. **Multi-tenant Support**: Enhanced isolation and resource management

---

**Note**: This system replaces the previous file-based upload order approach. All order management is now database-driven using PostgreSQL (BIOMERO.db) and SQLAlchemy for improved reliability, scalability, and integration with BIOMERO.

## Data Access Architecture

The BIOMERO.importer system requires a shared storage architecture where data is accessible from multiple containers with read/write permissions. This is essential for in-place imports and preprocessing workflows.

### Storage Requirements

The system requires a **shared storage volume** (typically a Samba/CIFS mount or NFS) that is mounted identically across all containers:

- **OMERO Server**: For in-place imports using `ln_s` transfers
- **OMERO Web**: For OMERO.biomero plugin to browse and select files
- **BIOMERO.importer**: For reading source files and writing processed data
- **OMERO Workers**: For script access to data files

**Critical requirement**: All mounts must have **read/write (R/W) permissions**, not read-only.

### Mount Configuration

```yaml
# Example docker-compose.yml mounts
services:
  omeroserver:
    volumes:
      - "omero:/OMERO"
      - "./web/L-Drive:/data"  # Shared storage mounted as /data
      
  omeroweb:
    volumes:
      - "./web/L-Drive:/data:rw"  # Same mount path, R/W access
      
  biomero-importer:
    volumes:
      - "omero:/OMERO"
      - "./web/L-Drive:/data"  # Identical mount path for in-place imports
```

### In-Place Import Workflow

The BIOMERO.importer system uses **in-place imports** exclusively, which means:

1. **Source Data**: Files remain on the shared storage
2. **OMERO Import**: Uses `transfer=ln_s` to create symlinks instead of copying data
3. **No Data Duplication**: Original files stay in place, only metadata is stored in OMERO
4. **Preprocessing**: Creates new files but maintains in-place import approach

### Preprocessing Data Flow

When preprocessing is enabled, the system follows this data flow:

```
Original Data (Remote Storage)
    ↓
Container Processing (On OMERO Server)
    ↓
Processed Data → Two Destinations:
    1. Remote Storage (/.processed subfolder)
    2. Temporary Local Storage (alt_path)
    ↓
OMERO Import (from temporary storage)
    ↓
Symlink Redirect (to remote storage)
    ↓
Cleanup (temporary storage deleted)
```

#### Why This Architecture?

1. **Performance**: Import from local temporary storage is faster than remote storage
2. **Reliability**: Avoid network issues during import process
3. **Storage Efficiency**: Final data resides on remote storage, not OMERO server
4. **Backup**: Processed data is preserved on remote storage

#### Implementation Details

From the source code (`importer.py`):

```python
# Preprocessing creates data in both locations
remote_path = os.path.join(file_path, PROCESSED_DATA_FOLDER)  # /.processed
alt_path = f"/OMERO/OMERO_inplace/{uuid}"  # Temporary local storage

# Import from temporary storage for speed
imported = self.import_to_omero(
    file_path=alt_path,
    target_id=dataset_id,
    target_type='Dataset',
    transfer="ln_s"
)

# After import, redirect symlinks to remote storage
for symlink_path in omero_managed_files:
    os.unlink(symlink_path)  # Remove temporary symlink
    new_target = os.path.join(remote_path, filename)
    os.symlink(new_target, symlink_path)  # Point to remote storage
```

### Metadata Integration

The system supports metadata inclusion through two mechanisms:

#### 1. CSV Metadata Files

Place a `metadata.csv` file alongside your import data:

```csv
key,value
acquisition_date,2024-01-15
magnification,63x
staining_method,DAPI
```

The system automatically detects and processes CSV files in:
- Original data directory
- Processed data directory (`.processed` subfolder)

#### 2. JSON Metadata from Preprocessing

Preprocessing containers can output metadata in their JSON response:

```json
[
  {
    "alt_path": "/out/processed_image.tif",
    "keyvalues": [
      {"processing_method": "deconvolution"},
      {"algorithm": "Richardson-Lucy"},
      {"iterations": "10"}
    ]
  }
]
```

This allows containers to:
- **Enrich metadata** by calling external APIs
- **Add processing parameters** automatically
- **Create metadata-only containers** that don't modify source data

### Example Deployment

#### Docker Compose Setup

```yaml
volumes:
  - "/mnt/shared-storage:/data"  # Shared storage mount
  - "omero:/OMERO"               # OMERO managed repository
```

#### Podman Setup (Linux)

```bash
podman run -d --rm --name biomero-importer \
    --privileged \
    --device /dev/fuse \
    --security-opt label=disable \
    -e OMERO_HOST=omeroserver \
    -e OMERO_USER=root \
    -e OMERO_PASSWORD=secret \
    -e OMERO_PORT=4064 \
    -e PODMAN_USERNS_MODE=keep-id \
    --network omero \
    --volume /mnt/datadisk/omero:/OMERO \
    --volume /mnt/L-Drive/basic/divg:/data \
    --volume "$(pwd)/logs/biomero-importer:/auto-importer/logs:Z" \
    --volume "$(pwd)/config:/auto-importer/config" \
    --userns=keep-id:uid=1000,gid=1000 \
    cellularimagingcf/biomero-importer:latest
```

### Storage Permissions

Ensure proper permissions on your shared storage.

Basic examples:

```bash
# Example for Linux hosts
sudo chmod -R 755 /mnt/shared-storage
sudo chown -R 1000:1000 /mnt/shared-storage

# For Samba/CIFS mounts, ensure the mount options allow R/W:
mount -t cifs //server/share /mnt/shared-storage -o username=user,rw,file_mode=0755,dir_mode=0755
```


### Troubleshooting Storage Issues

Common storage-related problems:

1. **Permission Denied**: Check R/W permissions on shared storage
2. **Import Failures**: Verify identical mount paths across all containers
3. **Symlink Errors**: Ensure OMERO managed repository is accessible
4. **Preprocessing Failures**: Check temporary storage space and permissions

Use these commands to diagnose:

```bash
# Check mount points
docker exec biomero-importer df -h

# Test file access
docker exec biomero-importer ls -la /data
docker exec biomero-importer touch /data/test-write-permissions

# Verify OMERO storage
docker exec biomero-importer ls -la /OMERO/ManagedRepository
```

This architecture ensures efficient, reliable data import while maintaining data integrity and providing flexibility for preprocessing workflows.

## BIOMERO 2.0 Integration

BIOMERO.importer is a core component of the BIOMERO 2.0 ecosystem, working alongside:

- **BIOMERO.analyzer**: For HPC-based image analysis workflows
- **BIOMERO.scripts**: For OMERO script-based workflow execution  
- **BIOMERO.db**: Shared PostgreSQL database for workflow coordination
- **OMERO.biomero**: Modern web interface for data import and analysis

Together, these components provide a comprehensive FAIR imaging platform for automated data management and analysis.

## Developer guide: schema changes and migrations

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



