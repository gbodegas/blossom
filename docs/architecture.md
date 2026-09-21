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

At the edge, `blossom/household.py` is a gate on the route trees: with two
passphrases set, hers and a parent's, a request answers only someone who has
signed in and may open that tree. Her passphrase opens the student tree; a
parent's opens all three. A sign-in is a cookie signed with a keyed hash from
the standard library under a key per person, drawn from a secret kept beside
the database and that person's passphrase, so nothing here needs a library
or a service, a cookie made elsewhere is refused, and a changed passphrase
signs that person out. Wrong passphrases are counted per device and answered
with a wait past ten. With neither passphrase set the gate stands open, which
the tests and the sample rely on. Open or not, the gate turns away any request
that would change something and does not name this server as where it came
from, read from the browser's Origin header or, failing that, its Referer,
against the address the request was sent to: a page elsewhere cannot make a
signed-in browser send a form here. That check comes before the sign-in and
the open routes, and the tests' client sends the header with every request.

Beside the gate, `blossom/intake.py` is the way in for assignments: a reader
for the school portal's own text, its homework page, its weekly summary, and
its "Missing" email, pasted by a parent, and for one assignment typed by hand.
It reads line shapes, not a model, in one pass over the lines: into
assignments, into each channel's claim about a due date with where the claim
was read, so the reconciliation treats a pasted page as it treats a fixture,
into the teacher's note under a card, and into what the school reports about
an assignment's status, a fact of its own kept with who reported it and the
day. A line shaped like a card that does not read as one is text for review,
never a teacher's words. The reader writes nothing. `blossom/routes/inbox.py`
compares what was read with the record as it is, row by row, matched by
course and title whatever the row's id, and shows the result week by week
with what saving would do; the saving compares again and writes under the
store's lock, in one transaction, so the same text saved twice, from two
tabs, adds nothing twice, an answer of new work included, since the row
that answer makes is found by its id. That is the import's doing, not a
constraint on the claims table: every observation a channel makes is kept,
a card repeated in one text is one observation, and a household file from
before this schema is brought up to it by adding columns, by dropping
the one index a version between made on the claims, and by folding status
reports held twice for one day to the first. A recorded due date is replaced by a
paste only when a parent says the assignment moved; each fact on a row carries
its origin, the portal, the email, or a parent, so the pages can say whose
it is, each field with the channel that gave it when a text mixes the email
and the portal. A note is the teacher's, a parent's, or hers, and the record
says which in one place, `Assignment.note_by`: marked as a parent's entry it
is a parent's, marked as her report it is hers, and any other mark or none is
the school's, since every note kept before notes carried a mark came from the
school's card. The pages say it to whoever reads, and both models are told
it, hers as `student_noted`: her account of work she added, never the
teacher's instruction. A plan carries a fingerprint of the week it was made
from, the reported status and whose words each note is included, and a
waiting plan whose week reads differently is stale on both pages and refused
at approval; a decided plan is history. With no note there is no hand to
name, so a mark alone changes no fingerprint. The fingerprint is drawn from a
fixed namespace, one for each shape it has had; a plan that was waiting when
the shape changed reads as changed once and is asked for again, and nothing
asks a model for it. Saving and deciding share the decision lock, so a decision is
checked against a week that holds still until it lands.

Her own word about her work is the third account, beside the school's and the
record's dates, and `blossom/assignment_status.py` reads them apart. A report
is Done, meaning she has finished her part, or Not yet, with a note if she
wants one; the store keeps each as an event in a chain per assignment, with
the status and note that stand after it, the day it was made, and the event
before it, so what stands now is the chain's head and an undo is one more
event that restores what stood before. The report whose words stand is found
by walking back from the head through every undo, however many, so a restored
report keeps its own day; and the store refuses an event that is not whole,
an undo that is first, takes back anything but the report at the head, or
restores something other than what stood before it, the sample's seed
included. A save carries the head the page showed, and the form is read whole
first: its own fields, each once, and an update it names must be one of that
assignment's events, or nothing is compared or written. Then the same update
as the one standing is already saved and writes nothing, a page whose head
has moved on is refused with the newer update shown, and anything else is
appended, one operation under the decision lock and the store's. A write the
file refuses is rolled back whole, and the page comes back with her words and
no word of a save. The pages read her events, the school's reports, and the
family's checks in three batched reads whatever the number of cards, each a
statement written whole with the names bound as one value: each card shows
what every school channel says now, not one latest report, and folds her
history under it, her updates and corrections with their days and the
school's reports apart. Her Done decides one thing, whether the assignment is
still work to plan: the planner, the critic, and the checks see only the work
that is left, a check of its own fails a plan that speaks about finished
work, and a window with nothing left ends a run before any model is asked. A
Done beside a school report of Missing is something for the family to check,
said on both pages and decided by neither. The form is a form alone, a parent
signed in reads her update and cannot make one, and nothing writes her
account but her own device. Where her Done stands beside a Missing, a parent
can mark the pair checked on the family page, with a note her card shows. The
check is a chain of the family's own events in a table of its own, made
against a basis worked out from her events and the school's reports, the
report that began her Done and each current statement of Missing, and
compared again under the decision lock and the store's, the writer reserved
first, before it is written; a row whose facts or record moved is refused
with what stands now. A check another parent made with other words is refused
the same way, both notes shown. Check again reopens it, held to the basis its
page showed. A check changes neither account, no plan, and no digest.

