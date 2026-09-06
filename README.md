<p align="center">
  <img src="blossom/static/mark.svg" alt="Blossom" width="200">
</p>

<h1 align="center">Blossom</h1>

<p align="center">
  A planning assistant to help a student make sense of schoolwork and deadlines.
</p>

<p align="center">
  <a href="https://github.com/gbodegas/blossom/actions/workflows/ci.yml"><img src="https://github.com/gbodegas/blossom/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.12%20%7C%203.13-blue" alt="Python 3.12 or 3.13">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
</p>

---

## Why this exists

Blossom is a homework and deadline tracker I am building for my teenage
daughter. She named it.

Her deadlines come from too many places. The school platform says one thing,
an email notification says something slightly different, I remember a third
version, and she has her own account of it. Every planner I have seen picks
one of those and presents it as the truth. When it picks wrong, she is the one
who pays for it.

Blossom does not pick. When the sources agree, it says so. When they disagree,
it shows her every claim and who made it, and leaves the decision with her.
When nothing confirms a date at all, it says that too, instead of quietly
showing the date anyway.

The second thing it is for is the days when everything is already too much.
Nobody in that state has the capacity to open an app and rate their overwhelm
on a scale from one to ten. Blossom will have a single control that means
"too much right now", as frictionless as squeezing a stress ball, and it will
treat that signal as true the moment it arrives.

## What runs today

The weekly view is real, end to end. The fixture set is a fictional student's
week as a school portal would show it: seven items, including a form to sign
that has no due date, a book to cover whose date the portal gives two
different ways, and a quiz the record puts next week that the portal puts this
Friday. Their sources are reconciled, and each one is labeled with how well
its due date is corroborated: one confirmed by two sources, two resting on a
single source, two where sources disagree, and two nothing corroborates. The
quiz is in the week because the portal puts it there, and the page says the
school disagrees with the record. Every item the week holds is shown, however
poorly its date is corroborated. The same set carries the household's
standing rules about how she works and one note the planner kept about a past
plan, so a run of the planner has both to read.

## What makes it different

- **It cannot send anything to anyone.** Everything Blossom writes stops in a
  draft that a person reads and sends by hand. This is not a rule the agent
  follows. There is no tool for emailing a teacher, messaging her, or
  submitting work, so there is nothing to call. A rule can be argued around
  under a plausible-sounding situation. A tool that was never built cannot be
  called. Planning does talk to a model, which is a request to Anthropic;
  Getting started says exactly what goes in it.
- **It shows disagreement instead of hiding it.** Reconciling sources that
  conflict is the core of the system, not an error path. Every due date on
  screen carries a label saying how well it is corroborated, including the
  ones nothing corroborates.
- **It never studies her.** The agent reflects on its own performance, such
  as learning that its reminders land badly at a certain hour, and the store
  refuses any reflection whose subject is not the system itself. That is a
  check on the label, not on the words: a note filed under the system could
  still be about her, and nothing yet reads the text to tell. The design calls
  for that to be structural, and it is not there yet.
- **Three principals, three views.** She sees her week. A parent sees a
  review page, where the planner's proposal waits to be approved or refused,
  and a checkpoint summary rather than a live feed. A verifier is designed to
  sit between anything the agent writes and anything that leaves the system.
  Their interests genuinely conflict, and the design keeps that conflict
  visible instead of resolving it silently.
- **It says what it expects before it looks.** Before the planner reads what
  the school says about a due date, it states what the family's record says.
  A date the sources do not support is a contradiction, and a contradiction
  changes the plan: the deadline becomes the earliest date anyone gives, the
  planner is told to say the record needs checking, and the draft names it.
  The comparison is typed and deterministic, and "cannot tell" is its own
  verdict rather than a contradiction by default.

## Status

Blossom is early, and it is built for one household. What runs today is the
student's weekly view, the source reconciliation behind it, and the plan graph:
a planner and a critic, each a model call returning one typed value, with
deterministic checks between them and a human gate after them. A plan that
fails the checks or the critic goes back to the planner, up to two more times.
A parent starts a run from a page, reads the draft it produces, and approves
or refuses it there; the drafts, the decisions, and the record of each run are
kept in a file that survives a restart.
Around it are the guardrails that constrain it: the missing send tool, the
reflection boundary, and a tool registry that cannot hold anything that reports
having sent something.

The "too much" signal is accepted and not yet acted on. Retrieval reads its
corpora whole; no vector store is wired. [docs/architecture.md](docs/architecture.md)
lists every gap between the design and the code.

