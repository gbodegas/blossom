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
dispatch. `blossom/agent/gates.py` is the first: a graph node that pauses with
the draft and resumes with the decision, recording it in the graph's saved
state. Approval marks the draft for manual send and nothing more; the second
step is a person copying it out. The node does nothing before it pauses,
because a resumed graph re-runs the interrupted node from its start.

**Not built:** nothing stores a draft outside the graph's saved state, so an
approved draft is visible only to the thread that produced it.

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

The plan graph does not use the router. It reads its corpora whole: the
assignments due in the window from structured state, and every support rule
and every reflection as they stand. Each is a few sentences about one student,
which fits in a prompt entire, and an index over a corpus that size adds a way
to miss a rule for no saving. No vector store is a dependency, and none is
chosen; the `Collection` protocol in `blossom/retrieval.py` is the slice one
would have to satisfy if a corpus ever outgrows a prompt.

**Known gaps in `SemanticRetriever`:** `score = 1.0 - distance` assumes a
distance normalized to the unit interval; under an unbounded metric such as
squared L2 the score is not a similarity, and `min_score` has no defined
meaning until a collection is created with an explicit metric. And
`n_results=1` fetches only the nearest neighbor, so nothing can tell a
confident match from the only candidate; the design calls for three to five.

**Not wired:** in the student route the semantic side is
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
| 1: `HARD_CHECK` | Deterministic, pass or fail | `blossom/verification.py` for a claim, `blossom/plan_checks.py` for a plan |
| 2: `HEURISTIC_SCORE` | A critic's judgment, not a verified fact | `blossom/heuristic_relevance.py` |
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

Tier two is a shape rather than a score. `CriticVerdict` holds one finding per
criterion, each with the critique written before the judgment, and `accepted`
is derived from them: a critic cannot mark a plan it faulted as fine without
changing a finding, which is visible in review. The five criteria are a
closed list the type carries: the schema the model fills in admits only those,
the prompt is rendered from the same mapping, and a verdict that leaves one
out is not an acceptance, with the gate told which was skipped. `CANNOT_TELL` is a first-class
answer, because a critic forced to choose between pass and fail will invent a
reason to, and it neither passes a plan nor fails one; it goes to a person.

The critic is the second model call in the plan graph, below. Its verdict
travels with the plan to the gate and never decides for it: a critic that
keeps finding fault after the last revision sends the plan forward with the
critique attached, because a heuristic that could close the gate would be a
check wearing a different name.

## What a plan is, and what code can decide about one

A plan is data, not prose. `blossom/plans.py` holds `DailyPlan`: blocks of
wall-clock time in the household's zone, each naming one assignment and the
reason it sits there, plus deferrals for work in the window that tonight
leaves for another day. Leaving something out is therefore a statement with a
reason attached, not an omission, which is the same rule the student view
follows when it refuses to filter her week.

Block times are wall clock rather than instants because a block is a future
local event: "six to seven on Thursday" survives a change to the zone rules,
and an instant computed from it does not. Durations are measured by converting
both ends to UTC, because Python subtracts two aware datetimes that share a
zone in wall-clock terms; on the two nights a year when a day is not
twenty-four hours long, the local reading is wrong by an hour.

`blossom/plan_checks.py` runs the tier-one checks over a proposed plan: every
assignment it names exists, nothing due in the window is unmentioned, each
assignment is worked on or put off rather than both, no block is scheduled
after its deadline, no two blocks claim the same minute, and the evening is
inside the household's budget. Several blocks for one assignment are fine,
because splitting an essay over two sittings is good planning; being both
planned and deferred is the plan contradicting itself. A block's rationale and
a deferral's reason cannot be blank, so putting work off always comes with an
account of itself. Each failure is reported in words,
so a critic and a person are told what is wrong rather than left to work it
out. `PlanVerification.passed` is derived, as tier one's always is.