What she says about turning work in is a fourth account. Its forms are on an
assignment's details and nowhere else, in a section after her update,
`student_hand_in.html`, through two routes of their own in
`blossom/routes/hand_in.py` that follow her work update's: the form read
whole, who may save decided from the sign-in, one operation under the
decision lock, a redirect to a result the address lands on, and a refusal
that keeps what she chose and wrote, a moved head shown with what stands
above a form headed as not saved. An Undo already taken back is said to be
that, from the head the refusing save read, and her work update's Undo says
the same. `blossom/to_turn_in.py` draws her To turn in list from the page's
one reading, with no read of its own: every assignment she reports as still
to turn in, whatever her work update says and whatever week it is due in,
ordered by the event that began that state as the file ordered it, so an
edit does not move a row, coming back to the state puts it last, and an undo
puts it back where it was; a record that cannot be read is named under the
list and never dropped from it. The list is a page of its own,
`/student/to-turn-in`, there when empty, reached from a link under the main
controls that carries a count only when there is one, and its first rows
are on her week after Today. A row's one press is the same save as the
details' form, turned in with the head the row showed and no words, and a
checked field says where its result is shown: on the list or on her week,
at a place that is there whether or not the row still is. A form that says
it is the list's is held to the list's own shape, every field, no other, no
way back, each value exactly as the page wrote it, and for the press that
one state with no words; anything else is refused whole. The store holds
the list's forms to what stands, in the transaction that would write: the
press is written only over still to turn in, and the Undo takes back only her
report that it was turned in, so a form in the list's shape that names
anything else writes nothing. Her routes that
carry an assignment's id take it as the rest of the path up to their own
ending, so an id that holds a slash is reached by the addresses made for it.
A place a redirect lands on is named by one helper, used by the page that
writes the id and by the address sent to it, and her week with a card in view
is one address builder that escapes the id in the query and in the fragment,
so the two agree whatever an id holds. A card or a row on her week writes its
id with one helper too, the assignment's id escaped, and every fragment that
names it is made by that helper, so two ids that differ only by an escape
are two places. The redirect names the event the save accepted, and the page
that answers looks it up in that assignment's history in its one reading:
while that event is the latest the result is what stands, said with the
dated statement that stands, with an Undo for that event and no other, and a
press already saved may name an undo that put her report back, which offers
none; once something newer follows, it is said as
something done earlier beside what stands now, with no Undo; an id the
history does not hold says nothing, and an assignment that has left the
record is told to be gone only when her events for it, which stay in the file
and are read in the same batch, bear the address out. A refusal carries what was tried and on
which assignment, and the page shows that assignment as it stands now beside
the refusal, on the list or off it, with the press again only while it is
still to turn in. A write the file refuses, when the list cannot be read
back either, is answered by the plain page that reads no store, whose one
sentence takes the focus as the page arrives. The way back
from the details gains a fourth place, the list. Help
remembering is a GET that opens the form with Still to turn in chosen and
writes nothing. Her week's rows show one line and a link, the family page lists
what is still to turn in, however old, then what else she said in the last
fourteen days, with no form, and a row worth checking together carries it as
context that decides nothing. A page's one reading holds every chain, read
once through `hand_in_readings`, which takes the rows one assignment at a
time: a row that cannot be decoded, or a chain that does not hold, makes
that assignment's record unreadable, named apart, said on the page as one
that cannot be read and never shown as nothing recorded, while every other
assignment reads as usual and no page fails for it. The writers do not come
that way: they read one chain strictly and write nothing on a bad one. A save
refused over such a record has no form to return to, so what she chose and
wrote is shown as sent, read-only. A refusal about no one field is said
first on the details and takes the focus there, with a link down to the
section; a save and an undo read the clock once and draw the day from that
moment.
`blossom/hand_in.py` has the
event and the reading of a chain, and the store keeps the chain in
`hand_in_events` the way it keeps her work reports: the writer reserved
before the head is read, the same words already saved whatever head the page
held, a moved head refused with the head as that transaction read it, and an
undo that restores what the chain says stood before, a repeat refused like
any other. A head that looks whole proves nothing about what is behind it,
so every save and undo first reads that assignment's whole chain, inside the
transaction that writes, and holds it to the chain's rules by the same pass
every reader uses: each event follows the one before it and is that
assignment's, no id comes twice, and an undo takes back the report just
before it and carries exactly what stood before that. A chain that does not
hold is unavailable, which is not the same as saying nothing: nothing is
called already saved, stale, or new on it, nothing is written or repaired,
and a reader is to say the record cannot be read, never that she has
reported nothing. The table and its index are made in one transaction, so a
file from before gains both or neither. Still to turn in, turned in, nothing to turn in, and not sure are
the states; not sure is a report, and nothing said is the absence of one. One
next action is kept only with still to turn in. Three things are read from a
chain and kept apart: the head a save is compared with, the event that began
the state standing now, whose day a page will say and which an edit inside
the state does not move, and the event the words came from, so a note changed
on a later day carries that day. Events are ordered by a number the file
gives and named by an id. The words in these fields go through
`blossom/authored_text.py`: a control character or half a character is
refused where it sits, a joiner inside an emoji and the replacement character
are kept, one line holds no break, length is counted in code points, and
nothing is ever taken out of what a person wrote. The notes her updates and
the family's checks already keep are read by their own rule. Her work
reports, the school's reports, the family's checks, and the plan fingerprint
never read this account, and it never changes them.

A plan is saved twice over from one composition: the text both readers see,
and a snapshot beside it, `blossom/plan_snapshot.py`, one versioned JSON
document with the plan as the planner returned it, the title, course, and due
date of each assignment it speaks about as the run read them, and the
sentences composed around it. `compose` words each sentence once and derives
both from that wording, inside the node that already saved the draft, so the
snapshot adds no key to the graph's state, no type to the checkpoint
allowlist, and no graph version: a run paused before this resumes as it
always did and keeps its text-only draft. The drafts table keeps the snapshot
in a nullable column, written in one transaction with the text, the ids, the
fingerprint, and the run; once a draft is on the pages its bundle is the
record, so the same composition saved again changes nothing and a different
one is refused. `blossom/plan_reading.py` reads a draft one record at a time:
a version-1 snapshot that is whole, every key it writes present and every
block time a wall time, and that agrees with its draft is shown by its rows,
no snapshot is an earlier plan shown as its text, and a snapshot that cannot
be used is shown as its text with a sentence saying so, the draft id logged
with which part of the envelope failed and nothing of what it says, no
assignment's id among it. The snapshot is decoded and checked in one step by
the model's own JSON reader, so text no page could send, half of a surrogate
pair written as an escape, makes that one snapshot unavailable and never an
error while a page is being sent. Nothing reads the text to find an
assignment, and nothing repairs a draft on a GET.

