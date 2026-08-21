"""Seam for the Chroma persistent client.

Status: unused seam. Nothing calls this, and no semantic store is wired, so
``chromadb`` is imported inside the function rather than at module scope to keep
the dependency off the import path of a system that does not use it.

``chromadb`` is no longer a declared dependency. It was removed because of
CVE-2026-45829, a critical pre-authentication code injection in the Chroma
server: an unauthenticated request to the collections endpoint can execute
arbitrary code by supplying a malicious model repository with
``trust_remote_code`` set. Every release from 1.0.0 through 1.5.9 is affected
and no patched version has been published, so there was nothing to upgrade to.
Since this seam had no callers, removing the package removed the exposure.

Do not re-add ``chromadb`` to ``pyproject.toml`` without checking whether a
fixed release exists. If none does, the semantic store needs either a different
vector store or a deployment that never exposes the Chroma server. Note that
running Chroma embedded still pulls in the vulnerable code, even though the
vulnerable path is the server's HTTP endpoint.
"""

from pathlib import Path


def persistent_client(path: Path) -> object:
    """Create the local Chroma persistent client when semantic stores are wired."""
    import chromadb

    return chromadb.PersistentClient(path=str(path))
