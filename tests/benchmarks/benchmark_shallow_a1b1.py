"""Profile A1/B1 returned-Plate verification and normalization strategies.

This is a manual benchmark, not part of the unit-test suite.  The full mode
needs three identical disposable full-result copies: one for ISCC verification,
one for the former retained-tree staging strategy, and one for the move-journal
normalizer with diagnostic size scans.  ``--move-fast-root`` profiles the
production move-journal path without those scans. A previously produced shallow
manifest supplies the canonical input identities and label inventory.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
from threading import Lock
from time import perf_counter
from uuid import UUID, uuid4

from biomero_schema.zarr import (
    CanonicalInput,
    CanonicalInputManifest,
    CanonicalPlateImage,
    CanonicalPlateSource,
    SHALLOW_COLLECTION_MANIFEST,
    ShallowCollection,
)
from biomero_schema.imports import (
    ImportOptionsEnvelope,
    ShallowZarrImportOperation,
)

from biomero_importer.utils import lifecycle as lifecycle_module
from biomero_importer.utils.lifecycle import ImportLifecycleEngine
from biomero_importer.utils import result_zarr as rz
from biomero_importer.utils.pixel_identity import IsccBioIdentityProvider


def timed_call(timings, name, function, *args, **kwargs):
    started = perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        timings[name] += perf_counter() - started


def build_contracts(root: Path, manifest_path: Path):
    collection = ShallowCollection.from_dict(json.loads(
        manifest_path.read_text(encoding="utf-8")
    ))
    first = collection.images[0].source
    plate_images = tuple(
        CanonicalPlateImage(
            image_node_path=image.image_node_path,
            source=image.source,
            labels=tuple(
                component for component in image.label_components
                if component.source is not None
            ),
        )
        for image in collection.images
    )
    plate_source = CanonicalPlateSource(
        storage_root=first.storage_root,
        relative_path=first.relative_path,
        source_object_id=first.source_object_id,
        source_generation=first.source_generation,
        interchange_profile=first.interchange_profile,
        images=plate_images,
    )
    matched = CanonicalInput(
        ordinal=0,
        selected_object_type="Plate",
        selected_object_id=first.source_object_id,
        transfer_artifact=root.name,
        plate_source=plate_source,
    )
    canonical = CanonicalInputManifest(
        workflow_id=collection.workflow_id,
        export_task_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        inputs=(matched,),
    )
    components = tuple(
        component
        for image in collection.images
        for component in image.label_components
    )
    decision = rz.ReturnedZarrDecision(
        store_path=root,
        outcome="eligible",
        reason="input-plate-unchanged",
        image_identities=tuple(
            image.returned_pixel_identity for image in collection.images
        ),
        label_identities=tuple(
            component.pixel_identity for component in components
        ),
        label_components=components,
        label_node_paths=tuple(
            component.logical_node_path for component in components
        ),
        matched_inputs=(matched,),
    )
    return collection, canonical, decision


class TimedIdentityProvider:
    def __init__(self):
        self.delegate = IsccBioIdentityProvider()
        self.timings = defaultdict(float)
        self.calls = defaultdict(int)
        self.lock = Lock()

    def generate(self, root, **kwargs):
        role = kwargs["role"]
        started = perf_counter()
        try:
            return self.delegate.generate(root, **kwargs)
        finally:
            elapsed = perf_counter() - started
            with self.lock:
                self.calls[role] += 1
                self.timings[f"hash_{role}_seconds"] += elapsed


def profile_identity(
    root: Path,
    manifest_path: Path,
    *,
    identity_workers: int = 1,
):
    _, canonical, _ = build_contracts(root, manifest_path)
    provider = TimedIdentityProvider()
    timings = defaultdict(float)
    original_discover = rz.discover_ngff_nodes
    original_identity_batch = rz._identities_for_nodes

    def discover(*args, **kwargs):
        return timed_call(
            timings,
            "ngff_discovery_seconds",
            original_discover,
            *args,
            **kwargs,
        )

    def identity_batch(root, nodes, identity_provider, **kwargs):
        role = nodes[0].role if nodes else "empty"
        return timed_call(
            timings,
            f"hash_{role}_phase_seconds",
            original_identity_batch,
            root,
            nodes,
            identity_provider,
            **kwargs,
        )

    rz.discover_ngff_nodes = discover
    rz._identities_for_nodes = identity_batch
    started = perf_counter()
    try:
        decision = rz.evaluate_returned_zarr(
            root,
            canonical,
            identity_provider=provider,
            identity_workers=identity_workers,
        )
    finally:
        rz.discover_ngff_nodes = original_discover
        rz._identities_for_nodes = original_identity_batch
    total = perf_counter() - started
    return {
        "total_seconds": total,
        **timings,
        "aggregate_image_worker_seconds": provider.timings[
            "hash_image_seconds"
        ],
        "aggregate_label_worker_seconds": provider.timings[
            "hash_label_seconds"
        ],
        "image_hash_calls": provider.calls["image"],
        "label_hash_calls": provider.calls["label"],
        "identity_workers": identity_workers,
        "decision": decision.outcome,
        "unaccounted_seconds": total - sum(timings.values()),
    }


def legacy_stage_normalize(root: Path, manifest_path: Path):
    """Profile the former copy-retained-side implementation."""
    collection, _, decision = build_contracts(root, manifest_path)
    timings = defaultdict(float)
    started = perf_counter()
    nodes = timed_call(
        timings, "ngff_discovery_seconds", rz.discover_ngff_nodes, root
    )
    image_nodes = tuple(node for node in nodes if node.role == "image")
    omitted = set()
    planning_started = perf_counter()
    for image_node in image_nodes:
        omitted.update(rz._declared_image_dataset_directories(
            root,
            image_node.node_path,
        ))
    inherited = {
        component.logical_node_path
        for component in decision.label_components
        if component.source is not None
    }
    omitted.update(rz._node_directory(root, path) for path in inherited)
    timings["normalization_planning_seconds"] += (
        perf_counter() - planning_started
    )
    bytes_before = timed_call(
        timings, "tree_size_before_seconds", rz._tree_size, root
    )
    token = uuid4().hex
    staging = root.with_name(f".{root.name}.legacy-stage-{token}")
    backup = root.with_name(f".{root.name}.legacy-full-{token}")

    def ignore(directory, names):
        current = Path(directory)
        return {name for name in names if current / name in omitted}

    timed_call(
        timings,
        "copy_retained_tree_seconds",
        shutil.copytree,
        root,
        staging,
        symlinks=True,
        ignore=ignore,
    )
    metadata_started = perf_counter()
    for image_node in image_nodes:
        attrs_path = rz._node_directory(staging, image_node.node_path) / ".zattrs"
        attrs = rz._read_attrs(attrs_path.parent)
        attrs.pop("multiscales", None)
        attrs["biomero"] = {
            "model": collection.model,
            "manifest": SHALLOW_COLLECTION_MANIFEST,
            "workflowId": str(collection.workflow_id),
        }
        rz._write_json(attrs_path, attrs)
    rz._write_json(
        staging / SHALLOW_COLLECTION_MANIFEST,
        collection.to_dict(),
    )
    ShallowCollection.from_dict(json.loads(
        (staging / SHALLOW_COLLECTION_MANIFEST).read_text(encoding="utf-8")
    ))
    timings["metadata_manifest_seconds"] += perf_counter() - metadata_started
    swap_started = perf_counter()
    os.replace(root, backup)
    os.replace(staging, root)
    timings["atomic_swap_seconds"] += perf_counter() - swap_started
    bytes_after = timed_call(
        timings, "tree_size_after_seconds", rz._tree_size, root
    )
    timed_call(
        timings, "delete_full_backup_seconds", shutil.rmtree, backup
    )
    total = perf_counter() - started
    return {
        "total_seconds": total,
        **timings,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "unaccounted_seconds": total - sum(timings.values()),
    }


def profile_move_normalize(
    root: Path,
    manifest_path: Path,
    *,
    measure_bytes: bool = True,
):
    collection, _, decision = build_contracts(root, manifest_path)
    timings = defaultdict(float)
    counts = defaultdict(int)
    originals = {
        "discover": rz.discover_ngff_nodes,
        "datasets": rz._declared_image_dataset_directories,
        "tree_size": rz._tree_size,
        "read_attrs": rz._read_attrs,
        "write_json": rz._write_json,
        "rmtree": rz.shutil.rmtree,
    }

    def discover(*args, **kwargs):
        return timed_call(
            timings, "ngff_discovery_seconds", originals["discover"],
            *args, **kwargs
        )

    def datasets(*args, **kwargs):
        return timed_call(
            timings, "dataset_planning_seconds", originals["datasets"],
            *args, **kwargs
        )

    def tree_size(*args, **kwargs):
        index = counts["tree_size"]
        counts["tree_size"] += 1
        name = (
            "tree_size_before_seconds" if index == 0
            else "tree_size_after_seconds"
        )
        return timed_call(
            timings, name, originals["tree_size"], *args, **kwargs
        )

    def read_attrs(*args, **kwargs):
        counts["attrs_reads"] += 1
        return timed_call(
            timings, "attrs_read_seconds", originals["read_attrs"],
            *args, **kwargs
        )

    def write_json(*args, **kwargs):
        counts["json_writes"] += 1
        return timed_call(
            timings, "metadata_manifest_write_seconds",
            originals["write_json"], *args, **kwargs
        )

    def rmtree(*args, **kwargs):
        counts["rmtree_calls"] += 1
        return timed_call(
            timings, "delete_pruned_arrays_seconds", originals["rmtree"],
            *args, **kwargs
        )

    def replace(source, target):
        counts["array_moves"] += 1
        return timed_call(
            timings, "move_arrays_seconds", os.replace, source, target
        )

    rz.discover_ngff_nodes = discover
    rz._declared_image_dataset_directories = datasets
    rz._tree_size = tree_size
    rz._read_attrs = read_attrs
    rz._write_json = write_json
    rz.shutil.rmtree = rmtree
    started = perf_counter()
    try:
        result = rz.normalize_returned_zarr(
            decision,
            collection.workflow_id,
            replace=replace,
            measure_bytes=measure_bytes,
        )
    finally:
        rz.discover_ngff_nodes = originals["discover"]
        rz._declared_image_dataset_directories = originals["datasets"]
        rz._tree_size = originals["tree_size"]
        rz._read_attrs = originals["read_attrs"]
        rz._write_json = originals["write_json"]
        rz.shutil.rmtree = originals["rmtree"]
    total = perf_counter() - started
    return {
        "total_seconds": total,
        **timings,
        **counts,
        "bytes_before": result.bytes_before,
        "bytes_after": result.bytes_after,
        "unaccounted_seconds": total - sum(timings.values()),
    }


def profile_importer_lifecycle(root: Path, manifest_path: Path):
    """Profile the production importer-owned evaluate/normalize hand-off."""

    _, canonical, _ = build_contracts(root, manifest_path)
    options = ImportOptionsEnvelope(operations=(
        ShallowZarrImportOperation(canonicalInputs=canonical),
    ))
    timings = defaultdict(float)
    original_evaluate = lifecycle_module.evaluate_returned_zarr
    original_normalize = lifecycle_module.normalize_returned_zarr

    def evaluate(*args, **kwargs):
        return timed_call(
            timings,
            "identity_evaluation_seconds",
            original_evaluate,
            *args,
            **kwargs,
        )

    def normalize(*args, **kwargs):
        return timed_call(
            timings,
            "normalization_seconds",
            original_normalize,
            *args,
            **kwargs,
        )

    bytes_before = rz._tree_size(root)
    lifecycle_module.evaluate_returned_zarr = evaluate
    lifecycle_module.normalize_returned_zarr = normalize
    started = perf_counter()
    try:
        plan = ImportLifecycleEngine().prepare((root,), options)
    finally:
        lifecycle_module.evaluate_returned_zarr = original_evaluate
        lifecycle_module.normalize_returned_zarr = original_normalize
    total = perf_counter() - started
    bytes_after = rz._tree_size(root)
    roles = defaultdict(int)
    for item in plan.items:
        roles[item.role] += 1
    return {
        "total_seconds": total,
        **timings,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "bytes_saved": bytes_before - bytes_after,
        "saved_percent": (
            100 * (bytes_before - bytes_after) / bytes_before
            if bytes_before else 0
        ),
        "decision": plan.decisions[0].outcome if plan.decisions else None,
        "prepared_items": len(plan.items),
        "prepared_roles": dict(sorted(roles.items())),
        "unaccounted_seconds": total - sum(timings.values()),
    }


def rounded(value):
    if isinstance(value, float):
        return round(value, 3)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity-root", type=Path)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--move-root", type=Path)
    parser.add_argument("--move-fast-root", type=Path)
    parser.add_argument("--identity-only-root", type=Path)
    parser.add_argument("--lifecycle-root", type=Path)
    parser.add_argument("--identity-workers", type=int, default=1)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.lifecycle_root is not None:
        result = {
            "importer_lifecycle": profile_importer_lifecycle(
                args.lifecycle_root,
                args.manifest,
            ),
        }
    elif args.identity_only_root is not None:
        result = {
            "identity_verification": profile_identity(
                args.identity_only_root,
                args.manifest,
                identity_workers=args.identity_workers,
            ),
        }
    elif args.move_fast_root is not None:
        result = {
            "move_journal_without_size_scans": profile_move_normalize(
                args.move_fast_root,
                args.manifest,
                measure_bytes=False,
            ),
        }
    else:
        if not all((args.identity_root, args.legacy_root, args.move_root)):
            parser.error(
                "identity, legacy, and move roots are required for the full "
                "benchmark"
            )
        result = {
            "identity_verification": profile_identity(
                args.identity_root,
                args.manifest,
                identity_workers=args.identity_workers,
            ),
            "legacy_copy_staging": legacy_stage_normalize(
                args.legacy_root, args.manifest
            ),
            "move_journal": profile_move_normalize(
                args.move_root, args.manifest
            ),
        }
    print(json.dumps({
        group: {key: rounded(value) for key, value in values.items()}
        for group, values in result.items()
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