Which plan is current is decided apart from its review: the household day is
read once for a page and handed to everything on it, the heading, the week,
the notice, the marks, and the stale check, so a page rendered across
midnight is about one day. The family page reads its drafts once too, what
waits, what was decided, and which draft is the day's, in one statement under
the store's lock, so a plan published or decided while the page is being
built cannot leave it naming a draft it did not read. Today's working plan is
the last draft published for that day that no later one displaced, waiting,
approved, or refused. That one reading shows, beside each row, that she
reports its assignment as Done, from the page's one reading of the record.
`read_everything` in `blossom/noticing.py` reads every assignment, the claims
about each, and what she, the school, and the family have said, in one hold
of the store, with today's plan's assignments named to it; the week, the
planning window, the notice above the plan, the marks beside its rows,
whether a waiting plan still fits the week, and the family page's assignment
updates all come out of that reading, so they agree and her reports are read
once for a page, however many plans it shows. The hold is the store's lock
and, inside it, one read transaction, `ProjectStateStore.reading()`: the lock
keeps this process's other callers out, and the transaction keeps the file as
it was at the first read, since the drafts, her signals, and her requests
write the same file through connections of their own. The reading is at most
six read statements however much the record holds, between the statement
that begins the transaction and the one that ends it: the claims and her
hand-in events are each asked for once and not once per assignment, and a
reader asked about no assignments runs none, so an empty record costs two. Her
week and the family page read her homework notes beside the record, inside that
same transaction: one statement more however many notes there are, and one
again, only when a request for help is about a note, for every such note at
once. The notes are no part of the reading a plan, a digest, or a brief is
made from, so none of those can hold one; a note added to homework reaches
them only as the assignment it became. It ends before
anything is rendered; while it lasts, another connection's commit waits, a
few reads long. The file stays in rollback-journal mode with the default
wait. The rule is only that the
assignment's effective status is Done now; nothing is compared with when the
plan was made, a parent's check, or the school's word. An earlier plan for
today, a plan for another day, and the text as composed carry no marks.
Showing a plan writes nothing, and a mark is never a new approval: the stale
checks and their refusals are as they were.

An assignment's details, `/student/assignments/{id}`, read the assignment by
id, never through her week, and show the card's own facts and the one update
component, handed a context object that says who may update and where its
forms and links go. The details' evidence lists every claim a source has made
about the date, the ones that agree included, where a card lists claims only
when the date is in doubt. The two report routes serve cards and details
alike; a form says which it came from, and that decides only where its result
is shown. The way back is data, three checked fields, and every address is
made on the server from values escaped where they go, so no page sends a
reader to an address a request handed in. Her links to today's plan land on a
place with an id of its own, the same whichever plan it holds, since a link
is followed later than it is written: another plan may have taken that one's
place, and on a day with no plan the place is still there and says so. A
saved plan keeps the id made from its draft, which the family page and its
history link to.

**Not built:** the design calls for a visibility policy sitting between the
shared state and both agents, such that neither can read a store directly and
each receives only what the policy permits. What exists is the gate at the
edge and a convention enforced at serialization time. Together they make an
accidental leak hard; they would not stop a future route that reads a store
and renders whatever it likes. Also not built: encryption on the wire, since
the home network carries plain HTTP, and any account beyond the two
passphrases.

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
dispatch. `blossom/agent/gates.py` is the node for the first: it pauses with a
draft and resumes with a decision, recording it in the graph's saved state.
For a draft meant to leave, a note to a teacher say, approval marks it for
manual send and nothing more, and the second step is a person copying it out;
no such draft exists yet. The node does nothing before it pauses, because a
resumed graph re-runs the interrupted node from its start.

The evening plan uses the same node for something else: a pause for a review
she does not wait for. The plan never leaves the household, and it is on her
page and hers to use from the moment her run pauses; what the parent records
through the gate is a review, shown under the plan, not permission for it.
The plan graph runs from `POST /student/plans` and `POST /parent/plans`, her
page's door and the parent's, both through `blossom/routes/runs.py`, and the
pause is resumed from `POST /parent/approvals/{draft_id}`, which carries the
review back into the thread. Between the two, the draft sits in the drafts
table, `blossom/stores/drafts.py`, which is the record across threads: what
waits, what a parent said and why. The graph writes it twice, once when the
draft is composed and once after the gate, each as an upsert keyed by a draft
id derived from the thread, so a node that runs twice leaves one row. The table lives in its own file, under the
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
time. One process serves a household: at startup the process takes an
exclusive lock on a file beside the drafts file and another beside the
saved-state file, after each path has passed the guard the stores apply, each named for the file as it really is so two spellings of
one file claim one lock, and holds them while it runs, so a second process
over either file is refused with a sentence naming the lock, and the decision
lock, the set of runs in flight, and the sweep cover everything that happens
to those files.

The page at `/parent` is the same three things as forms: a date to plan for
her, the drafts waiting for review with their text and two buttons, and the
earlier plans, each reviewed, expired, or superseded by a later one. Its two
form actions call the functions the JSON routes call and redirect back to the
page, so there is one way to start a run and one way to review whichever door
it comes through, and a failure renders the page with the same status and
reason the API would have answered. The decision field admits exactly the two
button values, and the reason is capped at `REASON_MAX_LENGTH`, five hundred
characters, on the form and on the JSON request alike, so a longer one is
refused at the boundary rather than stored. The two buttons read "Looks good"
and "Ask for a change", and each carries an accessible name with the draft's
evening, and its position when several wait, so the controls can be told apart
without reading around them. Without a key the page still reads and says why
a plan cannot start.

