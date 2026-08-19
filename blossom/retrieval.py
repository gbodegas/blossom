from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    lookup_key: str | None = None
    min_score: float = 0.75


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_name: str
    record_id: str
    source_channel: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    asserted_at: datetime
    score: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NothingRetrieved(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


type RetrievalResponse = RetrievalResult | NothingRetrieved


class Retriever(Protocol):
    store_name: str

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        ...


class StructuredRepository(Protocol):
    def lookup(self, key: str) -> RetrievalResult | None:
        ...


class StructuredRetriever:
    store_name = "project_state"

    def __init__(self, repository: StructuredRepository) -> None:
        self._repository = repository

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        if query.lookup_key is None:
            return NothingRetrieved(reason="structured retrieval requires a lookup key")
        result = self._repository.lookup(query.lookup_key)
        if result is None:
            return NothingRetrieved(reason=f"no structured record for {query.lookup_key}")
        return result


class Collection(Protocol):
    def query(
        self,
        *,
        query_texts: list[str],
        n_results: int,
    ) -> dict[str, list[list[object]]]:
        ...


class SemanticRetriever:
    store_name = "semantic"

    def __init__(self, collection: Collection, *, store_name: str, source_channel: str) -> None:
        self._collection = collection
        self.store_name = store_name
        self._source_channel = source_channel

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        raw = self._collection.query(query_texts=[query.text], n_results=1)
        ids = raw.get("ids", [[]])[0]
        distances = raw.get("distances", [[]])[0]
        metadatas = raw.get("metadatas", [[]])[0]
        if not ids or not distances:
            return NothingRetrieved(reason="semantic store returned no candidates")
        distance = distances[0]
        if not isinstance(distance, int | float):
            return NothingRetrieved(reason="semantic store returned an invalid distance")
        score = 1.0 - float(distance)
        if score < query.min_score:
            return NothingRetrieved(reason="semantic score below threshold")
        metadata_value = metadatas[0] if metadatas else {}
        metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}
        asserted_at_value = metadata.get("asserted_at")
        asserted_at = (
            datetime.fromisoformat(str(asserted_at_value))
            if asserted_at_value is not None
            else datetime.now(UTC)
        )
        return RetrievalResult(
            store_name=self.store_name,
            record_id=str(ids[0]),
            source_channel=self._source_channel,
            asserted_at=asserted_at,
            score=score,
            payload=metadata,
        )


class RetrievalRouter:
    def __init__(self, structured: Retriever, semantic: Retriever) -> None:
        self._structured = structured
        self._semantic = semantic

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        if query.lookup_key is not None:
            return self._structured.retrieve(query)
        return self._semantic.retrieve(query)
