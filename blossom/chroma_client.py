"""Seam for the Chroma persistent client.

Status: unused seam. Nothing calls this, and no semantic store is wired, so
``chromadb`` is imported inside the function rather than at module scope to keep
the dependency off the import path of a system that does not use it.

Worth knowing: ``chromadb`` is declared as a runtime dependency in
``pyproject.toml`` while having zero callers, so it is installed for every
environment including CI purely to hold this seam open. Moving it to an
optional group would be honest, and is a dependency change rather than a
documentation one.
"""

from pathlib import Path


def persistent_client(path: Path) -> object:
    """Create the local Chroma persistent client when semantic stores are wired."""
    import chromadb

    return chromadb.PersistentClient(path=str(path))