I do not have a success metric yet. Success looks like the agent making her
own tracking legible to her rather than replacing it, and I intend to define
what that means with her rather than on her behalf.

## Install

Blossom needs Git, Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/).
If you do not have a matching Python, uv downloads one on its own.

**Windows** (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS and Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal after installing so `uv` is on your path. Then, on any
platform:

```bash
git clone https://github.com/gbodegas/blossom.git
cd blossom
uv sync --dev
```

That creates `.venv` inside the repository and installs the pinned
dependencies from `uv.lock`. No API key and no school platform access are
needed. Everything runs against the synthetic fixtures in `data/synthetic/`.

## Getting started

Nothing here has a login. The three views are separate pages, not separate
people: anyone who can reach the server can open all of them. Run it on your
own machine, for yourself. The visibility policy the design calls for is not
built.

The fixtures carry due dates in the week of August 19, 2026. "Due this week"
is a rolling window, the chosen day and the six after it, plus anything with
no due date on record, which is in every week. On the real clock most of the
fixtures fall outside the window and the page shows only the syllabus form.
`.env.example` pins the clock to August 19, so the shortest first
run shows all seven items on every platform and shell:

```bash
uv run --env-file .env.example uvicorn blossom.app:app --reload
```

Then open <http://127.0.0.1:8000/student/due-this-week>. It should show seven
assignments, one of them with a banner saying the school disagrees with the
record.

Other things to look at while it runs:

- <http://127.0.0.1:8000/parent> is the parent's page: plan an evening, read
  the draft the planner produced, see how it was made step by step, and
  approve or refuse it. A run that ended without a plan is listed with its
  steps too. Planning needs an API key; reading and deciding do not, so
  without one the page says a plan cannot start and everything else works.
- <http://127.0.0.1:8000/parent/approvals> is the same queue as JSON.
- <http://127.0.0.1:8000/parent/checkpoint> is the parent's checkpoint: a
  summary of status and conflicts, not a live feed. A placeholder, JSON for
  now.
- <http://127.0.0.1:8000/verifier/claims> is the verifier's view: each factual
  claim, the policy it was checked against, and the result. A placeholder: one
  fixed example of the shape, not a report on the latest run. JSON for now.
- <http://127.0.0.1:8000/docs> is the interactive API page, where the parent's
  routes can be driven directly and the "too much" signal sent by posting to
  `/student/workload-signals` with no body; today it is acknowledged and
  discarded.

To run the planner for real, copy `.env.example` to `.env`, put an Anthropic
API key in `ANTHROPIC_API_KEY`, start the app with `--env-file .env`, and use
"Plan it" on the parent's page. That sends requests to Anthropic's API, to the
model `claude-opus-5`. Each request carries the week's assignments with their
courses, dates, and confidence labels, the household's standing rules, and the
planner's notes about past plans; the critic also receives the proposed plan,
and a revision receives what was wrong with the last one. With the bundled
fixtures, all of that is synthetic. A run is one to six calls. The planner is
one; only a plan that passes the checks goes to the critic, which is another;
and a plan that fails the checks or the critic's review goes back to the
planner, up to two more times. A run the model cuts short ends with the call
that failed. What it costs depends on the week and the revisions; the API's
usage page says after a run. All of this happens before the plan reaches the
page for a decision.
Approving it sends nothing anywhere.

The fixture folder and the three files the app writes are configurable through
the `BLOSSOM_*` variables in `.env.example`, and `--env-file .env` reads
whatever is set there; the packaged templates and static assets are not. Each
path has a working default. One variable has no default and must be set:
`BLOSSOM_TIMEZONE`, the household's IANA zone, because "due this week" means
the days you live in and no value is right for everyone. It is set in
`.env.example`, which is why the first run above needs nothing else.
`BLOSSOM_TIMEZONE` and `BLOSSOM_TODAY` can also be set as ordinary environment
variables in your shell if you prefer.

Assignments are read from the fixtures into memory at every start. Three
files under `.local/` outlive it: `blossom.sqlite3` holds the drafts, the
decisions, and the record of every run; `checkpoints.sqlite3` holds a graph's
saved state, including a pause at the approval gate; and `traces.sqlite3`
holds the framework's trace of each run, every node and model call with what
went in and came out, for looking into a run that went wrong. The trace holds
prompts and answers verbatim, so it is swept after two weeks. Saved state is
cleared as soon as a run ends or a decision is made, and a draft nobody
decides within two weeks of its evening is closed as expired. Deleting the
three files resets the demo, and with it every saved draft, decision, run,
and trace.

To run the same checks CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy blossom tests
uv run pytest
```

## Troubleshooting

**The app refuses to start and names `BLOSSOM_TIMEZONE`.** The household's
time zone has no default. Start with `--env-file .env.example`, or set
`BLOSSOM_TIMEZONE` to an IANA key such as `America/New_York` in `.env` or in
the shell.

**The weekly page shows only the syllabus.** The clock is not pinned, so
"this week" is the real week, and the only fixture in every week is the one
with no due date. Start the app with `--env-file .env.example` as shown above,
or pin `BLOSSOM_TODAY` to a day in the fixture week.

**The app refuses to start and says saved state may not live on a network
share or in a synced folder.** The repository is inside a folder that OneDrive,
Dropbox, iCloud Drive, or Google Drive syncs, or on a mapped drive, and the
files the app writes would go there with it. Point `BLOSSOM_DATABASE_PATH`,
`BLOSSOM_CHECKPOINT_PATH`, and `BLOSSOM_TRACE_PATH` at an ordinary local
folder instead, in `.env` or in the shell, for example
`C:\blossom-state\blossom.sqlite3` on Windows or `~/blossom-state/blossom.sqlite3`
elsewhere, and the same folder for the other two.

**The parent's page says no API key is configured.** That is the state the
first run is meant to be in: the queue and the decisions work, and only
starting a new plan needs a key. To plan for real, follow the paragraph in
Getting started.

**`uv sync` fails with `HandshakeFailure` or a TLS error.** On a network that
routes Python packages through an internal proxy or index, `uv` may not pick
up the configuration that `pip` already has. The quickest fallback is to
build the environment with pip. If `uv sync` already created `.venv` before
failing, that venv has no pip yet, so bootstrap it first. Pip is always run
through the venv's own Python, because the executable it installs is named
differently from one system to the next. Uvicorn's own `--env-file` needs a
package this project does not install, so the fallback sets the two variables
the first run needs in the shell instead.

Windows (PowerShell):

```powershell
.\.venv\Scripts\python -m ensurepip
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
$env:BLOSSOM_TIMEZONE = "America/New_York"
$env:BLOSSOM_TODAY = "2026-08-19"
.\.venv\Scripts\python -m uvicorn blossom.app:app --reload
```

macOS and Linux:

```bash
.venv/bin/python -m ensurepip
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
BLOSSOM_TIMEZONE=America/New_York BLOSSOM_TODAY=2026-08-19 .venv/bin/python -m uvicorn blossom.app:app --reload
```

If there is no `.venv` yet, create one first with `python -m venv .venv` using
a Python 3.12 or 3.13 interpreter. Keep the version pins matching
`pyproject.toml`; a test checks that they do. Pip resolves the rest of the
dependencies on its own, so their versions can differ slightly from
`uv.lock`. Run the checks through the same Python, as
`.venv/bin/python -m pytest` and so on, rather than through `uv run`, which
would try the download that failed.

**`uv` is not recognized after installing.** Open a new terminal. The
installer updates your path for new shells only. If that does not help,
`pip install uv` works too.

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

## A note on data

No real family data belongs in this repository, and none is checked in.
Everything in `data/synthetic/` describes a fictional student. The
support-rules corpus is held as operational instructions rather than
clinical descriptions, a decision made for privacy reasons that turned out to
serve retrieval as well, since an instruction is a self-contained unit of
meaning and a clinical description is not.

If you are reading this because you are considering something similar for
your own family, the part I would carry over is not the architecture. It is
the habit of asking, for every capability, whether the person it is built for
would consent to it existing.

## Contributing

Blossom is built for one household, and that shapes what fits. Bug fixes,
tests, accessibility improvements, and narrowly scoped engineering changes
are welcome. Features that push it toward a general family-management
platform are not the direction, and I would rather discuss an idea in an
issue first than review a large pull request cold. No contribution may
contain real student or family data; the synthetic fixtures are the only
data that belongs here. New third-party imports are constrained on purpose:
a test walks the package and refuses any import that is not on a justified
allowlist, so adding a dependency means adding it there with the reason.

## Architecture

[docs/architecture.md](docs/architecture.md) describes what the code does
today: the three principals and their view models, the ways the missing send
path is enforced, how sources are reconciled, what a plan is and how the graph
that proposes one is bounded, and where the code still falls short of the
design. The design notes behind those decisions are kept outside this
repository.

## License

[MIT](LICENSE).
