from pathlib import Path


def persistent_client(path: Path) -> object:
    """Create the local Chroma persistent client when semantic stores are wired."""
    import chromadb

    return chromadb.PersistentClient(path=str(path))
