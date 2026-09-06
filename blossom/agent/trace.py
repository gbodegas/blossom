"""A tracer that keeps the framework's run tree on this machine and nowhere else.

``BaseTracer`` is the framework's own callback base. It assembles a tree of
runs, one for the graph, one per node, one per model call, and hands the
finished tree to ``_persist_run`` once the root ends. ``LocalRunTracer`` writes
that tree to the trace store after passing every input, output, and error
through a redaction hook. The hook is a plain function from text to text. The
default keeps the text as it is; a household that wants names or dates kept
out of the file replaces it in one place.

This is the one module allowed to import from the framework's tracer package.
The hosted tracer lives in the same package, and the boundary scan opens
exactly two paths here and nothing else.
"""

import json
import logging
from collections.abc import Callable

from langchain_core.tracers.base import BaseTracer
from langchain_core.tracers.schemas import Run

from blossom.agent.runs import THREAD_ID_KEY
from blossom.stores.traces import TracedRun, TraceStore

Redactor = Callable[[str], str]
"""Text in, text out, applied to every input, output, and error before it is written."""


def unredacted(text: str) -> str:
    """The default hook: the text as it is."""
    return text


logger = logging.getLogger(__name__)


class LocalRunTracer(BaseTracer):
    """Writes each finished run tree to the trace store, redacted, and sweeps old ones."""

    def __init__(self, store: TraceStore, *, redact: Redactor = unredacted) -> None:
        super().__init__()
        self._store = store
        self._redact = redact

    def _persist_run(self, run: Run) -> None:
        """Called by the framework once per root run, with the whole tree beneath it.

        The base class remembers every run's place in its tree for as long as
        the tracer lives and forgets nothing of that on its own, so a tracer
        kept for the life of the process would grow with every run. Once a
        tree is written, its ids are dropped from that map. The map of live
        runs is the base class's own: it removes each run as it ends, the root
        included, right after this returns, so nothing is popped from it here.
        """
        try:
            self._store.record(traced(run, self._redact))
            self._store.sweep()
        except Exception:
            # The trace is a record for looking into a run, never a condition on
            # it. A store that cannot take the tree is reported to the process
            # log, and the tracer forgets the tree all the same, so a broken
            # store does not turn into a tracer that grows with every run.
            logger.exception("the trace of run %s could not be kept", run.id)
        finally:
            for finished in tree(run):
                self.order_map.pop(finished.id, None)


def traced(run: Run, redact: Redactor) -> TracedRun:
    """The framework's run as the store's record, every text passed through ``redact``."""
    metadata = (run.extra or {}).get("metadata") or {}
    thread_id = metadata.get(THREAD_ID_KEY)
    return TracedRun(
        run_id=str(run.id),
        trace_id=str(run.trace_id or run.id),
        parent_run_id=None if run.parent_run_id is None else str(run.parent_run_id),
        thread_id=None if thread_id is None else str(thread_id),
        name=run.name,
        run_type=run.run_type,
        started_at=run.start_time,
        ended_at=run.end_time,
        inputs=redact(as_json(run.inputs)),
        outputs=None if run.outputs is None else redact(as_json(run.outputs)),
        error=None if run.error is None else redact(run.error),
        children=tuple(traced(child, redact) for child in run.child_runs),
    )


def tree(run: Run) -> list[Run]:
    """A run and everything beneath it, parents first."""
    runs = [run]
    for child in run.child_runs:
        runs.extend(tree(child))
    return runs


def as_json(value: object) -> str:
    """Serialize what the framework recorded, pydantic values by their fields, the rest by name.

    Characters stay as they are rather than becoming escapes, so a redaction
    hook written against a name sees the name.
    """
    return json.dumps(value, default=plain, sort_keys=True, ensure_ascii=False)


def plain(value: object) -> object:
    """What ``json`` cannot serialize on its own: a model's fields, or its text."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)
