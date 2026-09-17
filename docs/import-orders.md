# Import orders

Upload orders are typically created through a user interface, such as the OMERO.biomero plugin (Importer tab) at `/omero_biomero/biomero/`, an OMERO.web extension. However, orders can also be created programmatically. New integrations should call `biomero_importer.submit_import_order(order)` and inspect `biomero_importer.get_importer_capabilities()` before requesting an optional lifecycle operation. The API validates and writes the same append-only database order used by existing clients; direct legacy database writers remain supported.

## Client API

Configure `INGEST_TRACKING_DB_URL` and the importer environment before submitting.
Paths must be accessible to the importer and OMERO through their shared mounts.

```python
from uuid import uuid4
from biomero_importer import submit_import_order

order = {
    "Group": "research-group",
    "Username": "researcher",
    "UUID": str(uuid4()),
    "DestinationID": dataset_id,
    "DestinationType": "Dataset",
    "Files": ["/data/experiment/image.tif"],
}
order_uuid = submit_import_order(order)
```

Use an existing destination ID appropriate to the data (`Screen` for Plates).
Submission queues an order; it does not wait for its import to finish.

## Example scripts

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

See [shallow storage](shallow-zarr.md) for canonical inputs and label-backed Plate registration.

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
