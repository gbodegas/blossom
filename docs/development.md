# Development and troubleshooting

The README gets Blossom running once. This is everything else a person
working on it locally needs: what the server serves, how it is configured,
what it writes to disk, what the planner sends and costs, the fallback when
uv cannot download, and the problems seen so far with their fixes. The design
and the reasons behind it are in [architecture.md](architecture.md).

## Installing uv

The [uv installation page](https://docs.astral.sh/uv/getting-started/installation/)
has the current instructions. The one-line installers are:

**Windows** (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS and Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal after installing so `uv` is on your path. If it still is
not, `pip install uv` works too. If no Python 3.12 or 3.13 is installed, uv
downloads one on its own when it builds the environment.

## What the server serves

With the app running as the README describes:

- <http://127.0.0.1:8000/student/due-this-week> is her page: today's plan,
  which she asks for there and which appears the moment it is made, with a
  parent's review under it once there is one; then the school week, Monday
  to Sunday, every assignment saying where its due date came from, and the
  work assigned that week and due after it. `?week=` with any date shows the
  week that holds it. Each card takes her update, Done or Not yet with an
  optional note, through a form alone, and offers to change or undo it; work
  she has reported done folds under the active cards. There is no JSON route
  for her updates.
- <http://127.0.0.1:8000/student/assignments/ASSIGNMENT-ID> is one
  assignment's details: its current record, her update through the same form
  and the same two routes as her cards, and a link back to where the reader
  came from. Cards, the rows of a saved plan, and the family page's rows link
  to it by id. The way back is three query fields, `return_to` (`week`,
  `today`, or `family`), `week`, and `plan_id`, checked on the server; nothing
  else is read as an address, and a value these pages do not make falls back
  to her week, or to the family page for a parent who is signed in. A parent
  reads the details and cannot save from them. There is no JSON route for it.
- <http://127.0.0.1:8000/student/plans/today> is today's plan as JSON; a POST
  to `/student/plans` makes one.
- <http://127.0.0.1:8000/student/homework-notes> is her homework notes, with
  the ones she put away under `/archived`, the ones in homework under
  `/added`, the form for a new one under `/new`, and each note on a page of
  its own under its id, where asking for help about it is `/help`, giving
  it details or adding it to homework is `/add`, and finding homework already
  here to join it to is `/search`, with the search words and the page of
  results in the address. A parent's way to those last two pages is
  <http://127.0.0.1:8000/parent/homework-notes/{id}/add> and `/search` beside
  it, linked from the family page. Joining a note to homework found, moving
  it, and unlinking it are presses under `/student/actions/homework-notes/{id}`
  and `/parent/actions/homework-notes/{id}`, `/link` and `/unlink`. Reading
  any of the pages writes nothing.
- <http://127.0.0.1:8000/student/help-requests> lists her requests for help
  as JSON, the open ones and those resolved within two weeks; a POST there
  asks, with an optional note, and a DELETE takes one back while nobody has
  taken it up.
  The parent's side is `/parent/help-requests`, with `/accept` and
  `/resolve` under each request.
- <http://127.0.0.1:8000/parent> is the parent's page: read the plan she has,
  see how it was made step by step, and say it looks good or ask for a
  change. A parent can also start an evening's plan for her there. A run that
  ended without a plan is listed with its steps too. Planning needs an API
  key; reading and reviewing do not, so without one either page says a plan
  cannot start and everything else works. Where her Done stands beside a
  school report of Missing, the page offers Mark checked, with a note for
  her card, and Check again once marked; both are forms alone that write
  the family's own record and nothing of hers or the school's.
- <http://127.0.0.1:8000/parent/approvals> is the review queue as JSON.
- <http://127.0.0.1:8000/parent/checkpoint> is the parent's checkpoint, a
  summary of status and conflicts rather than a live feed. A placeholder,
  JSON for now.
- <http://127.0.0.1:8000/verifier/claims> is the verifier's view: each
  factual claim, the policy it was checked against, and the result. A
  placeholder: one fixed example of the shape, not a report on the latest
  run. JSON for now.
- <http://127.0.0.1:8000/docs> is the interactive API page, where the
  parent's routes can be driven directly. The "too much" signal is sent by
  posting to `/student/workload-signals` with no body; the same path lists
  the signals still kept, and a delete on one removes it.

With neither passphrase set, nothing here has a login: the views are separate
pages, and anyone who can reach the server can open all of them. That is the
shape for a machine only the family touches. For the household's own use, see
"Running for the household" below.

Whether or not the passphrases are set, a request that changes something, any
POST or DELETE, must say it came from this server: browsers send an Origin
header with every form and script call, and one naming another site, or none,
is answered 403 in plain text. For curl requests that change state through
the JSON routes, send an Origin header matching the URL's scheme, host, and
port, for example `-H 'Origin: http://localhost:8000'` when calling
`http://localhost:8000`. The interactive API page sends it on its own.

## Configuration

Every setting is an environment variable named in `.env.example`, and
`--env-file` on the `uv run` command reads whichever file is named. The
fixture folder and the three files the app writes are configurable through
the `BLOSSOM_*` variables; the packaged templates and static assets are not.
Each path has a working default under `.local/`.

One variable has no default and must be set: `BLOSSOM_TIMEZONE`, the
household's IANA zone, because "due this week" means the days you live in and
no value is right for everyone. `BLOSSOM_TODAY` pins the clock to a date;
unset, the real clock is used. `.env.example` sets both, which is why the
first run needs nothing else. Either can also be set as an ordinary
environment variable in the shell.

Two variables are numbers: `BLOSSOM_EVENING_MINUTES`, how many minutes of
schoolwork an evening's plan may hold, and `BLOSSOM_TOO_MUCH_MINUTES`, what a
plan is held to on an evening she has said is too much. They are 150 and 75
unless set, and the second must be less than the first; the app refuses to
start otherwise, naming the variable.

The fixtures carry due dates in the school week of August 17, 2026, and two
in the week after. Her page frames the school week, Monday to Sunday, with
links to the weeks either side; a plan looks at the chosen day and the six
after it, and the page says so. Anything with no due date on record is in
every week. On the real clock most of the fixtures fall outside the week and
the page shows only the syllabus form; pinned to August 19, it shows five
items, with the algebra set and the reading log listed as assigned this week
and due the next.

## What survives a restart

Three files under `.local/` outlive a restart:

- `blossom.sqlite3` holds the assignments and every channel's claim about
  their dates, read from a fixture only when `BLOSSOM_FIXTURE_PATH` names one
  and the start creates the file, and otherwise only what the family puts
  there. It holds her own updates on each assignment too, Done or Not yet with
  any note, as a chain of events she can take back, kept apart from what the
  school reports, and the family's checks of her Done beside a school
  Missing, a chain of events per assignment in a table of its own. A third
  table, `hand_in_events`, holds what she says about turning work in, from
  an assignment's details: a chain of events per assignment, read by her
  pages and the family page and by nothing that plans. A fourth,
  `school_instructions`, holds the school's instructions for each
  assignment, apart from anyone's note: each one's words once, where it was
  first read, who pasted it, and whether it applies now, was said before,
  or waits for a parent's review. A fifth, `intake_decisions`, keeps each
  answer about which homework a school row is about: the answer, the row's
  class, title, and due date, where it landed, and the homework the card
  listed. A file from before any of them gains the tables on the first start.
  It also holds the drafts, the decisions about them, and the
  record of every run, one line per node saying what it expected and found.
  A draft is its text and, in a nullable `plan_snapshot` column, the plan as
  data: one JSON document with a version, the plan, and the title, course,
  and due date of each assignment as the run read them. A file from before
  the column gains it on the first start and its drafts keep none: they are
  read as text, never rebuilt from it, and no plan needs making again to be
  read.
  Kept for the school year. A draft nobody decides within two weeks of its
  evening is closed as expired, and one a later plan for the same evening is
  published over is closed as superseded, so one plan waits per evening. Her "too much" signals live here too, with
  any words she added, kept for a week and removable from her page, and so
  do her requests for help with what a parent did with each, kept until
  resolved and for two weeks after. Her homework notes are here as well: each
  note as it stands, its first words, and every change to it. There is no way
  to delete a note from a page, putting one away keeps it, and nothing sweeps
  them, so a copy of this file made as a backup holds her notes and their
  history too, for as long as that copy is kept.
- `checkpoints.sqlite3` holds a graph's saved state, including a pause at the
  approval gate. It is cleared as soon as a run ends or a decision is made,
  and an expired draft's state goes with it, so it holds only what is
  waiting for a person.
- `traces.sqlite3` holds the framework's trace of each run: every node and
  model call with what went in and came out, for looking into a run that
  went wrong. It holds prompts and answers verbatim, so rows older than two
  weeks are swept at startup, after each run, and every hour the process is
  up; the same hourly sweep expires drafts and removes old signals. A
  redaction hook in
  `blossom/agent/trace.py` is the place to keep names or dates out of it;
  the default keeps everything.

Deleting the three files resets the demo, and with it every saved draft,
decision, run, and trace.

The test suite never opens them. Each test keeps its state in a temporary
folder. A `BLOSSOM_DATABASE_PATH`, `BLOSSOM_CHECKPOINT_PATH`, or
`BLOSSOM_TRACE_PATH` the shell hands the run is set aside until it ends, and
a test that reaches for `.local/`, or for the folder one of those variables
named, is refused before a folder is made or a file opened there, however
the path is spelled. `tests/state_guard.py` holds the rule and
`tests/test_state_guard.py` holds it to account.

The helpers the tests share for preparing settings, opening a record, or
building the application, in `tests/support.py`, work only in such a run.
Anywhere else they refuse before they build or open anything: a script that
imported them would build the application on the defaults, which is the
checkout's own `.local/`, with no fixture there to move them. They ask
whether the guard is really in place behind the application. That pytest is
imported, or what the environment says, is not that.

To look at the application by hand, run the application itself with all three
variables naming files in one folder made to be thrown away. The claim's two
lock files and the sign-in secret are made beside the files those variables
name, so they land in that folder as well, and nothing else is written. A
second checkout is not that: its defaults are a `.local/` of its own, and a
variable left in the shell still names whatever it named.

## Running the planner for real

Copy `.env.example` to `.env`, put an Anthropic API key in
`ANTHROPIC_API_KEY`, start the app with `--env-file .env`, and use "Plan it"
on the parent's page. The model is `claude-opus-5`, at high effort for the
planner and medium for the critic, with each answer capped at 16,000 tokens.
The endpoint is fixed in code, so no shell variable can change where a prompt
is sent.

Each request carries the week's assignments with their courses, dates, and
confidence labels, the household's standing rules, the planner's notes about
past plans, and whether she has said the evening is too much; the critic also
receives the proposed plan, and a revision receives what was wrong with the
last one. With the bundled fixtures, all of
that is synthetic. A run is one to six calls. The planner is one; only a plan
that passes the checks goes to the critic, which is another; and a plan that
fails the checks or the critic's review goes back to the planner, up to two
more times. A run the model cuts short ends with the call that failed. What
it costs depends on the week and the revisions; the API's usage page says
after a run. All of this happens before the plan reaches her page and the
parent's for review, and a review sends nothing anywhere.

## Running for the household

Her computer, her tablet, and a parent's computer all open Blossom over the
home network, so the pages ask who is there. Set two passphrases in `.env`,
one hers and one a parent's; they must differ and be at least twelve
characters each, a few plain words:

```
BLOSSOM_STUDENT_PASSPHRASE=a phrase only she knows
BLOSSOM_PARENT_PASSPHRASE=a phrase only the parents know
```

Setting one without the other refuses to start, by name. With both set, every
page and route asks for a sign-in first: her passphrase opens her week, a
parent's opens her week and the family review, and a signed-in student who
opens the family review is told it is a parent's page. A sign-in is a cookie
signed with a secret Blossom makes once and keeps beside the database, in
`household.secret` under the same guard as the database, so a restart keeps
everyone signed in; a device stays signed in for a month or until "Sign out".
If that file is ever short or altered, the start stops and names it: delete
it, start again, and everyone signs in once more.

Choose passphrases that are long and unalike, a few plain words each: they
are typed on a tablet, and they are the whole of who is who. Wrong
passphrases from one device are counted, and after ten that device is told
to wait a minute before trying again; a right passphrase in between does
not start the count over, other devices are not affected, and nothing typed
is kept.

If her sign-in expires before she saves an update, Blossom sends her to
sign in. The update is not saved, and the note she typed is not kept through
the sign-in; after signing in, she enters the update again. A parent signed
in sees her updates on her page and cannot make one in her name. Her device
cannot mark a check: the gate answers it 403 on the family page's paths. An
update saved from an assignment's details meets an expired sign-in the same
way as one saved from a card. So does a homework note: one written or changed
after the sign-in expired is not saved, and its words are not kept through the
sign-in, so she writes it again.

If a passphrase may have been seen, change it in `.env` and restart: every
device signed in with it is signed out, the other person's devices stay
signed in, and the new passphrase works at once. Each person's cookies are
signed with a key drawn from the secret and their own passphrase, which is
what makes the change take. "Sign out" forgets the device it is pressed on
and nothing else; to sign every device out at once, stop the app, delete
`household.secret`, and start it again.

A `.env` written from an example that named `data/synthetic` needs
`BLOSSOM_FIXTURE_PATH` cleared before the first start with the record in the
file. The family's file is safe either way, since a fixture is read only into
a file the start creates, but the planner would read that set's rules and
notes as the household's.

Start the app so the other devices can reach it, on the computer that stays
on:

```bash
uv run --env-file .env uvicorn blossom.app:app --host 0.0.0.0 --port 8000
```

Windows asks once whether to allow Python through the firewall for private
networks; say yes for private only. On her tablet or computer, open
`http://<the computer's name>:8000/student/due-this-week`, where the name is
what Windows shows under Settings, System, About, or the computer's address on
the home network. Bookmark it.

The connection is plain HTTP, which is fine on the home network and nowhere
else: never forward the port through the router or put the server on the
internet. The sign-in tells the two people apart on the family's own network;
it is not a defense against the internet. Nothing goes in front of the
server either: the origin check compares the scheme and address the server
itself was reached by, and a proxy that ended TLS ahead of it would make
every form read as from elsewhere.

### Upgrading and rolling back

An upgrade can move data the older version does not read, as the one that
kept the school's instructions apart from the note field did: it moved every
school note whole, however long. Before upgrading:

1. Stop Blossom.
2. With it stopped, check that no `blossom.sqlite3-journal` file sits beside
   the database; if one does, start and stop the current version once so it
   finishes what it was doing.
3. Copy `blossom.sqlite3` to a dated name in the same folder, such as
   `blossom-2026-09-23.sqlite3`, or make the copy with SQLite's own
   `.backup` command. Keep the copy until the new version has been trusted
   for a while.

To roll back, stop the new version, set its `blossom.sqlite3` aside under
another name, copy the dated file back to `blossom.sqlite3`, and start the
older version. Anything saved after the upgrade is not in that copy.

Running the older version on the upgraded file is not a rollback. It cannot
see the moved instructions: assignments show no school instruction, and
nothing tells the planner about them. A school note it saves is found by the
next start of the newer version and, beside instructions already kept, waits
for a parent's review.

## Adding assignments

The family page has a fold, "Add assignments", with the two ways in. The
first is a box for the school portal's own text: open the homework page, the
weekly summary, or the school's "Missing" email, select the text, copy it,
and paste it whole; "See an example" shows the shape. The reader knows the
portal's line shapes: the day headers, with or without a bullet before them,
the course lines, the ``Assigned: <Title>: (Due:MM/DD/YYYY)`` and
``Due: <Title>:`` cards, the summary's one-line cards, the download's
trailing backslashes, and the email's one line per assignment with the grade
"Missing". It reads them into assignments and into the portal's claims about
their dates, with where each claim was read: the assignment's own line or
the day's header. One item seen under an assigned day and again under a due
day, often in two weeks, is one assignment, matched by its course and title
as the portal writes them.

"Preview assignments" writes nothing. The review page groups what was read
by school week, Monday to Sunday, so work due on a Sunday sits in the week
before the one the portal's Sunday-first picker shows it under, and heads
each week with a count: new, updates, already saved. Each card says exactly
what saving would do. New work is saved as new. A date that differs from the
saved one is shown beside it, "Saved due date" and "Pasted due date", and is
saved as evidence beside the saved date, which stays, so her page can say
the sources disagree. A record with no due date takes the pasted one; an
assigned date the record lacks is filled in. Work that comes round
again under the same name, a weekly practice named again a week or more
from the saved date, or twice in one text a week or more apart, is not
merged in silence: the card asks whether it is the same assignment with its
due date changed or new work. Saying it is the same moves the saved date to
the pasted one, or folds the later card into the first; saying it is new
work makes a row of its own. Either answer is kept, so the next paste of
that text finds the right row and asks nothing, and the same answer sent
twice, from a retry or a page left open, changes nothing.

When the record can't tell which homework a row is about, the card asks. That
happens when the row has the class and title of homework made from her note
and no parent has said yet whether it's the same homework, or when two or more
saved assignments share the row's class and title and its due date doesn't
pick out exactly one. The card lists each of them and "Different homework with
the same title", and nothing is chosen in advance. Same homework puts the
school's dates, reports, and instructions on her assignment and leaves her
note and her dates alone; a school date that differs is kept beside hers.
Different homework makes the school's own assignment, whose id comes from a
token the review page makes once, so a retry finds that assignment instead of
making another. The answer is kept, so the next paste of that work lands
without a question. An undated report, such as a Missing email, that could
mean more than one assignment asks for each report, and placing one report
never places another; the email's leading date is shown but never read as a
date. A paste that repeats text already saved lands where it was saved and
asks nothing. A parent's typed note never replaces the note she gave her own
homework: the review says her note will stay and that the other changes can
still be saved, and the family page says so again after the save.

The type, homework
or task, is suggested from the title, paperwork and materials being tasks,
and can be changed on any card, a saved one included; a parent's choice is
kept as theirs, and the type typed with an entry corrects a saved row the
same way. A choice is a select changed from what the page showed; a card
left as shown is no answer, so when several cards are about one assignment
a type chosen on any of them, a folded one included, is the assignment's,
and two cards choosing different types stop the save until one is picked.
A page that comes back because a question is still open keeps every choice
made on it, the folded cards' answers and the questions answered already
included, and says whether the question is one it put or one the record
raised since. The card shown for
an assignment decides its type: a change made on it, a change back to what
the reader suggested included, stands over any choice a folded card carried. Several cards about one saved assignment in one text compose: the
date one moves, the instruction another brings, and the assigned date a
third fills all reach the one row, every date observation with them, and the count on
return says how many assignments changed, not how many cards. The Save
button counts a question waiting for an answer as a save. "Save N assignments" writes all of it
in one step and returns to the family page with what was added, updated, and
unchanged; "Edit" goes back with the text or the fields as they were;
"Cancel import" writes nothing. Pasting the same page or week again is safe:
what is there already is said to be, and nothing is added twice, even from
two tabs at once.

Lines the reader did not take are listed first, as text that needs review,
with their line numbers, so nothing is dropped in silence. A line shaped
like a day or a card that does not read as one, ``(Due:TBD)`` for instance,
is listed there and never taken for a teacher's words, and it ends the card
before it.

The teacher's instruction under a card is the school's, kept apart from
anyone's note, line breaks and all, once for its assignment however many
cards or pastes bring it: the same words under an Assigned card and a Due
card are one instruction, and a card's day says where it was read, never
that it is newer. The first instruction an assignment gets applies. A paste
that brings a different one, or several at once, asks on the card which of
the school's instructions apply now, listing the saved ones and the new, each
with where it was read; nothing is ticked in advance, and "No school
instruction applies now" is its own answer. Nothing of the text is saved
until every such question is answered, and whatever is not chosen is kept as
history, so nothing the school said is lost. Saved with such a question left
unanswered, the page puts the focus on a summary that names it, and keeps
every answer given on the other cards. All the cards about one
assignment in one text are one question, asked once, on the first card of the
text that lands on it. A choice made on a page the instructions have changed
under since, from another tab or a retry, is refused whole, with the choice
that was not saved said in words beside the instructions as they stand now and
nothing ticked, so it is never carried onto facts it was not made against; a
choice that already stands saves nothing more. The answers on every card of
one assignment, one that ticks nothing among them, are each checked the same
way before any is taken with another, and answers that ask for different
things choose nothing and are said back as not saved. A form the page did not
write, a field sent twice among them, or an answer to a question the page did
not put, is refused whole, and what it chose that can be read is said back as
the parent's unsaved choice on its card, with nothing ticked, even when the
text puts no question there now; the card then offers "Review school
instructions". The page signs the answers to which homework each card is that
it was made with, and each question it asked about the school's instructions
with the revision and assignment it showed, so a save knows which questions it
really asked. A form without that signature, or one from before a restart, is
read as a page made before those answers that asked nothing. The instructions'
words travel in the form encoded, so a browser's line endings never change
them; an instruction longer than any paste, kept from before, travels by its
row, in a choice shown as not saved too, even while its card waits on which
homework it is.

Her card, the assignment's details, the lists of work due later, and the
family page's rows show the instructions that apply as "From the school", in
the order of their words, which means nothing else, with any said before, or
waiting for review, in a fold beneath. The page says these are the school's
words, and that which of them apply is the family's choice, which changes
neither the school's record nor her own updates. The family's reading of the
details, and the family page's rows, offer "Review school instructions",
where a parent, or the household with the sign-in off, can restore an earlier
instruction, retire one, settle one waiting for review, or say none applies;
opening it writes nothing, and it saves by the same rule as the paste. A save
returns to the page with what it did, said as standing only while nothing has
changed since, and signed by the running Blossom, so an address worked out by
hand, or one from before a restart, says nothing; a save that none applies
says so in its own words. A save the file refuses is tried once, never again, and the
answer that was not saved is shown, from the record read once more or, when
that fails too, from a page that reads nothing, which says a choice made by
reference to a row was selected by reference and that its text could not be
read. She reads the instructions
and never the control. A note typed by a parent shows as "A parent wrote",
and the planner is told whose words a note is, and the school's instructions
that apply as the teacher's; a school note left in the old note field, which
no one chose, is shown apart as not yet reviewed and never reaches a plan.
The record keeps where each fact came from, the row itself, its note, its due
and assigned dates, and its type, so her page can say "Entered by a parent";
a parent's typed note fills the note or replaces the saved one.

A "Missing" line in the school's email is kept as what the school reported,
with the day: the email's own date when the paste carries its date line, a
``Date:`` or ``Sent:`` header, a forwarded "On Tue, Sep 8, 2026 at 9:14 AM
... wrote:", or the line "Tue, Sep 8, 2026" itself, otherwise the day it
was pasted, and both pages say which; the day it was pasted is the day the
text was first previewed, carried through the review, so a review that
spans midnight saves the day the page said; the date line is read where it
stands, before the lines it dates, and a date inside a title or an
instruction never dates the email. When one text holds the email and the
portal's page, or the two are saved one after the other, each fact keeps the
channel that gave it: the report is the email's, the dates and the
instruction are the portal's, whichever named the assignment first. The date the
email writes beside an assignment is kept as the text it is, "The email
writes 09/09 beside it, which it does not explain", and is never a due date.
Her page shows the report as a banner on the card, "The school reports this
missing", with the source and the day; the family page lists the same under
"Assignment updates", beside her own word about the work, and puts a "done"
of hers that stands beside a school "missing" first, as worth checking
together.

The second way in is one assignment by hand: a course as the portal names
it and a title are required; a due date, an assigned date, the type, and a
note are optional, and a type left as it is keeps a saved assignment's type
and takes the title's suggestion for a new one, so an entry that only adds
a date or a note to a saved task leaves it a task. A date more than a year from today is refused, since it
is almost always a mistyped year. A form that fails comes back with every
field as it was, the failing field named and focused, and the fold open. An
entry is reviewed the same way before it is saved, and its date is the
family's own claim, which her page names as such.

What is never kept: the heading's first name, grades other than "Missing",
and anything the reader did not understand; a line in the email's shape with
another grade is left for review. A teacher's name inside an instruction is
kept with it, as the portal shows it, since the instruction is the
assignment's.

A plan is made from the week as it stands. When work is saved into a waiting
plan's window, or a date, a type, a note, a status the school reports, or
what a source says about a date, or which of the school's instructions
apply, changes there, both pages say so, "Assignments changed after this plan was
made", and the plan is not approved as it stands; plan again. Saving what is
on record already, or work due well past the plan's week, changes nothing
the plan was made from, and a plan a parent has decided is history, measured
against the week no more. A saving and a decision never cross: the saving
waits the moment a decision takes, and a decision after a saving is refused
as stale. A household file from before this schema is brought
up to it on the first start: columns it lacks are added, the index a
version between made on the claims, which held each claim once, is dropped
so that every observation is kept, and status reports the file holds twice
for one channel, status, and day are folded to the first before the index
that keeps them once is made. No assignment and no claim is dropped or
rewritten. A school note in the old note field, from a file written before
the school's instructions were kept apart, is moved into them once, in the
same transaction: each is kept, checked to be kept exactly once, and only
then cleared from the field with its mark; her notes and a parent's stay
where they are. A school note found in the old field later, beside
instructions already kept, waits for a parent's review, and the family page
says so with a link to the review. The steps in "Upgrading and rolling back"
come first.

## The sample week

`data/sample/` is a second synthetic set for showing Blossom: four ordinary
assignments in the school week of September 7, 2026, each with one date from
the school portal and nothing disputing it, so the opening view is a plain
week rather than the awkward cases the main fixtures exist to exercise. Its
launch file, `data/sample/sample.env`, points the fixture path at it, pins
the clock to Monday the seventh, puts the three state files under
`.local/sample/`, and sets `BLOSSOM_SAMPLE=1`, which marks both pages
"Sample week". Load it after `.env`, so the household's time zone and the
model key come from there and nothing in it touches the family's own state:

```bash
uv run --env-file .env --env-file data/sample/sample.env uvicorn blossom.app:app --reload
```

The README's [Start the sample](../README.md#try-it-locally) block does the
same loading in PowerShell without uv: the household's `.env`, or the
example when there is none, followed by the sample's paths and clock. The
pip instructions below also cover macOS and Linux. Neither Python nor the
app reads `.env` automatically.

The variables last for that PowerShell window. Use a fresh window when
switching from the sample to the household's own settings. Without a key in
`.env` the pages work and the plan button is not offered.

To start the sample again from nothing, stop the app and delete the
`.local/sample/` folder; the family's own state under `.local/` is untouched,
and the next launch creates the folder again. The sample's assignments live in
that folder's file too, read from `data/sample/` when the launch creates it,
so a change to the set shows once the folder is deleted, and a sample folder
from before the record lived in its file has no assignments until it is.

Her page shows two items due that week as active cards, each saying its
date is from the school portal, the signed syllabus folded under "Reported
done (1)" as her own update, and the reading log, assigned that Monday and
due the next, under "Assigned this week, due later"; the following week shows
the same reading log as due. The syllabus update is the one report the set
carries, in `data/sample/student_reports.json`, seeded with the assignments
when the sample's file is created; the main fixtures carry none. Deleting the
sample folder starts it again with that one update and nothing she has done
since. A plan made for that evening lists no dates to clarify,
because nothing is missing or contested. "Previous week" shows an empty week,
and the main fixtures under `data/synthetic/` keep the disagreement, the
contradiction, and the undated form for when those are the point.

`data/sample/prepared_plan.md` is a written plan for the same evening, for
the case where live planning is not available while the sample is shown. It
was not made by the planner and no review of it has run, and it says so at
the top. Show it as prepared, never as a plan the system made or one that
passed its checks; the app has no way to serve it as a plan, by design.

## When uv cannot download

Depending on your network and certificate settings, uv can have trouble
downloading packages even when pip works. If that happens, or you'd rather
use the tools you already have, Python and pip are enough to run Blossom. The
[README](../README.md#try-it-locally) gives the full Windows PowerShell path,
including creating the environment and loading the sample settings.

For macOS and Linux, run these once from the repository folder, with Python
3.12 or 3.13 installed. Use `python3.13` in the first line if that is the
version you have:

```bash
python3.12 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e .
```

Paste the following block from the Blossom folder to start the sample. Use
it again whenever you want to start the app. It reads your `.env` if you've
made one, or `.env.example` if you haven't, then loads the sample settings
so your changes stay under `.local/sample/`. Like the PowerShell version,
it reads plain `KEY=value` lines and allows spaces in passphrases. Enter
values without surrounding quotes.

```bash
.venv/bin/python - <<'PYTHON'
import os
import sys
from pathlib import Path

settings = Path(".env") if Path(".env").exists() else Path(".env.example")
for path in (settings, Path("data/sample/sample.env")):
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().isidentifier():
            os.environ[key.strip()] = value.strip()

os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "blossom.app:app", "--reload"])
PYTHON
```

Open <http://127.0.0.1:8000/student/due-this-week> or
<http://127.0.0.1:8000/parent>. Press Ctrl+C to stop. For the optional planner,
copy `.env.example` to `.env` if you do not already have one, set
`ANTHROPIC_API_KEY` in your editor, and repeat the launch block. An existing
`.env` is never overwritten by the launcher.

Blossom doesn't read `.env` on its own; the launch block does that for you.
Uvicorn also has an `--env-file` option, but it needs an extra package,
`python-dotenv`, that Blossom doesn't install. The blocks above work without
it. If an earlier uv attempt left an environment without pip, the `ensurepip`
step puts it back. You don't need to activate the environment on either
platform.

If you are contributing code, install the development tools as well. These
are not needed just to try the app.

Windows (PowerShell):

```powershell
.\.venv\Scripts\python -m ensurepip --upgrade
.\.venv\Scripts\python -m pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy blossom tests
.\.venv\Scripts\python -m pytest
```

macOS and Linux:

```bash
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy blossom tests
.venv/bin/python -m pytest
```

Keep the version pins matching `pyproject.toml`; a test checks that they do.
Pip resolves transitive dependencies itself, so those versions can differ
from `uv.lock`. Run the checks through the environment's own Python rather
than `uv run`, which would try the download that failed.

## Troubleshooting

**The app refuses to start and names `BLOSSOM_TIMEZONE`.** The household's
time zone has no default. Start with `--env-file .env.example`, or set
`BLOSSOM_TIMEZONE` to an IANA key such as `America/New_York` in `.env` or in
the shell.

**The weekly page is empty.** With no fixture named, the record starts empty
and stays so until a parent adds assignments from the family page, under
"Add assignments"; an empty page with no fixture and nothing pasted yet is
the expected shape.
To see the synthetic set, load the sample week's launch file, or name the set
and pin the clock to its week, `BLOSSOM_FIXTURE_PATH=data/synthetic` and
`BLOSSOM_TODAY=2026-08-19`, into a state folder of its own, since a fixture is
read only into a file the start creates. With the clock unpinned, "this week"
is the real week, and the only item of the set in every week is the one with
no due date.

**A waiting plan says assignments changed.** Something in the plan's window
changed after the plan was made: work saved from the family page, or a date,
a type, a note, or what a source says about a date. The plan stays as
history, but it does not cover the week as it stands, so approving it is
refused. Plan again and review the new plan.

**The app refuses to start and says saved state may not live on a network
share or in a synced folder.** The repository is inside a folder that
OneDrive, Dropbox, iCloud Drive, or Google Drive syncs, or on a mapped drive,
and the files the app writes would go there with it. Point
`BLOSSOM_DATABASE_PATH`, `BLOSSOM_CHECKPOINT_PATH`, and `BLOSSOM_TRACE_PATH`
at an ordinary local folder instead, in `.env` or in the shell, for example
`C:\blossom-state\blossom.sqlite3` on Windows or
`~/blossom-state/blossom.sqlite3` elsewhere, and the same folder for the
other two.

**The app refuses to start and says another Blossom process has this
household's files open.** One process serves a household; the file it names,
`blossom.lock` beside the drafts file or `checkpoints.lock` beside the
saved-state file, is held by the process already running, and released when
that process stops. Stop the other server, or point this one
at other files with the three path variables above. A process that was killed
releases the lock on its own, so nothing needs deleting.

**The parent's page says no API key is configured.** That is the state the
first run is meant to be in: the queue and the decisions work, and only
starting a new plan needs a key. To plan for real, see above.

**Windows: `BLOSSOM_TODAY=2026-08-19 uv run ...` says the command is not
recognized.** That is bash syntax. In PowerShell use
`$env:BLOSSOM_TODAY = "2026-08-19"` on its own line first, or just use
`--env-file .env.example`.

**Port 8000 is already in use.** Add `--port 8765` (or any free port) to the
uvicorn command and open that port instead.

**The editor says `fastapi` or `pydantic` cannot be found.** The editor is
using a different interpreter than the one in `.venv`. In VS Code, run
**Python: Select Interpreter** from the command palette and pick the one
under `.venv`.

**mypy or ruff in the editor disagrees with CI.** The editor is running the
copy bundled with its extension rather than the pinned one. The repository's
`.vscode/settings.json` points both extensions at the versions in `.venv`, so
selecting that interpreter fixes it.
