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

The gate is reached from `POST /parent/plans`, which runs the plan graph for
one evening, and resumed from `POST /parent/approvals/{draft_id}`, which
carries the decision back into the paused thread. Between the two, the draft
sits in the drafts table, `blossom/stores/drafts.py`, which is the record
across threads: what waits, what was approved, what was refused and why. The
graph writes it twice, once when the draft is composed and once after the
gate, each as an upsert keyed by a draft id derived from the thread, so a node
that runs twice leaves one row. The table lives in its own file, under the
same guard as saved state, with deleted rows overwritten. `GET
/parent/approvals` reads the table and needs no model, so a parent can always
see what is waiting. Starting a run needs the model seam and says so with a
503 when there is no key. Deciding does not: nothing past the gate asks a
model, so a graph built without a key can resume a paused thread and record
the decision, and the table is consulted before the graph is built, so a
draft that does not exist or is already decided is answered as such with or
without a key. Two decisions about one draft cannot
both land: the route holds one lock from the table check through the resume,
and the table refuses a second, different decision, keeping the first and its
time, which also covers a request from another process.

The page at `/parent` is the same three things as forms: a date to plan, the
drafts waiting with their text and two buttons, and what has been decided. Its
two form actions call the functions the JSON routes call and redirect back to
the page, so there is one way to start a run and one way to decide whichever
door it comes through, and a failure renders the page with the same status and
reason the API would have answered. The decision field admits exactly the two
button values. Without a key the page still reads and says why a plan cannot
start.

Under each draft the page shows how the plan was made: the run's step records,
one per node, each saying what the node expected and what it found. A run that
ended before the gate, because its plan never passed the checks or the model
did not answer, has no draft to show, so the page lists it under its own
heading with the same record, and the JSON answer to `POST /parent/plans`
carries the steps too. The route saves the record to the drafts file once the
run has stopped, whichever way it stopped, in two tables beside the drafts:
one row per run and one per step.

**Not built:** nothing yet lets her see that a draft was approved.

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

A source can contradict itself. A school portal shows one due date in a
day's header and another inline in the item's title, and both are claims from
the same channel. `SourceRecord.seen_in` names where in the source a claim was
read, so two records from one channel render as "LMS (day header)" and "LMS
(title)" rather than as the channel disagreeing with itself.

An assignment's shape follows the portal too. `due_date` may be absent: an
undated item still occupies the week, appears in every week until it has a
date, and is flagged on a plan rather than failing it. `assigned_on` is when
the work became available, a separate fact from when it is due, so an item
seen under both dates across two weeks is one assignment. `kind` separates
`HOMEWORK`, a sitting, from `TASK`, a form to sign or a book to cover, so the
planner is told what it is sizing.

**Not modeled:** the distinction between a record that is *stale* (accurate
when observed, since changed) and one that is *invalid* (accurate, but does not
support the conclusion drawn from it). A submission flag confirms a file was
uploaded, not that the work was finished. Staleness is answered by observing
again; validity is not. `SourceRecord.observed_at` exists so staleness can
eventually be reasoned about, but nothing reads it yet.

**Not built:** the adapter that reads a portal. `LMSSource` records the rules
it will follow and raises. The fixtures mirror the portal's shapes for a
fictional student: an undated task, assigned dates beside due dates, a source
that gives one item two dates, and the four confidence states on one page.

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

Nothing in the package calls the router today. The student's page and the
plan graph both read the week through `read_week` in `blossom/noticing.py`,
and the graph reads its other corpora whole: every support rule and every
reflection as they stand. Each is a few sentences about one student,
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

**Not wired:** no route or node constructs the router. `ProjectStateStore.lookup`
still answers the structured side for one that would.

## Four stores, four risk profiles

| Store | Contents | State |
|---|---|---|
| `ProjectStateStore` | Assignments: due and assigned dates, either possibly absent, kind, dependencies, reported submission status | Wired and tested; in memory |
| `SupportRulesStore` | Operational rules derived from her accommodations, one per chunk | Seeded from the fixtures; read whole by the plan graph |
| `ReflectionsStore` | The agent's notes about its own performance | Seeded from the fixtures; read whole by the plan graph |
| `DraftsStore` | Every draft that reached the gate, every decision about it, and every run's record of what each node expected and found | Wired and tested; a file at `BLOSSOM_DATABASE_PATH` |

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

