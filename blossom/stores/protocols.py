from typing import Protocol


class Store(Protocol):
    retention_policy: str
    name: str
