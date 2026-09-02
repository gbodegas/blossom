"""Seam for the Chroma persistent client.

Status: placeholder; nothing calls this and no semantic store is wired.
``chromadb`` is imported inside the function so the dependency stays off the
import path.

``chromadb`` is not a declared dependency because of CVE-2026-45829, a critical
pre-authentication code injection in the Chroma server: an unauthenticated
request to the collections endpoint executes code via a malicious model
repository with ``trust_remote_code`` set. Every release from 1.0.0 through
1.5.9 is affected and no patched version has been published. Do not re-add
``chromadb`` to ``pyproject.toml`` without checking for a fixed release. If none
exists, the semantic store needs a different vector store or a deployment that
never exposes the Chroma server. Running Chroma embedded still pulls in the
vulnerable code, even though the vulnerable path is the server's HTTP endpoint.
"""

from pathlib import Path


def persistent_client(path: Path) -> object:
    """Create the local Chroma persistent client when semantic stores are wired."""
    import chromadb

    return chromadb.PersistentClient(path=str(path))