Seven nodes, in this order. `retrieve` reads every assignment on record,
states each one's due date before it reads the sources, sets the two against
each other, and then selects the week: undated work, and dated work that the
record or any source puts in the window. `plan` asks the planner. `verify` runs the tier-one checks, and a plan that fails goes
back to `plan` with the findings before any critic sees it, because a
judgment about a plan that is already wrong is a wasted call. `critique` asks
the critic; fault sends the plan back with the critique, doubt sends it
forward. `compose` renders the plan, the doubtful due dates, and the
reviewer's notes as the text a parent reads, and saves it to the drafts table
as waiting, under an id derived from the thread. `require_human_approval` is
the gate from `blossom/agent/gates.py`, unchanged. `record_decision`, after
the gate, saves what the person decided; it is the only node past the gate,
and a node there may be added without a version bump.

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
the evening, the plan, what was found about it, the draft, and the record of
each step, and nothing that runs the process.

Each of the four nodes before `compose` appends one `StepRecord` from
`blossom/agent/steps.py` to the state's `steps` key, under the same reducer
`rounds` uses: the node's name, the planner round it belongs to, what it
expected before acting, what it found, and the household clock's time. The
words are built from the typed values, never from the model's prose: the
week's counts, the plan's shape, which checks failed and why, which criteria
the reviewer faulted or could not tell, and what a model call cost in tokens
when the answer carried it. The record exists because the final state cannot
say how a run got where it did: a passing check clears the findings that sent
the plan back, and an accepted verdict says nothing about the one before it.
Nothing reads the steps to decide what happens next.

**Not built:** the reviewer's five criteria are fixed in the prompt rather
than configurable.

## Expectation before action

An observation alone is data. Set against an expectation stated before the
look, it becomes confirmation or contradiction, and contradiction is the signal
the design most wants noticed: the family's record and the school disagree, and
nobody has been told.

`blossom/noticing.py` makes the expectation a value of its own.
`expect_due_date` builds one from the record alone, `notice_due_date` takes it
beside the source records and returns a `Noticing`, and the graph's `retrieve`
node calls them in that order, so the belief is committed before the sources
are read. The comparison is typed: each source value is read as an ISO date or
not read at all, and dates are compared with dates. There are three verdicts.
Confirmed means every readable source date is the record's. Contradicted means
at least one source gives a readable date and none gives the record's, which
includes a record with no date set against a source that has one. Undecidable
covers the rest: no sources, no readable dates, or sources that name the
record's date beside another. "Cannot tell" stays distinct from "these
disagree" because reading the undecidable as contradiction would bury the
signal under noise about formats and missing sources. No model takes part.

A contradiction changes three things. The tier-one deadline check measures a
contradicted assignment against the earliest date anyone gives, record or
source, so a plan cannot pass by trusting a record the school does not
support; the check holds deferrals to the same day, since putting work off
moves it to another day at the earliest. The week itself is selected after the
sources are read, so an item the record puts next month and a source puts this
week is planned for rather than never queried. The student's page reads the
week the same way, so the two cannot differ about what is in it, and an
assignment whose record date the sources contradict says so on her page beside
the date on record. The planner and the critic are shown the contradiction in its own
block, with an instruction to plan for the earliest date and to say the record
needs checking. The draft names it in a section of its own, so the person at
the gate sees what to correct.

`tests/noticing_cases.py` is a labeled table, balanced between contradictions
and confirmations with a separate group of undecidable rows, and
`tests/test_noticing.py` reports precision and recall for the contradicted
verdict as counts. The comparator is deterministic, so both are held at one.

The plan graph carries the same discipline into its own nodes. Each states
what it expects before it acts and records what it found, and the records are
saved with the run; the plan graph section says how.

**Not built:** only the due date is compared. The design's example is a
submission status the record holds and a portal can confirm or deny, and
nothing observes submission status yet. The step records are Blossom's own
account of a run; the framework's trace of every call beneath them, with its
inputs and outputs, is not kept anywhere.

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

`BLOSSOM_DATABASE_PATH` holds the drafts table, so the parent's queue survives
a restart. Project state itself is still in memory, because deciding when it
becomes durable is a design question rather than a wiring detail.

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
