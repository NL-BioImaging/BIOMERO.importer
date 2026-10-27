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

## Interrupted imports and recovery

Recovery is automatic and needs no additional feature flag. Ordinary imports
without a lifecycle operation are unaffected. Before inspecting a shallow result,
the importer obtains the shared
shallower filesystem lock and recovers any interrupted prune journal. Recovery
restores uncommitted pixel moves and metadata before evaluating the store or
reusing its shallow manifest. The same path applies to v2 and v3. The lock remains
held through normalization and registration-plan preparation; it is released if
the process dies. Recovery storage must provide working shared filesystem locks
and the shallower's atomic move and synchronization requirements.

An explicit unsupported-lock error uses legacy preparation with a warning,
provided no interrupted prune journal exists. This retains the existing
normalization behavior without adding recovery or concurrency protection.
Busy locks, permissions, storage errors and failures during recovery or
normalization do not trigger this fallback. Interrupted transactions still
require working locks before preparation can continue.

A busy store, conflicting journal or incomplete recovery fails preparation.
The importer does not register the incomplete store or reinterpret it as a full
result. Keep the result and its sibling `.biomero-prune-*` journal for diagnosis;
do not delete the journal to bypass recovery. Symlinked result roots are rejected.
Resolve an interrupted journal on storage supporting recovery before retrying.

There is no migration or scan of old OMERO images, Plates or Zarrs. The scripts'
small recovery record belongs to a newly staged workflow-result import plan,
not each image; historical data is considered only when explicitly retried.

Filesystem recovery is separate from OMERO registration. An order can fail after
registering some of its objects, and startup marks interrupted orders failed.
Review the order's log and registered objects before issuing a replacement.
These changes do not automatically requeue failed orders or guarantee exactly
one registration for arbitrary external clients that submit duplicate orders.
BIOMERO result scripts use a saved order plan to resume polling existing orders.
Before creating a plan they test the new durable-write requirements using
disposable metadata. Unsupported file synchronization, atomic replacement or
POSIX directory synchronization selects legacy order submission before any
import is queued. A saved plan cannot be bypassed through this fallback.
The importer's existing normalization write requirements are unchanged.

## Image and Plate registration

An eligible Image result registers its shallow collection root as the primary
OMERO Image. Its PixelBuffer uses the canonical source pixels and its shallow
reference exposes the result labels to label-aware viewers. When
`importImageLabelViews` is enabled, the importer also registers lightweight
label projections so TIFF-based workflows can select and export an individual
mask without copying its arrays.

The default Plate registration uses canonical source pixels while preserving
the result's Plate, Well and WellSample hierarchy. The optional
`platePixelSource="label"` and `plateLabelName` registration options create a
label-backed Plate view without copying arrays. The selected label must exist
on every image; incomplete selections fail instead of substituting a label.

### Label image names and display

Imported label images include their source store and, for nested images, their
field or series path. For example, `result.ome.zarr/labels/labels_cells` becomes
`result_labels_cells`; `plate.zarr/A/1/0/labels/cells` becomes
`plate_A_1_0_cells`. This changes the OMERO name only.

The importer preserves valid `omero.channels[].window` settings on the label
group itself. Missing channel activation and colour settings default to active
and white. It does not copy contrast settings from the source intensity image.
When a label window is missing or invalid, the importer reads the
full-resolution label array one storage chunk at a time and saves a window
from zero to its maximum ID in OMERO. All-background masks use 0–1. Label IDs,
array types and Zarr metadata remain unchanged.

This fallback adds a full label-array read during import; its working memory
depends on storage chunk size. Producers can avoid that scan by writing valid
display windows on each label group, for example:

```json
{
  "omero": {
    "channels": [{
      "label": "Cells",
      "active": true,
      "color": "FFFFFF",
      "window": {"min": 0, "max": 4294967295, "start": 0, "end": 407272}
    }]
  }
}
```

Use the actual maximum label ID, not the number of objects or a reduced
pyramid's maximum. For OME-Zarr 0.5 this metadata belongs under the group's
`ome` attribute; see the [OME-Zarr specification](https://ngff.openmicroscopy.org/0.5/).
Existing imported images keep their saved names and rendering settings.

## Provenance

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