Under each draft the page shows how the plan was made: the run's step records,
one per node, each saying what the node expected and what it found. A run that
ended before the gate, because its plan never passed the checks or the model
did not answer, has no draft to show, so the page lists it under its own
heading with the same record, and the JSON answer to `POST /parent/plans`
carries the steps too. The graph saves the record, not the route, so a process
that stops between the run and the page cannot leave a draft without its
account: `compose` writes the draft and the record in one transaction, and a
run that ends before the gate writes the record from its last node. The record
sits in two tables beside the drafts, one row per run and one per step. A
replacement that fails part way rolls back whole, a repeat keeps the first
time stamp, and every read of a draft or a run joins its steps in one query so
no record pairs one save's outcome with another's steps.

**Not built:** her page shows only today's plan. A plan made for another
evening reaches her page on that day, and nothing shows her a plan ahead of
time.

## Sources disagree, and that is the interesting case

Assignment state is assembled from several imperfect channels rather than
retrieved from an authoritative record. `blossom/reconciliation.py` returns one
of three outcomes and never picks a winner:

| Outcome | Meaning |
|---|---|
| `Agreement` | Every channel that spoke asserted the same value. Contributing records are kept, so one agreeing channel stays distinguishable from four. |
| `Disagreement` | Channels conflict. Every claim is preserved with the channel that made it. |
| `NoSourceRecords` | Nothing corroborates this fact at all. |

For a due date, only claims that read as dates take part, compared by the
date each names and kept as the source spelled them, so a stray space is not a
disagreement and a reader still sees what was said. A
value the reader cannot make a date of, a weekday name for one, takes no part
in the outcome; her page lists it apart, and it neither confirms nor
contradicts anything. That is `reconcile_dates` in `blossom/noticing.py`, and
both her page and the graph's `retrieve` node go through it.

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

## Seven stores, seven risk profiles

| Store | Contents | State |
|---|---|---|
| `ProjectStateStore` | Assignments: due and assigned dates, either possibly absent, kind, dependencies, reported submission status; every channel's claim about a due date; what the school reports about a status; her own updates, a chain of events per assignment; the family's checks of a Done beside a Missing, a chain of events per assignment | Wired and tested; a file at `BLOSSOM_DATABASE_PATH`, read from a fixture only when the start creates the file |
| `SupportRulesStore` | Operational rules derived from her accommodations, one per chunk | Seeded from the fixtures; read whole by the plan graph |
| `ReflectionsStore` | The agent's notes about its own performance | Seeded from the fixtures; read whole by the plan graph |
| `DraftsStore` | Every draft that reached the gate, as its text and, in a nullable versioned column, the plan as data; every decision about it; and every run's record of what each node expected and found | Wired and tested; a file at `BLOSSOM_DATABASE_PATH` |
| `TraceStore` | The framework's trace of each run: every node and model call with inputs, outputs, and errors, redacted on the way in | Wired and tested; a file at `BLOSSOM_TRACE_PATH`, swept after two weeks |
| `WorkloadSignalsStore` | Her presses of the "too much" control: which evening, when, nothing about her | Wired and tested; in the drafts file, swept after a week, deletable by her |
| `HelpRequestsStore` | Her requests for help: when, her words if any, where each stands, the parent's word back, and the id of the homework note a request is about, never the note's words | Wired and tested; in the drafts file, kept until resolved and swept two weeks after |

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

`blossom/plan_checks.py` runs the tier-one checks over a proposed plan: the
plan is for the evening the run was asked to plan, every assignment it names
exists, nothing due in the window is unmentioned, each assignment is worked
on or put off rather than both, no block is scheduled after its deadline, no
two blocks claim the same minute, and the evening is inside the household's
budget. Several blocks for one assignment are fine, because splitting an
essay over two sittings is good planning; being both planned and deferred is
the plan contradicting itself. A block's rationale and a deferral's reason
cannot be blank, so putting work off always comes with an account of itself.
Each failure is reported in words, so a critic and a person are told what is
wrong rather than left to work it out. `PlanVerification.passed` is derived,
as tier one's always is. The evening is handed to the checks by the caller
and has no default: the graph passes the `plan_date` its run was started
with, never the plan's own date and never the clock, since a parent can ask
for another evening and a run can cross midnight. A plan dated a day early or
late is sent back with both dates like any failing plan, before the reviewer
is asked, and is never redated; the drafts store still refuses a bundle whose
snapshot and draft name different evenings, whatever the checks said.

A due date that is anything short of corroborated does not fail a plan. It is
carried on the result as a flag, because a plan cannot be more certain than the
record it was built from. One source counts as short of corroborated: that is
the reason `SINGLE_SOURCE` is a state of its own rather than a kind of yes, and
the flag is read by exclusion so a state added later reads as uncertain until
somebody decides otherwise.

The two minute budgets are the household's settings, `BLOSSOM_EVENING_MINUTES`
and `BLOSSOM_TOO_MUCH_MINUTES`, read once at startup and handed to the graph:
what an evening may hold, and what it is held to once she has said today is too
much. Neither is a rule of the system, because no evening length is right for
every family. The defaults are 150 and 75, and the second must be less than
the first or her signal would change nothing.

**Not built:** a way to change the budgets from a page rather than the
environment, and a budget that varies by day of the week.

## The plan graph

`blossom/agent/graph.py` is a workflow, not an agent. The planner and the
critic are model calls that each return one typed value, `DailyPlan` and
`CriticVerdict`, through the provider's constrained output rather than a tool
call, and neither holds a tool. So there is no loop in which a model decides
what to call next: the graph decides, from the checks and the verdict, and
every route it can take is written in one file.

