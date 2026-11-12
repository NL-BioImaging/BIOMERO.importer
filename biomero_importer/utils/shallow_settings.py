"""Environment settings of shallow-Zarr handling, read and parsed in one place.

This module has no heavy imports so that the API, the importer and the
lifecycle can all use it without pulling in optional dependencies.
"""

import os

ENV_SHALLOW_ZARR = "BIOMERO_SHALLOW_ZARR"
ENV_SHALLOW_WORKERS = "BIOMERO_SHALLOW_ZARR_WORKERS"
ENV_REMOTE_SHALLOW_ZARR = "BIOMERO_REMOTE_SHALLOW_ZARR"

# Shallowing is opt-in; consuming remote receipts is on unless switched off.
DEFAULT_SHALLOW_ZARR = False
DEFAULT_REMOTE_SHALLOW_ZARR = True
DEFAULT_SHALLOW_WORKERS = 1

_TRUE_VALUE = "true"


def env_flag_enabled(name: str, default: bool) -> bool:
    """Whether an environment flag is set to ``true`` (case-insensitive).

    Args:
        name: Environment variable to read.
        default: Value used when the variable is not set.

    Returns:
        True only if the (stripped) value equals ``true`` ignoring case.
    """
    return os.getenv(name, str(default)).strip().lower() == _TRUE_VALUE
