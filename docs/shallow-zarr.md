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

Within an enabled operation, usable RO-Crate output declarations take
precedence, followed by the workflow's saved `original_pixels_not_required`
setting. Both routes reuse the known source without measuring original pixels
again. Otherwise the shared shallower compares decoded pixels at the coarsest
compatible pyramid level; labels retain full-resolution comparison. Full-image
shape, axes, dtype and coordinate guards still apply. Missing or stale comparison
metadata is refreshed from the same canonical backing store when needed; no
source export, copy or reimport is required. Fresh OMERO exports retain their
separate full-resolution correctness check.
Remote helpers without a current source comparison cache keep full results for
local evaluation against managed canonical storage. A mutable HPC input copy
cannot refresh that authoritative cache.

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

For remote shallowing, enable `BIOMERO_REMOTE_SHALLOW_ZARR`. Receipt trust
assumes that the sender of the import order, BIOMERO's own scripts, is trusted;
this is a deliberate decision, so there is no allow-list of helper images or
versions. The receipt's report checksum binds the report to the receipt but is
not independent of it, because both arrive in the same import options. The
importer also validates the report and manifest with its installed
`biomero-shallower` package, and only logs the receipt's image. If the receipt
reports another shallower version, the importer logs a warning naming both
versions and the image after validation succeeds. A kept-full result is
imported in full and a compatible shallow result is accepted. If the installed
package cannot parse the report or manifest (incompatible schema, including
unknown fields), the import fails with an error naming both versions; install
the matching `biomero-shallower` in the importer or rerun the workflow. A
result the helper already shallowed cannot be kept full. Dependency
compatibility is maintained in `pyproject.toml`, not in this guide.

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
canonical sources cause an error. A trusted kept-full receipt can also refresh
canonical comparison metadata without repeating an unsuccessful comparison.
Full results without such a receipt can use local shallowing. A corrupt or incomplete shallow result is not
silently treated as a full Zarr.

Canonical storage must remain available through the configured shared mounts
and storage mappings. Deleting or moving the canonical arrays breaks results
that reference them. See [storage and preprocessing](storage.md).

## Canonical source metadata

Indexing a canonical source records its detailed identities (pixel identity,
plate image and label inventory) in a canonical marker. This is metadata, not
another pixel copy.

| Zarr | Marker |
| --- | --- |
| Created by BIOMERO (inside a `.processed` folder) | `.biomero-canonical.json` inside the Zarr |
| Existing user Zarr indexed in place | `<zarr parent>/.processed/.biomero/<zarr name>.canonical.json` |

For example, indexing `/data/groupA/project/cells.ome.zarr` in place writes
`/data/groupA/project/.processed/.biomero/cells.ome.zarr.canonical.json`. This
is the same per-directory `.processed` folder BIOMERO already uses for its
other outputs, and the name comes from `PROCESSED_DATA_FOLDER` (default
`.processed`). Set it identically wherever BIOMERO code runs (importer and
OMERO script worker), because the location is derived from it; the worker
receives it through BIOMERO core's `slurm_env` settings. The value must be one
folder name; an empty, absolute or multi-segment value is ignored with a
warning and `.processed` is used.

BIOMERO writes only into its own folders (`.processed`, `.analyzed`); users
agree not to touch them, and BIOMERO never writes into user folders, because
users may move or delete their data at any time. The location depends only on
the Zarr's own path, not on storage mappings, so every component derives the
same path and it moves or is deleted together with the user's folder.
`external_canonical_marker_path` in `biomero_importer/utils/canonical_store.py`
is the single owner of this location; other components call it.

The marker is an optimisation. If the BIOMERO folder cannot be created or
written (permissions, read-only mount), BIOMERO logs a warning with the path
and reason and does not index the input: the workflow continues and the input
is used through the normal path (for an Image, no canonical registration is
added; for a Plate, no detailed inventory is stored), without shallow result
matching for that input. A corrupt, uncommitted or unreachable sidecar is
treated the same way: as absent, with a warning. Every sidecar problem degrades
to the normal path, so no pre-flight write check is needed.

Earlier betas wrote the marker next to the input, in
`<input parent>/.biomero/<name>.canonical.json`. That file is still read. The
next write of the marker (indexing, trust upgrade or comparison refresh) puts
it in the BIOMERO folder and removes only BIOMERO's own old file, and the
`.biomero` folder only when it is then empty; if the old file cannot be removed
a warning is logged and the new file stays authoritative. Reading never moves
or deletes. An importer from before this change does not read the new location,
so after a downgrade indexed inputs are treated as not indexed until they are
indexed again; release the importer and biomero-scripts changes together.

Back up the `.processed` folders together with the data they belong to; the
sidecars live in their `.biomero` subfolders.

## Interrupted imports and recovery

Recovery is automatic within an explicitly requested shallow lifecycle operation
and needs no additional feature flag. `BIOMERO_SHALLOW_ZARR=true` is still
required; disabled shallow operations are rejected before locking or recovery.
Remote receipts also require `BIOMERO_REMOTE_SHALLOW_ZARR` to remain enabled.
If local shallowing fails after the eligibility check (corrupt metadata,
identity or filesystem errors), the transaction is rolled back, a warning is
logged and the full result is imported, recorded as `full-zarr` with reason
`normalization-failed: <error>`. If the failure happens after the commit point
(for example while deleting the rollback folder), the committed shallow result
is used and the next automatic recovery finishes the cleanup of the leftover
`.<name>.biomero-prune-*` folder. Never delete that folder or its journal by
hand: the journal is what lets recovery restore or finish a prune. From the
biomero-shallower release with the cleanup fix (BIOMERO.shallower#35) the
journal is deleted last, so a missing journal with pruned arrays left behind
cannot result from an interrupted post-commit cleanup.
Ordinary imports without a lifecycle operation are unaffected, even when shallow
storage is enabled. Before inspecting a shallow result,
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
Import provenance is mandatory: a file is successful only after its registered
objects have the import-order annotation. The importer keeps sessions active
during processing and checks a supplied session before starting a
connection-backed operation.
If it has expired, a new session is opened for the order's user and group.
Registration is not automatically repeated after it has started; if a session
fails during registration, reconcile any partial objects before retrying.
If annotation access fails, the importer makes up to three
annotation-only attempts, reconnecting as the original user in the original
group and reusing the exact registered IDs. A write whose response was lost is
accepted only after all expected provenance fields are found on the linked
annotation. Conflicting or incomplete provenance fails the import.

If these attempts fail, the order is marked failed and its description records
the registered object type and IDs. Preserve that event and repair those objects;
do not rerun registration to repair annotations. This bounded metadata recovery
does not make arbitrary resubmission of a failed or interrupted order safe.
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