Eight nodes. `retrieve` reads every assignment on record,
states each one's due date before it reads the sources, sets the two against
each other, and then selects the week: undated work, and dated work that the
record or any source puts in the window. It reads her updates with the rows
and leaves out what she has reported done, so the planner, the critic, and
the checks see the work still to do, each assignment with its dependencies
as the record has them, while the ids left out are kept on the server for
the check that fails a plan speaking about them. That reading is the run's
input from there on: both models, the checks, and every revision work from
it, and the draft carries its fingerprint. The models are asked without the
decision lock, so her page can save an update while one is answering; the
update is not swapped in half way and no second plan is paid for on its
account. The draft reads as stale on both pages the moment it is published,
its notice names work she reports as done, and approval is refused until a
new plan is asked for, the same whichever model was being asked when the
update landed. The run's record keeps every
finding in full; what goes back to the planner leaves out every finding that
names work she has reported done, whichever check it tripped, and says only
to use the work listed; a window with nothing left when it is read ends the run
there, with its record and no model asked, as the routes refuse such an
evening before the run.
`plan` asks the planner. `verify` runs the tier-one checks, and a plan that fails goes
back to `plan` with the findings before any critic sees it, because a
judgment about a plan that is already wrong is a wasted call. `critique` asks
the critic; fault sends the plan back with the critique, doubt sends it
forward. `compose` renders the plan, the doubtful due dates, and the
reviewer's notes as the text she reads, and saves it to the drafts table
under an id derived from the thread, as the record; the route publishes it
once the run has paused, and from that moment the plan is on her page. `require_human_approval` is the gate from `blossom/agent/gates.py`,
unchanged in mechanism and narrowed in meaning: it pauses the thread for a
parent's review, which she does not wait for. Ordinary planning is hers, so
the review is shown under the plan on her page, looks good or a change asked
for, and is never a condition on her using it. The pause is the same one that
will hold a note to a teacher or a request for help until a person decides,
where a decision is the point. `record_decision`, after the gate, saves what
the parent said; it is the only node past the gate, and a node there may be
added without a version bump. `record_run` is where a
run goes instead of `compose` when it ends before the gate, with a plan that
never passed the checks or a model that did not answer: it saves the run's
record, which `compose` saves with the draft. It is off the path to the gate,
so a paused thread never meets it and the version stays put.

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
the reviewer faulted, could not tell, or left out, and what a model call cost
in tokens when the answer carried it. The reviewer's critique is its own
prose, so it stays in the draft's notes and out of the record; the planner's
rationales likewise. The record exists because the final state cannot
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

The framework's own trace is kept too. `LocalRunTracer` in
`blossom/agent/trace.py` subclasses the framework's tracer base, the class
that assembles a tree of runs for the graph, each node, and each model call,
and writes the finished tree to `TraceStore` in `blossom/stores/traces.py`.
Every input, output, and error passes through a redaction hook first, a plain
function from text to text whose default changes nothing and which a household
replaces in one place. The tracer is attached to every run the routes start
or resume through the run configuration's callbacks, which are never saved
with the run, and the hosted tracer stays closed: the boundary scan opens the
tracer base and its run schema to this one file and nothing else, and the
package-level re-export stays closed even there. The trace holds the
student's schoolwork verbatim, so it is its own file, and rows older than
`TRACE_RETENTION_DAYS` are swept at startup, after each run, and every hour
the process is up. The store
stamps and sweeps by the real clock even when the household clock is pinned
for the fixtures, since a pinned clock would never move the cutoff. Nothing
reads it to decide anything; it is for finding out why a run did what it did.

**Not built:** only the due date is compared. The design's example is a
submission status the record holds and a portal can confirm or deny, and
nothing observes submission status yet. No page shows a trace; it is read from
the file.

## The workload signal

The control is one press. `POST /student/workload-signals` takes no body, and
the button on her page sends nothing but the press. It does not ask her to
rate or describe anything: assigning a rating requires stepping back and
assessing, and that capacity is least available exactly when the signal
matters most. Words may be attached, and are never asked for.

A press records that today, by the household's clock, is too much, in
`WorkloadSignalsStore`, `blossom/stores/workload_signals.py`. Three things
follow, each visible at once. Her page shows the signal, the time she gave it,
and what it changes, with a button to take it back. The evening's budget is
cut to the household's shorter one, `BLOSSOM_TOO_MUCH_MINUTES`, and the cut is
made in the graph's `retrieve` node before the planner is asked, so the
tier-one budget check enforces it and
the planner is told, in a block written by this system, that her word on the
evening is final. The draft, written to her and read by both, says the plan
was kept to the reduced budget because she said so. This is the third tier of verification
acting the only way it can: her judgment overrides the plan directly rather
than becoming one more input to a score.

A draft already waiting for the evening carries the signal state it was made
for. When her signal as it stands is not that one, a signal with a plan made
for the full evening or none with a plan kept short, the parent's page says to
plan again, the approve button is gone, and the approval route refuses with
the same sentence; neither page says which came first, since a signal can
change while a run is still on its way to the draft;
refusing still works, since refusing sends nothing. A plan made after the
change fits again, and on the parent's page a decided draft is not measured
against the evening again, nor is a draft for an evening that has passed,
which cannot be planned again and reaches no page of hers. Her page measures today's latest plan against the
signal as it stands whatever a parent has said about it, since the plan is
hers to use either way, and says in her words why to plan again. A signal is
recorded or taken back under the lock a decision holds, so
the evening a decision was checked against cannot change before the decision
lands. A signal that is gone was taken back or aged out, and the store does
not say which, so the message names both rather than putting an action on her
that she may not have taken.

Words she adds are capped at `DETAIL_MAX_LENGTH`, five hundred characters, at
the boundary and in the store, so a request cannot grow the file or every
later page. The store keeps a signal for `SIGNAL_RETENTION_DAYS`, seven, stamped and swept
by the real clock even when the household clock is pinned, and every read
applies the same cutoff, so a signal past its week stops counting whether or
not a sweep has run since. The sweep runs at startup and then every hour the
process is up, with the trace sweep and the saved-state rules, so nothing
waits for a restart to be gone. Her page lists
everything still kept, each with a way to remove it, and the JSON routes list
and delete the same. The store answers one question for the planner, whether
an evening was signaled, and offers nothing about patterns: no query groups
signals by weekday or counts them over a month, because a record like that
would be about her rather than about the plan.

