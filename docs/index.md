# BIOMERO.importer

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

![Flow Diagram of BIOMERO.importer process](https://raw.githubusercontent.com/NL-BioImaging/BIOMERO.importer/main/flow_diagram_ADI_import.png)

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
- Stores optional Zarr registration choices in the nullable `import_options`
  JSON text column. Existing orders without it behave exactly as before.

### `imports_preprocessing`
- Stores preprocessing configuration for containerized workflows
- Links to imports records via foreign key
- Supports dynamic parameters via JSON field

## Guides

- [Administration](administration.md): configuration, environment and monitoring.
- [Storage and preprocessing](storage.md): shared paths, converter output and permissions.
- [Import orders](import-orders.md): submitting data through the client API.
- [Shallow Zarr](shallow-zarr.md): canonical pixels, local normalization and remote receipts.
- [Development](development.md): tests and database migrations.
- [Container security](security.md): nested Podman and deployment constraints.

For deployment of the complete platform, start with
[NL-BIOMERO](https://nl-bioimaging.github.io/NL-BIOMERO/).
