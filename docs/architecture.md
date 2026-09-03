# Blossom architecture

This describes what the code does today. Where a design commitment is not yet
built, that is stated.

The reasoning behind these decisions lives in a set of design notes kept
outside this repository.

## Three principals, three view models

The student, parent, and verifier each see a different projection of the same
planning state. They are separate route trees under `blossom/routes/` with
separate Pydantic view models in `blossom/views.py`, rather than one view with
a role flag on it. Every view model sets `extra="forbid"`, so a field that does
not belong in a projection fails validation instead of leaking into it.

**Not built:** the design calls for a visibility policy sitting between the
shared state and both agents, such that neither can read a store directly and
each receives only what the policy permits. What exists is a convention
enforced at serialization time. It makes an accidental leak hard; it would not
stop a future route that reads a store and renders whatever it likes. This is
the largest gap between the design and the code.

**Not built:** if the system notifies a parent, she is meant to be able to see
that the notification happened. Nothing implements that.

## There is no sending path

Outbound work terminates in a draft that a human transmits by hand. This is
enforced three ways rather than by instruction:

- `ToolCallable` in `blossom/tools.py` returns `Draft` and nothing else, so the
  registry structurally cannot hold a tool that returns the result of having
  transmitted something.
- `ALLOWED_CAPABILITIES` is an allowlist, validated at import rather than only
  under test, so a tool declaring an unanticipated capability fails at load.
- `tests/test_capability_boundaries.py` walks the package with `ast` and
  asserts every imported module is on a justified allowlist, and that each
  network-capable dependency is confined to one file. A third list closes
  paths inside admitted packages that no file may import: the hosted tracer,
  the context manager that enables it, and the remote graph client.

The claim is not that nothing reaches the network; calling a model
necessarily would. It is that the ability lives in one named seam per
dependency instead of anywhere a future route reaches for it.

The model framework brings its own tool abstraction, whose tools return
anything, so the guarantee is carried across it in two layers rather than
trusted to convention:

- Construction. `as_langchain_tool` in `blossom/tools.py` is the only tool
  constructor this package provides, and it builds only for an entry of the
  registry, checked by identity, so a spec assembled anywhere else is refused
  whatever capability it claims. It remembers every object it builds, and at
  runtime it accepts nothing but a `Draft` back from the callable. What a
  registered callable does before it returns is bounded by review of the
  registry and by the import allowlist, not by this function. A test confines
  the framework's direct tool constructors, and its tool node, to named files.
- Runtime. `blossom/agent/boundary.py` is middleware with two hooks. Before a
  model call, it refuses to bind any tool the constructor did not build, by
  identity rather than name, which also catches a provider-executed tool passed
  as a dictionary. Before a tool call, it refuses any tool object it does not
  recognize, without invoking it, and a foreign tool that copies a registered
  name is refused the same way. It exists for tools that reach an agent by a
  path construction never saw: a loader for an external server's tools, a
  prebuilt agent's own tools. A test confines the middleware constructor to
  that file. Nothing attaches this middleware to an agent yet.

Anything that would leave the family takes two human steps, review and
dispatch. `blossom/agent/gates.py` is the first: a graph node that pauses
with the draft and resumes with the decision, recording it in checkpointed
state. Approval marks the draft for manual send and nothing more; the second
step is a person copying it out. The node does nothing before it pauses,
because a resumed graph re-runs the interrupted node from its start.

**Not built:** nothing stores a draft outside a graph's checkpointed state, so
an approved draft is visible only to the thread that produced it.

## Sources disagree, and that is the interesting case

Assignment state is assembled from several imperfect channels rather than
retrieved from an authoritative record. `blossom/reconciliation.py` returns one
of three outcomes and never picks a winner:

| Outcome | Meaning |
|---|---|
| `Agreement` | Every channel that spoke asserted the same value. Contributing records are kept, so one agreeing channel stays distinguishable from four. |
| `Disagreement` | Channels conflict. Every claim is preserved with the channel that made it. |
| `NoSourceRecords` | Nothing corroborates this fact at all. |

`SourceConfidence` maps those onto what the student sees: `CORROBORATED`,
`SINGLE_SOURCE`, `SOURCES_DISAGREE`, `UNVERIFIED`. Four states rather than a
boolean, because a date two channels agree on is not the same claim as the
identical date from one channel alone.

Nothing filters her week. Every assignment in the window reaches the page
carrying its confidence, and every card states its confidence even when the
date is well corroborated. A marker that appears only when something is wrong
trains a reader to skim past its absence.

**Not modeled:** the distinction between a record that is *stale* (accurate
when observed, since changed) and one that is *invalid* (accurate, but does not
support the conclusion drawn from it). A submission flag confirms a file was
uploaded, not that the work was finished. Staleness is answered by observing
again; validity is not. `SourceRecord.observed_at` exists so staleness can
eventually be reasoned about, but nothing reads it yet.

## Retrieval routes on key presence

`RetrievalRouter` switches on one thing: whether the query has a lookup key.
Keyed queries go to structured SQLite; unkeyed queries go to semantic search.
The switch is not a heuristic, a score, or a model call, because a due date
reaching the semantic path even occasionally is the failure this router exists
to prevent.

Retrieval may return nothing. `NothingRetrieved` is a value carrying a reason,
not an exception, because a vector search always returns its highest-ranked
result even when the corpus holds nothing relevant.

Every result carries provenance: which store, which channel, when the source
asserted it, and when this system read it.