**Not built:** the design wants the control to be closer to a stress ball than
a button on a page, a hardware button, a lock screen control, or a single-tap
shortcut, and whether asking for a coping strategy is the same gesture or a
second one is a question for her. The page's button is the form the control
takes until those are decided with her.

## Homework notes

A homework note is her own words about something to remember, saved before it
is homework. `blossom/captures.py` holds the types and the rules and reads no
file; `blossom/stores/captures.py` is the part of the record's store that
keeps them, in the file the assignments are in, since the later step that
makes a note into homework has to write both as one thing.
`blossom/routes/captures.py` holds the pages.

`homework_captures` holds each note as it stands, the words of its first save,
which nothing changes, everything that first save sent, and for a class or a
day who supplied it and through which way in. `capture_events` holds every
change with what stood before and after, and who made it: the student, a
parent, or the household when the sign-in is off. Both tables and both
indexes are made in one transaction, so a start that is refused one of them
leaves an older file as it was. A note's place among all notes is the number
the file gave its first event, so an edit, an archive, or a restore never
moves it.

A form is given a random id when its page is made, which writes nothing. A
first save under a new id is written with its event in one transaction that
reserves the writer before it reads. The same id again is compared with
everything that first save sent, the words with the class and the day: the
same is the same save, answered with the note as it stands now, edited or
archived as it may be, and nothing is written or brought back; anything else
is refused with both shown. An edit is compared whole too. The same three as
stand are already saved whatever page sent them; anything else must come from
the revision the page showed, so a page that is behind overwrites nothing and
an archived note is never brought back by an edit. An archive and a restore
go by that revision as well and never touch the words. A result names the
change the save made by the id the record gave it, and a save that wrote
nothing names the latest change it found, read in the transaction that found
nothing to do. The page looks that id up in the note's own history: while it
is the latest the result is what stands, once something newer follows it is
said as something done earlier, and a revision number, another note's id, or
anything else a person could put in the address says nothing.

A note on record that cannot be read is said as unavailable wherever it is
opened, its own page and the help page alike, and is never said to be off the
record; a request for help about it is refused and sends nothing. A change in
a note's history that cannot be read makes the note's page say the same. The
endpoints that answer a request for help in JSON read the notes their requests
name, once for all of them, as the two pages do.

A note's changes are one line or nothing is added to them. Every write to a
note on record, the same form again included, first reads that note's changes
through the store's own connection, inside the transaction that reserved the
writer, and holds them to it: the first is the first save, at revision 1, with
what that save sent and the place the file gave it; each later change is one
revision on, starts where the one before ended, and is what its kind does, an
archive and a restore moving the note one way each and touching no words, an
edit never bringing an archived note back; every snapshot is within the rules
a note's words are held to; and the last ends at the note as it stands. Days
and times are not compared, since a clock may run backward. The change about
to be written is held to the same rules as the last of that line before it is
written. A line that is not sound is refused with its cause kept, nothing is
written and nothing is mended, and the note's page says the note cannot be read
and gives no result. The page that offers to ask for help about a note, and the
request itself, read the note the same way, so a note its own page cannot show
is neither shown there nor asked about. The lists read no history, so this
costs a list nothing.

Whether a note is put away is written as 0 or 1 and read as nothing else. A row
that holds anything else is read with the notes that wait and named there as
one that cannot be read, so a damaged flag never takes a note off both lists
while its own page calls it archived. A note that cannot be read is counted
wherever her notes are counted, since it is still a note of hers that waits. A
revision is read from a form only as these pages write it, a count from 1 in
plain digits, because a save that asks for what already stands is answered
before revisions are compared. The notes a request for help names are context
for it: a read of them that fails is logged and shows each as unavailable, and
never fails an accept or a resolve that is already written.

Words are tidied on the way in and never on the way out. What a form sends is
kept under the text rule, edges off, line endings as one kind, a blank class as
no class. What the file holds must already be that: a row whose words, class,
or first save are held any other way was not written by the store, is not
quietly mended when read, and is a note that cannot be read, on the lists as on
its own page. The same holds for types and spellings. SQLite keeps bytes put
into a text column as bytes and turns a number put there into its digits, and
Python would make words of the first and read `20260918` as a day and `0_1` as
a count. So a note's row and a change's row are read column by column as what
the store writes: text as text, a count as an integer from 1, a day and a
moment in the one spelling it writes them, a snapshot as the JSON it wrote, a
change's id in the one shape it gives out. Revisions, a change's place in the
file, and a note's place among notes all start at 1, so nought or less was
written by nothing here, and a place of nought would sort its note ahead of
every real one. Nothing is coerced on its way to being her words. The details
a note may be given, the title, the kind, and the note about the work, and the
assignment a note is in, are read the same strict way and held to the limits
an assignment's fields are held to. The help store reads the id of the note
a request is about the same way: a note's id in the one spelling it writes, or
nothing. Anything else held there is never turned into text, since corrupted
bytes print as ordinary characters and would be handed on as an id; the request
is kept, marked as about a note whose reference cannot be read, shown with its
note unavailable, and answered in JSON with no id at all. The words of a day
that could not be read, which a form carries only to be shown again, are valid
only as a page writes them; sent any other way the form is refused whole,
whichever button is pressed beside them. The fields that steer the date are
held to the combinations a page renders, and the rule is the whole table: a
form with no mark has the one button, or none for the Enter key; a form marked
by a refused day has both buttons and carries the refused words only when they
could be shown, which are always words that did not read as a day. So the
button that saves without the date counts only with the mark, where it would
otherwise drop a day on the word of a button no page showed, and refused words
that read as a day were refused by no page. Save without the date does what it says whatever the date control
holds beside it: no day goes to the store, and what she typed there stays on
the form, with its mark, only so that another refusal can show it again.

A note's id is a UUID and its routes take it as one path segment. A note is
no assignment, makes no claim about a date, and is read by no model: the
pages read notes beside the record and never into it.

### From a note to homework