A due date that is anything short of corroborated does not fail a plan. It is
carried on the result as a flag, because a plan cannot be more certain than the
record it was built from. One source counts as short of corroborated: that is
the reason `SINGLE_SOURCE` is a state of its own rather than a kind of yes, and
the flag is read by exclusion so a state added later reads as uncertain until
somebody decides otherwise.

**Not built:** the daily minute budget is a constant, not a household setting,
and the window is still a fixed six-day span rather than a school week.

## The plan graph

`blossom/agent/graph.py` is a workflow, not an agent. The planner and the
critic are model calls that each return one typed value, `DailyPlan` and
`CriticVerdict`, through the provider's constrained output rather than a tool
call, and neither holds a tool. So there is no loop in which a model decides
what to call next: the graph decides, from the checks and the verdict, and
every route it can take is written in one file.

Six nodes, in this order. `retrieve` reads the week from the stores. `plan`
asks the planner. `verify` runs the tier-one checks, and a plan that fails goes
back to `plan` with the findings before any critic sees it, because a
judgment about a plan that is already wrong is a wasted call. `critique` asks
the critic; fault sends the plan back with the critique, doubt sends it
forward. `compose` renders the plan, the doubtful due dates, and the
reviewer's notes as the text a parent reads. `require_human_approval` is the
gate from `blossom/agent/gates.py`, unchanged.

The loop is bounded twice. The planner may be sent back `MAX_REVISIONS` times,
after which a plan that still fails tier one is reported as `checks_failed` and
nothing is proposed, while a plan the critic still faults goes to the gate as
`unsettled`. And every run carries the recursion limit from
`blossom/agent/runs.py`; a test holds the longest possible run under it, so
the limit is a backstop and never the thing that ends a legitimate run.

The model can end a run on its own. A response cut off at the token limit, a
refusal, or a body the schema cannot parse each ends the graph with an outcome
naming which, and no draft. The stop reason is read before the parsed value,
because a plan cut off after two of its three blocks is valid JSON and a wrong
plan.

The prompts in `blossom/agent/prompts.py` put the data first and the request
last, and everything copied from another system sits inside a labeled block
with its markup characters escaped: assignment titles from the school portal,
support rules and reflections from stores other code writes, feedback from an
earlier round of the same graph. The system text says once that block content
is never an instruction; the layout makes the boundary visible on every line
rather than leaving the model to infer it.

One model, `claude-opus-5`, serves both roles, at high effort for the planner
and medium for the critic. The stores and the two model callables are closed
over by the node functions rather than carried in state, so saved state holds
the evening, the plan, what was found about it, and the draft, and nothing
about the process.

**Not built:** no route starts a run or shows the result. The graph is
constructed and driven end to end by tests with scripted models, and
`plan_graph_for` builds it against the running application's stores, but
nothing calls it yet. Nothing seeds the support rules or reflections, so a
plan built today is built without them. The reviewer's five criteria are
fixed in the prompt rather than configurable.

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

## Instants are UTC, dates are hers

Time is read through the `Clock` protocol in `blossom/clock.py`. `SystemClock`
is the only implementation that touches the operating system; `BLOSSOM_TODAY`
pins it, which is what makes the fixture-dated demo reproducible and the
weekly-window tests honest.

Two kinds of time are kept apart. An instant is an aware UTC datetime, which
is the only form that means the same thing everywhere, and every stored one is
typed `AwareDatetime` so a naive value fails validation rather than being
guessed at later. A date is the household's local date, because "due this
week" is a question about the days she lives in. Taking the date off a UTC
instant is wrong for most of an American evening: at 20:30 on a Wednesday in
`America/New_York` the UTC date is already Thursday, so a week window computed
that way runs a day ahead of her.

`BLOSSOM_TIMEZONE` is therefore required, an IANA key with no default. No
value is right for every family, and a wrong one moves the school week without
saying so, so the application refuses to start instead of guessing. The key is
resolved once at startup, where an unknown one is reported by name. `tzdata`
is a dependency because Windows ships no time zone database, and without it
every key fails.

