# Shallow Zarr storage

Shallow storage removes duplicate image or label arrays from returned OME-Zarr
results and refers to verified canonical pixels already in managed storage.
New or changed arrays remain in the result. The importer still registers the
result in OMERO and attaches its provenance, even when all image pixels match
the input. Canonical inputs are never pruned.

BIOMERO.shallower provides the shared shallowing implementation. The
importer runs that library locally or validates results shallowed on Slurm.
See the [shallower guide](https://nl-bioimaging.github.io/BIOMERO.shallower/)
for supported formats, filesystem requirements and standalone CLI use, and
the [architecture guide](https://nl-bioimaging.github.io/BIOMERO.shallower/architecture/)
for transactions and recovery.

## Enable shallow imports

Install `biomero-importer[identity]` and set `BIOMERO_SHALLOW_ZARR=true` in
the importer service and in processes that submit shallow orders. The
NL-BIOMERO importer container includes the identity dependencies. Restart the
service after changing its environment or package installation.

The feature is off when the flag is absent or false. Enabling it advertises
the `biomero.shallow-zarr` capability; it does **not** turn ordinary uploads
into shallow imports. An order must explicitly request the operation and
provide the trusted canonical-input manifest for its workflow.

The calling process can check `get_importer_capabilities()` before submitting
an order. This describes that process's installed dependencies and environment;
it does not query a remote importer service. Keep their configuration aligned.

## Choose where shallowing runs

| Route | Shallowing | Importer responsibility |
| --- | --- | --- |
| Local | Importer runs the shared shallower library after preprocessing | Compare identities, shallow, resolve sources and register results |
| Remote | BIOMERO runs the shallower container on Slurm before result archiving and transfer | Validate receipts and retained data, resolve sources and register results |

Within enabled shallow workflows, remote shallowing is preferred. To use
the local path, set `BIOMERO_REMOTE_SHALLOW_ZARR=false` on both the workflow
worker and importer. This avoids the additional Slurm shallowing job but
transfers full results and performs shallowing on the importer host.
`BIOMERO_SHALLOW_ZARR_WORKERS` controls local identity parallelism; choose it
for the host's CPU and storage capacity.

For remote shallowing, enable `BIOMERO_REMOTE_SHALLOW_ZARR` and configure
`BIOMERO_REMOTE_SHALLOWER_IMAGE` and `BIOMERO_REMOTE_SHALLOWER_VERSION`
identically on the workflow worker and importer. Select the container from
the [shallower releases](https://github.com/NL-BioImaging/BIOMERO.shallower/releases)
and use the Python package version reported by that container's
`biomero-shallower --version`. Docker tag spelling and Python version spelling
can differ for prereleases. Dependency compatibility is maintained in
`pyproject.toml`, not in this guide.

The importer does not submit or download the Slurm helper. Image acquisition,
initialization and worker configuration are described in the
[NL-BIOMERO remote-shallower guide](https://nl-bioimaging.github.io/NL-BIOMERO/master/sysadmin/remote-shallower.html).

## Submit a local shallow order

BIOMERO result scripts construct these orders automatically. Other integrations
can submit them through the importer API. They need an existing, trusted
`CanonicalInputManifest`: source identities, generations and managed-storage
references must describe the actual canonical inputs. Do not construct this
manifest from untrusted result metadata or use an empty manifest as a shortcut.

Given a normal upload-order dictionary `order` and that manifest:

```python
from biomero_importer import get_importer_capabilities, submit_import_order
from biomero_schema.imports import ImportOptionsEnvelope, ShallowZarrImportOperation

capabilities = get_importer_capabilities()
if "biomero.shallow-zarr" not in capabilities["lifecycleOperations"]:
    raise RuntimeError(capabilities["configurationErrors"] or "Shallow imports disabled")

order["ImportOptions"] = ImportOptionsEnvelope(
    operations=(ShallowZarrImportOperation(canonicalInputs=canonical_manifest),)
).to_dict()
order_uuid = submit_import_order(order)
```

See [import orders](import-orders.md) for required order fields and the
[schema documentation](https://nl-bioimaging.github.io/biomero-schema/)
for the canonical-input and operation contracts. The operation runs after
external preprocessing and before OMERO registration, so it can consume an
existing Zarr or a Zarr produced by a converter. Orders with no operation
retain their existing behavior.

## Import remotely shallowed results

The workflow's import order additionally supplies `remoteReceipts` from its
completed shallower task. The importer validates the report checksum,
configured image and tool version, canonical snapshot, task and job IDs,
shallow manifest and retained structure. A matching receipt avoids repeating
pixel hashing and shallowing. Local source resolution and OMERO
registration still take place in the importer.

A remote result cannot be accepted merely because its archive contains a
report. Missing trusted receipts, mismatched versions, tampering and unresolved
canonical sources cause an error. Full results safely retained by the helper
can use local shallowing. A corrupt or incomplete shallow result is not
silently treated as a full Zarr.

Canonical storage must remain available through the configured shared mounts
and storage mappings. Deleting or moving the canonical arrays breaks results
that reference them. See [storage and preprocessing](storage.md).

## Plate registration and provenance

The default Plate registration uses canonical source pixels while preserving
the result's Plate, Well and WellSample hierarchy. The optional
`platePixelSource="label"` and `plateLabelName` registration options create a
label-backed Plate view without copying arrays. The selected label must exist
on every image; incomplete selections fail instead of substituting a label.

Input `.biomero-input.json` markers disambiguate renamed results with identical
pixels. They must match the operation's canonical manifest and are consumed
before registration. Local shallowing still verifies returned pixels.

The importer records the actual storage outcome in
`.biomero-import-storage.json` beside the result. Local shallowing records
the executing library version. Accepted remote receipts also record the helper
container, task/job IDs and report checksum. Full-Zarr fallbacks are recorded
as full storage. BIOMERO result scripts use this data for the compact OMERO
storage-provenance view without hashing pixels again. Historical shallow stores
without an outcome receipt do not acquire an inferred execution version.
