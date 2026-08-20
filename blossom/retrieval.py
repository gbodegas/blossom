"""Retrieval: two mechanisms, and a router that picks between them on key presence.

The rule is that the method follows the question. A due date has a lookup key,
so it is fetched by exact query against structured state; retrieving it by
similarity would return the most similar assignment rather than the correct
one, and a confidently wrong deadline is worse here than no deadline. Support
rules and reflections have no key to look them up by, so they are found by
resemblance to the planning situation.

Two properties matter more than the mechanism.

Every result carries provenance. ``RetrievalResult`` records which store it
came from, which channel asserted it, when that assertion was observed, and
when it was retrieved. Those are separate timestamps on purpose: a record can
be accurate when observed and stale by the time it is read.

Retrieval is allowed to return nothing. ``NothingRetrieved`` is a value, not an
error, and it carries the reason. A vector search always returns its
highest-ranked result even when the corpus holds nothing relevant, so without
an explicit empty answer the agent would receive a rule for every plan and
apply it, producing plans that appear to carry the authority of an
accommodation without being supported by one.

Two known gaps, both in ``SemanticRetriever``:

``score = 1.0 - distance`` assumes a distance already normalised to the unit
interval. Chroma's default space is squared L2, which is unbounded, so the
score is not a similarity and ``min_score`` has no defined meaning until the
collection is created with an explicit metric.

``n_results=1`` retrieves only the nearest neighbour, so nothing can tell a
confident match from the only candidate. The design calls for three to five.
"""

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RetrievalQuery(BaseModel):
    """One question, carrying its own answer to how it should be answered.

    ``lookup_key`` is what the router switches on: present means exact,
    absent means semantic. ``min_score`` is the floor below which a semantic
    result is discarded rather than returned weakly.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    lookup_key: str | None = None
    min_score: float = 0.75


class RetrievalResult(BaseModel):
    """A retrieved fact, inseparable from where and when it came from.

    ``asserted_at`` is when the source claimed it. ``retrieved_at`` is when
    this system read it. Keeping both is what lets staleness be reasoned about
    rather than assumed away.
    """

    model_config = ConfigDict(extra="forbid")

    store_name: str
    record_id: str
    source_channel: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    asserted_at: datetime
    score: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NothingRetrieved(BaseModel):
    """A successful retrieval that found nothing worth returning.

    Deliberately a value rather than an exception or an empty list, and it
    carries ``reason`` so a caller can distinguish "the store was empty" from
    "the best candidate scored below the threshold".
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
    """The slice of the Chroma collection API this package depends on.

    Narrow on purpose: depending on the whole client would make the semantic
    retriever hard to substitute in a test, and this is the entire surface the
    retriever actually uses.
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
    """Approximate retrieval, with a threshold so it can decline to answer.

    See the module docstring for two known gaps in this class: the score is not
    a normalised similarity, and only the single nearest neighbour is fetched.
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
    """Chooses the retrieval mechanism from the shape of the question.

    The switch is key presence and nothing else. It is not a heuristic, a score
    or a model call, because a due date reaching the semantic path even
    occasionally is the specific failure this router exists to prevent.
    """

    def __init__(self, structured: Retriever, semantic: Retriever) -> None:
        self._structured = structured
        self._semantic = semantic

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Send keyed queries to the structured store and the rest to semantic."""
        if query.lookup_key is not None:
            return self._structured.retrieve(query)
        return self._semantic.retrieve(query)