A note becomes homework by explicit choice, hers or a parent's, and by nothing
else: no title, date, or class is worked out from her words, and no model is
asked. `blossom/routes/note_details.py` holds one page in two trees,
`/student/homework-notes/{id}/add` and `/parent/homework-notes/{id}/add`. The
tree a press comes through is the channel it is recorded on, her report or a
family entry, and nothing a form carries can say otherwise; who pressed is
the sign-in, and the household while the sign-in is off. Signed in as the
other person, a press writes nothing and is answered 403.

Details are a class, a title, a day, a kind, and a note about the work, each
optional on a note and each with who supplied it. `clarify_capture` saves them
from the revision the page showed as one `clarify` event; a field whose value
did not change keeps the attribution it had, so a parent pressing the button
does not become the author of what she typed. Her words are no part of the
form. The class is chosen from the classes homework on record names, or typed
beside Another class; there is no catalog. The title is proposed from her
words only when they are one line that fits a title, and is never cut to fit.

`promote_capture` adds the note to homework in one transaction that reserves
the writer before it reads: the assignment, the note's link to it, the claim
about its date when a day was given, and the event with the decision are one
commit, and a failure leaves none of them. The assignment's id is named from
the note's id in a fixed namespace, never from its title or date, so the same
form again, or a lost answer sent again, finds the link standing and writes
nothing. Its record is marked with the way in the press came through and each
field with whoever supplied it; no report of the school's is made up for it.

Homework on record with the same class and title, paired by the rule the
school's paste pairs by, whitespace collapsed and case kept, is never merged
by itself. Every such assignment is a candidate whatever its date. The page
lists them and sends back a fingerprint of the list it showed, a digest of
each candidate's id, class, title, date, kind, and status. The store reads the
candidates again inside its transaction: a fingerprint that differs means
homework arrived, left, or changed since, and the choice is put again with
409 and everything typed kept. Same homework joins the note to that assignment
and changes nothing on it; a day the note gives is one more claim beside the
others. Keep as a separate assignment makes the note's own. The event keeps
the choice, the candidates it was made among, and the fingerprint.

`date_claims` says where a claim came from. Its rows carry the note and the
note's revision when a note made the claim, whether the claim still counts,
and when it was withdrawn; the columns are added to an older file in one
transaction, every insert names its columns, and a unique index over the
assignment, the note, and the revision makes a repeated write add nothing.
Readers that reconcile a date read only claims that count. A claim from a note
carries a confidence of 0.8 because the column requires one; nothing decides
anything by it.

A school paste that names homework made from a note is held whole. Until the
pages can ask whether the school's row is the same work, a text with such a
row saves nothing, not that row and not the rows beside it: the review names
the rows in the way, offers no save, and keeps the text, and a save sent
anyway is answered 409. Homework made from a note is known by its id, since a
parent's press marks the record a family entry and her ordinary reports on
school homework mark nothing. The save reserves the writer and compares inside
that transaction, so a note added through another connection between the
review and the save is met too.

A note in homework leaves the notes that wait and is listed with the others
that are in homework. Its page says so, leads to the assignment, and says when
the assignment is outside today's planning window. The assignment's details
show the notes it was added from or joined by, her words as they stand with
the day each was saved, as evidence beside the record: nothing of them is
copied into the assignment, and changing a note afterward changes nothing
there. Rows on her week and the family page say what a waiting note still
needs, a class and a title, a choice, or nothing more, from the one read of
the assignments those pages already make. Searching the record for homework
to join a note to, changing a link, and unlinking are not built.

## Asking for help

The design has her ask for help through the system as she asks for a lighter
evening: one press, and words only if she wants them. Unlike the signal, a
request is addressed to a person, so it has a state that a person moves.
`HelpRequestsStore`, `blossom/stores/help_requests.py`, keeps each request as
requested until a parent takes it up, accepted while the parent is on it, and
resolved when they answer, with a word back at either step if they leave one.
Her page shows the request and each step in plain words, and says a parent has
not seen it until a parent has taken it up, so nobody is said to be looking
into something before they have said so. She can take a request back while
it is only requested; once a parent has taken it up, it is theirs to resolve.
The parent's page lists what is open with the two moves under each and what
was resolved in the last two weeks. Both pages read the same view: what a
parent does with a request is shown to her in full, and nothing is kept about
a request that either of them cannot see.

A request is stamped by the real clock, like a signal, so retention runs even
when the household clock is pinned. A resolved request is kept for
`HELP_RETENTION_DAYS`, fourteen, long enough for the word back to be read, and
the cutoff is applied on every read as well as in the hourly sweep; an open
request is kept until someone resolves it, since a question nobody has
answered is not old news. Nothing counts requests or groups them by anything.

**Not built:** a request reaches the parent's page and nowhere else. The
design's notification to a parent, and her seeing that it went, are not
built.

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

Her page frames the school week, Monday to Sunday, the way the school's own
page does, and moves to the weeks either side. The planner reads a different
window, the evening it plans and the six days after, because a plan made on a
Sunday has to see the week ahead; her page says through which day a plan
looks. Both go through `read_week`, so they never differ about whether an
item is in a window, only about where the window starts.

**Not built:** a calendar policy. Nothing yet knows about no-school days,
bedtimes, or a term calendar. Durations that cross a daylight-saving night
need to be computed in UTC when that arrives; both transitions fall inside the
school year and are covered by tests today only at the level of the date.

Stores are opened once by the application lifespan in
`blossom/dependencies.py` and injected into routes with `Depends`. The SQLite
connection is shared across FastAPI's worker threads, so it is opened with
`check_same_thread=False` and every statement is serialized behind a lock.

`BLOSSOM_DATABASE_PATH` holds the assignments, every channel's claim about
their dates, and the drafts table, so the record and the parent's queue
survive a restart. A fixture, when one is named, is read whole and written
only into a blank file, in one transaction with the file's own tables, so a
start cut short leaves nothing the next start would take for the record; a
file with anything in it is the household's record, whatever it holds, and
is left alone.

## Saved graph state