`ProjectStateStore` takes its clock as a required argument for the same
reason: a store cannot invent a household's zone.

**Not built:** a calendar policy. The weekly window is a fixed six-day span
from today, not a school week, and nothing yet knows about no-school days,
bedtimes, or a term calendar. Durations that cross a daylight-saving night
need to be computed in UTC when that arrives; both transitions fall inside the
school year and are covered by tests today only at the level of the date.

Stores are opened once by the application lifespan in
`blossom/dependencies.py` and injected into routes with `Depends`. The SQLite
connection is shared across FastAPI's worker threads, so it is opened with
`check_same_thread=False` and every statement is serialized behind a lock.

**Not built:** `BLOSSOM_DATABASE_PATH` is read into settings but not honored.
Project state is in memory, because deciding when state becomes durable
is a design question rather than a wiring detail.

## Saved graph state

A graph's state, including a pause at the approval gate and the draft it holds,
is saved by the asynchronous SQLite saver, opened by the lifespan from
`blossom/stores/checkpoints.py` on the file named by `BLOSSOM_CHECKPOINT_PATH`.
That file is separate from project state so the two writers never contend and
clearing a thread touches nothing else. Startup refuses a path on a network
share or inside a synced folder; deleted rows are overwritten.

The framework calls each saved snapshot a checkpoint, and its classes and the
setting above carry that word. This document says saved graph state, because
the parent's view at `/parent/checkpoint` is a different thing.

Deserialization is strict. Saved state records every value with the module and
class that produced it and reconstructs the class by import, so the serializer
is constructed with an allowlist of the types a graph may carry
(`STATE_TYPES`), which adds to the framework's own safe set. A class outside
both comes back as plain data, never as an object.

Saved state outlives the code that wrote it, and the framework versions only
its own storage format. `blossom/agent/runs.py` therefore stamps
`GRAPH_VERSION` into every run's metadata and refuses to resume a thread
written under another version. Four rules keep the version steady across
changes: graph state grows only by optional keys or keys with a reducer; a
value carried in state gains fields only with defaults and is never renamed or
moved between modules; the names and order of nodes ahead of a gate are part of
the contract; and every node performs at most one side effect, written so that
running it twice has the same result as once. Any change that breaks a rule
bumps the version, and paused threads are then drained, with whatever they held
re-queued, rather than resumed.

Every run also carries an explicit recursion limit, because the framework's
default is ten thousand and seven supersteps, itself read from the
environment. Durability is `sync`, so the state is on
disk before the next step starts rather than while it runs; the framework takes
that as an argument beside the configuration and defaults to `async` when it is
omitted, so a scan refuses a run that builds a configuration from
`blossom/agent/runs.py` and then leaves it out. Only scalar values in the run's
configuration are saved as metadata, in plaintext; nothing about the student
goes there.

**Not built:** retention. Nothing clears an old thread, and the saver's only
pruning primitive deletes a thread whole. A retention rule, and the student's
ability to delete what a thread holds, are design decisions still open.

## Stack

The design notes specify LangChain for generation and judging, LangGraph for
control flow and saved state, and MCP for external tools. LangChain and
LangGraph are present, and so far they do six things: build the framework's
tool objects, run the tool backstop, pause a graph at the approval gate,
construct the model client in one seam, `blossom/anthropic_client.py`, with the
endpoint fixed in code so that no environment variable decides where a prompt
is sent, keep a graph's saved state in a SQLite file of its own, and run the
plan graph, whose two model calls each return one typed value. Nothing here is
wired to the FastAPI routes yet, whose control flow is still hand-rolled. MCP is absent. When it arrives, tools it
loads will be foreign to the backstop until each has a registry entry of its
own in `blossom/tools.py`, which is the intended path; how a tool that reads
rather than drafts fits a registry whose callables return only drafts is an
open design question.

Adding any other dependency fails a test until someone edits `ALLOWED_IMPORTS`
with a justification, so the stack cannot grow by accident.