**Known gaps in `SemanticRetriever`:** `score = 1.0 - distance` assumes a
distance normalized to the unit interval, but Chroma's default space is squared
L2, which is unbounded, so the score is not a similarity, and `min_score` has
no defined meaning until the collection is created with an explicit metric.
And `n_results=1` fetches only the nearest neighbor, so nothing can tell a
confident match from the only candidate; the design calls for three to five.

**Not wired:** in the running system the semantic side is
`EmptySemanticCollection`, a stub that always returns no candidates. The
structured side is real.

## Three stores, three risk profiles

| Store | Contents | State |
|---|---|---|
| `ProjectStateStore` | Assignments, dates, dependencies, reported submission status | Wired and tested |
| `SupportRulesStore` | Operational rules derived from her accommodations, one per chunk | Not wired, not tested |
| `ReflectionsStore` | The agent's notes about its own performance | Tested, not wired |

They are separate because their retention and access rules differ, not for
tidiness. `ReflectionsStore.write` refuses any subject other than `SYSTEM`, so
the store cannot become a diary about the student. The boundary is structural
rather than a matter of prompt wording.

**Not built:** reflections are meant to be readable, correctable, and deletable
by her, as a visible part of the interface. There is no such path. Retrieval is
also meant to weigh a reflection's age; `observed_at` is recorded but unread.

## Verification has three tiers, and only one is automatable

| Tier | What it is | Where it lives |
|---|---|---|
| 1: `HARD_CHECK` | Deterministic, pass or fail | `blossom/verification.py` |
| 2: `HEURISTIC_SCORE` | A critic's estimate, not a verified fact | `blossom/heuristic_relevance.py` |
| 3: `HER_JUDGMENT` | Whether a plan is right for her | Nowhere, by design |

Keeping them apart is what stops the system claiming more confidence than its
evidence supports. Tier three has no implementation because no automated check
can answer it, and her workload signal settles it directly rather than being
weighed against anything the system computed.

`VerificationResult.passed` is a derived property, not a field. There is no
attribute to assign, so nothing can flip a failed verification to passed. A
result missing any check does not pass; partial evidence is not a weaker yes.
`CheckOutcome.NOT_IMPLEMENTED` is distinct from `PASSED`, so an unwritten check
cannot be mistaken for a passing one.

**Consequence:** `POLICY_CONFORMANCE` reports `NOT_IMPLEMENTED`, so
`verify_reconciled_fact` cannot return a passing result. The policy check needs
the drafts-and-approval rules, which do not exist.

**Not built:** tier two is `min(len(text) / 100, 1.0)`. It is a placeholder,
uncalled and untested.

## Expectation before action

`AgentStep` requires a non-blank expectation as a keyword argument, so a step
cannot be recorded without stating what it expected to find. An observation
alone is data; compared against an expectation it becomes confirmation or
contradiction.

**Not built:** `compare_expectation_to_observation` tests whether the expectation is a
substring of the observation, which is not equivalence. In the one place it
runs, the expectation is a lookup key and the observation is the record id the
store echoes back, so it compares a string with itself and can never register a
contradiction. The result is discarded; only `expectation` is read.

Making this real needs a typed expectation, so a claim about a value is checked
deterministically, and a three-way verdict, so "cannot tell" stays distinct
from "these disagree". Defaulting an undecidable comparison to contradiction
would flood the one signal the system most needs to keep clean.

**Not built:** nothing persists a step. The checkable trace the design promises
does not exist.

## The workload signal

`POST /student/workload-signals` takes no body. It does not ask her to rate or
describe anything: assigning a rating requires stepping back and assessing, and
that capacity is least available exactly when the signal matters most.

**Not built:** the signal is accepted and discarded. Nothing stores it, nothing
reduces a plan in response, and the response reports only receipt. The design
requires it to produce an immediate visible result, since a control that
changes nothing observable gets abandoned. It also requires brief retention,
visibility to her, and deletion by her. None of that exists.

## Configuration, time, and lifecycle

Every filesystem path is resolved through `blossom/settings.py`, with relative
values resolved against the repository root so they mean one fixed location
regardless of the working directory. Packaged assets are not configurable.

Time is read through the `Clock` protocol in `blossom/clock.py`. `SystemClock`
is the only implementation that touches the operating system; `BLOSSOM_TODAY`
pins it, which is what makes the fixture-dated demo reproducible and the
weekly-window tests honest.

Stores are opened once by the application lifespan in
`blossom/dependencies.py` and injected into routes with `Depends`. The SQLite
connection is shared across FastAPI's worker threads, so it is opened with
`check_same_thread=False` and every statement is serialized behind a lock.

**Not built:** `BLOSSOM_DATABASE_PATH` is read into settings but not honored.
Project state is in memory, because deciding when state becomes durable
is a design question rather than a wiring detail.

## Stack

The design notes specify LangChain for generation and judging, LangGraph for
control flow and checkpointed state, and MCP for external tools. LangChain and
LangGraph are present, and so far they do four things: build the framework's
tool objects, run the tool backstop, pause a graph at the approval gate, and
construct the model client in one seam, `blossom/anthropic_client.py`, with the
endpoint fixed in code so that no environment variable decides where a prompt
is sent. No model is called yet, and nothing here is wired to the FastAPI routes, whose
control flow is still hand-rolled. MCP is absent. When it arrives, tools it
loads will be foreign to the backstop until each has a registry entry of its
own in `blossom/tools.py`, which is the intended path; how a tool that reads
rather than drafts fits a registry whose callables return only drafts is an
open design question.

Adding any other dependency fails a test until someone edits `ALLOWED_IMPORTS`
with a justification, so the stack cannot grow by accident.