A graph's state, including a pause at the approval gate and the draft it holds,
is saved by the asynchronous SQLite saver, opened by the lifespan from
`blossom/stores/checkpoints.py` on the file named by `BLOSSOM_CHECKPOINT_PATH`.
That file is separate from project state so the two writers never contend and
clearing a thread touches nothing else. Startup refuses a path on a network
share or inside a synced folder, through the one guard in
`blossom/stores/paths.py` that every state file passes; deleted rows are
overwritten.

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
value carried in state gains fields only with defaults and is never renamed
or moved between modules; the names and order of nodes ahead of a gate are
part of the contract; and every node performs at most one side effect,
written so that running it twice has the same result as once. Any change that
breaks a rule bumps the version, and paused threads are then drained, with
whatever they held re-queued, rather than resumed. A check added to tier one
breaks none of them: the state gains no key and `PlanVerification` no field,
a run paused at the gate is past `verify` and nothing after the gate reads
its verification again, and a run's record keeps the words it was written
with, so a plan that passed seven checks still says so. The version stayed
put when the evening check arrived.

Every run also carries an explicit recursion limit, because the framework's
default is ten thousand and seven supersteps, itself read from the
environment. Durability is `sync`, so the state is on
disk before the next step starts rather than while it runs; the framework takes
that as an argument beside the configuration and defaults to `async` when it is
omitted, so a scan refuses a run that builds a configuration from
`blossom/agent/runs.py` and then leaves it out. Only scalar values in the run's
configuration are saved as metadata, in plaintext; nothing about the student
goes there.

Saved state is the loop's short-term memory, and the design keeps it only
while the loop runs. `blossom/agent/retention.py` holds the rule. A run that
stops before the gate has its thread cleared by the route as soon as it
returns, since its record is already in the drafts file; a run paused at the
gate keeps its thread until a decision is recorded, and the route clears it
then. A draft nobody decides within `PAUSED_RETENTION_DAYS` of its evening is
closed as expired, and a draft a later plan for the same evening is published
over is closed as superseded, so at most one draft waits per evening and the
plan on her page is the one a review can land on; those are the two decision
values the system records itself, and a thread is cleared with each.

A draft is saved when it is composed and published when its run pauses, and
the two are different acts. The save is the record: it survives whatever
happens next, and a node replayed after a crash saves its text again and
changes nothing else. Publication, done by the route under the decision lock
once the run has paused, is what puts the draft on the pages: it takes the
next place in the published order, closes any published draft still waiting
for the evening as superseded by it, and clears their threads, since no review
can reach them. So nothing either page shows is a draft whose run might still
fail, a review in progress lands or is refused before its thread goes, and
which plan is current follows the order runs paused in, whatever order their
drafts were composed or saved in. A run that fails between saving and pausing
takes its draft back: the row goes and the run is kept with its steps as
interrupted. It displaced nothing, so the pages are what they were before the
run, and the run itself is listed among those that ended without a plan. A
publication that fails is treated the same way, before the failure reaches
the page, and each half of taking back is attempted whatever became of the
other. Before a plan is published, any review a waiting draft's thread holds
that the table never got is recorded, so a review that reached the thread is
never superseded away with it. A run joins the set of runs in flight under the decision lock, so
it starts either before a sweep or after one and a sweep never counts threads
while a run is joining. The
draft is taken back first and the thread cleared second, because the
saved-state store is the likelier of the two to be what failed; a thread that
cannot be cleared is left to the sweep. Opening a drafts file restores two
invariants whatever version wrote it: every published draft has its place in
the published order, and one draft waits per evening, the rest closed as
superseded by the evening's latest, so a file from before these rules, or one
a dying process left half opened, is brought into line and the startup sweep
clears the threads of what was closed. At startup a sweep applies the rules to
whatever the last process left behind: a draft saved but never published is
published if its thread paused at the gate, which the interrupt left on the
saved state shows, several in the order their checkpoints say they paused,
and taken back if the thread is missing, never reached the draft, or holds it
without having paused; a published draft no thread can review is taken back
too,
the drafts that waited too long are expired, and every thread that no waiting
draft refers to is cleared, which covers finished runs whose thread was never
removed and runs that never finished. The same sweep runs every hour the
process is up, under the decision lock, so a draft's fortnight ends when it
ends rather than at the next restart; it is told which threads the process is
running at that moment and leaves those runs and their drafts alone. A review
that reaches a thread and then fails to land in the table leaves the thread
past the gate with the decision it holds; the next review of that draft, or
the next sweep, finishes the record with that decision rather than taking a
new one, whatever the request or the evening's signal says by then, and a
request that disagrees is told what stood. Such a draft is never expired away.
Tidying a thread after a run or a review has its outcome is never what a
caller hears about: a thread that cannot be cleared is left to the sweep.
The saver's only pruning primitive deletes a thread whole, and
that is the only granularity the rule needs.

**Not built:** the student's ability to see and delete what a thread holds.

## Stack

The design notes specify LangChain for generation and judging, LangGraph for
control flow and saved state, and MCP for external tools. LangChain and
LangGraph are present, and so far they build the framework's tool objects,
run the tool backstop, pause a graph at the approval gate, construct the model
client in one seam, `blossom/anthropic_client.py`, with the endpoint fixed in
code so that no environment variable decides where a prompt is sent, keep a
graph's saved state in a SQLite file of its own, run the plan graph, whose two
model calls each return one typed value, and give the local tracer its base
class. The routes in
`blossom/routes/runs.py` drive the graph from her page and the parent's, and
`blossom/routes/parent.py` resumes it with the review; her week page and the
placeholder checkpoint and verifier routes are plain handlers with no graph
behind them. MCP is absent. When it arrives, tools it
loads will be foreign to the backstop until each has a registry entry of its
own in `blossom/tools.py`, which is the intended path; how a tool that reads
rather than drafts fits a registry whose callables return only drafts is an
open design question.

Adding any other dependency fails a test until someone edits `ALLOWED_IMPORTS`
with a justification, so the stack cannot grow by accident.
