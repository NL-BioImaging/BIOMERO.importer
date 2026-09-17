# Storage and preprocessing

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

See the [security overview](security.md) for more details on the podman-in-podman or podman-in-docker setups, requirements, and issues.

### Docker demo configuration

The NL-BIOMERO Docker demo may use the following settings for nested Podman.
For Linux rootless Podman without privileged mode, see [container security](security.md).

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

Image data remains on shared storage. Conventional imports use `transfer=ln_s`
to create filesystem symlinks. Zarr registration with `USE_REGISTER_ZARR=true`
instead records the physical Zarr location in `ExternalInfo.lsid` for the Zarr
pixel buffer. Preprocessing creates new outputs on shared storage. See
[Configuring the processed data folder](administration.md#configuring-the-processed-data-folder)
for the references that must remain valid when changing storage configuration.

### Preprocessing Data Flow

For conventional imports with preprocessing, the system follows this data flow:

```
Original Data (Remote Storage)
    ↓
Container Processing (On OMERO Server)
    ↓
Processed Data → Two Destinations:
    1. Remote Storage (PROCESSED_DATA_FOLDER subfolder; default .processed)
    2. Temporary Local Storage (alt_path)
    ↓
OMERO Import (from temporary storage)
    ↓
Symlink Redirect (to remote storage)
    ↓
Cleanup (temporary storage deleted)
```

For Zarr registration, `upload_files()` uses the converter's mapped `full_path`
on shared storage directly; it bypasses the temporary-storage import and
ManagedRepository symlink redirection shown above.

#### Why This Architecture?

1. **Performance**: Import from local temporary storage is faster than remote storage
2. **Reliability**: Avoid network issues during import process
3. **Storage Efficiency**: Final data resides on remote storage, not OMERO server
4. **Backup**: Processed data is preserved on remote storage

#### Implementation Details

In [`importer.py`](https://github.com/NL-BioImaging/BIOMERO.importer/blob/main/biomero_importer/utils/importer.py),
`DataProcessor.get_preprocessing_args()` creates the configured subfolder next
to the input and passes the matching converter path as `--outputfolder`.
`DataProcessor.run()` maps the converter's reported `full_path` and `alt_path`
back to host paths using the Podman mounts. `upload_files()` uses those reported
paths for registration or symlink redirection; it does not reconstruct the final
pixel location from a hardcoded `.processed` name.

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
- Configured processed data directory (`PROCESSED_DATA_FOLDER`, default `.processed`)

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
