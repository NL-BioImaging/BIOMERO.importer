"""Compatibility exports for the shared filesystem implementation."""
from biomero_shallower import result_zarr as _implementation

# Preserve private compatibility imports and monkeypatch targets as well.
globals().update({name: value for name, value in vars(_implementation).items()
                  if not name.startswith("__")})
__all__ = _implementation.__all__
