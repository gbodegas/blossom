"""Retrieval: exact lookup by key, semantic lookup by resemblance, and a router.

A question with a lookup key (a due date) goes to structured state: similarity
would return the most similar assignment rather than the correct one, and a
confidently wrong deadline is worse than none. Questions with no key go to
vector search. Every result records its store, source channel, assertion time,
and retrieval time. ``NothingRetrieved`` is a value, not an error, because vector
search always returns a top hit even when nothing in the corpus is relevant.
"""

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class RetrievalQuery(BaseModel):
    """A retrieval question. ``lookup_key`` present means exact lookup, absent
    means semantic; ``min_score`` is the floor below which a semantic result is
    discarded.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    lookup_key: str | None = None
    min_score: float = 0.75


class RetrievalResult(BaseModel):
    """A retrieved record with its provenance.

    ``asserted_at`` is when the source claimed it; ``retrieved_at`` is when this
    system read it. A record can be accurate when observed and stale when read.
    """

    model_config = ConfigDict(extra="forbid")

    store_name: str
    record_id: str
    source_channel: str
    retrieved_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    asserted_at: AwareDatetime
    score: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NothingRetrieved(BaseModel):
    """A retrieval that found nothing.

    A value rather than an exception or an empty list. ``reason`` distinguishes
    an empty store from a candidate that scored below the threshold.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    retrieved_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


type RetrievalResponse = RetrievalResult | NothingRetrieved


class Retriever(Protocol):
    """Anything that can answer a ``RetrievalQuery``, exactly or approximately."""

    store_name: str

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Answer the query, or report that nothing was found."""
        ...


class StructuredRepository(Protocol):
    """A store that can resolve an exact key. Implemented by ``ProjectStateStore``."""

    def lookup(self, key: str) -> RetrievalResult | None:
        """Return the record for ``key``, or ``None`` when the key is unknown."""
        ...


class StructuredRetriever:
    """Exact retrieval. Refuses a query with no key rather than guessing at one."""

    store_name = "project_state"

    def __init__(self, repository: StructuredRepository) -> None:
        self._repository = repository

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Resolve the query's key, or explain why nothing was returned."""
        if query.lookup_key is None:
            return NothingRetrieved(reason="structured retrieval requires a lookup key")
        result = self._repository.lookup(query.lookup_key)
        if result is None:
            return NothingRetrieved(reason=f"no structured record for {query.lookup_key}")
        return result


class Collection(Protocol):
    """The part of the Chroma collection API this package uses. Kept narrow so a
    test can substitute a fake.
    """

    def query(
        self,
        *,
        query_texts: list[str],
        n_results: int,
    ) -> dict[str, list[list[object]]]:
        """Return the ``n_results`` nearest entries to each query text."""
        ...


class SemanticRetriever:
    """Approximate retrieval with a threshold so it can decline to answer.

    Score is ``1.0 - distance`` and assumes a unit-interval metric; with Chroma's
    default squared L2 the threshold has no defined meaning until the collection
    is created with an explicit metric. Only the nearest neighbor is fetched, so
    nothing distinguishes a confident match from the only candidate; three to
    five would.
    """

    store_name = "semantic"

    def __init__(self, collection: Collection, *, store_name: str, source_channel: str) -> None:
        self._collection = collection
        self.store_name = store_name
        self._source_channel = source_channel

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Return the nearest entry if it clears ``min_score``, otherwise nothing."""
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
        asserted_at = datetime.now(UTC)
        if asserted_at_value is not None:
            try:
                asserted_at = datetime.fromisoformat(str(asserted_at_value))
            except ValueError:
                return NothingRetrieved(reason="semantic store returned an unreadable timestamp")
            if asserted_at.tzinfo is None:
                # A stored instant without an offset could mean any of
                # twenty-some moments, so it is refused rather than guessed at.
                return NothingRetrieved(reason="semantic store returned a naive timestamp")
        return RetrievalResult(
            store_name=self.store_name,
            record_id=str(ids[0]),
            source_channel=self._source_channel,
            asserted_at=asserted_at,
            score=score,
            payload=metadata,
        )


class RetrievalRouter:
    """Chooses the retrieval mechanism from the shape of the question.

    The switch is key presence alone, not a heuristic, a score, or a model call,
    so a due date never reaches the semantic path.
    """

    def __init__(self, structured: Retriever, semantic: Retriever) -> None:
        self._structured = structured
        self._semantic = semantic

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Send keyed queries to the structured store and the rest to semantic."""
        if query.lookup_key is not None:
            return self._structured.retrieve(query)
        return self._semantic.retrieve(query)
