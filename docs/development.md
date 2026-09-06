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
  parent's review under it once there is one; then her week, every
  assignment labeled with how well its due date is corroborated.
- <http://127.0.0.1:8000/student/plans/today> is today's plan as JSON; a POST
  to `/student/plans` makes one.
- <http://127.0.0.1:8000/parent> is the parent's page: read the plan she has,
  see how it was made step by step, and say it looks good or ask for a
  change. A parent can also start an evening's plan for her there. A run that
  ended without a plan is listed with its steps too. Planning needs an API
  key; reading and reviewing do not, so without one either page says a plan
  cannot start and everything else works.
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

Nothing here has a login. The three views are separate pages, not separate
people, and anyone who can reach the server can open all of them. The
visibility policy the design calls for is not built.

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

The fixtures carry due dates in the week of August 19, 2026. "Due this week"
is a rolling window, the chosen day and the six after it, plus anything with
no due date on record, which is in every week. On the real clock most of the
fixtures fall outside the window and the page shows only the syllabus form;
pinned to August 19, it shows all seven items.

## What survives a restart

Assignments are read from the fixtures into memory at every start. Three
files under `.local/` outlive it:

- `blossom.sqlite3` holds the drafts, the decisions about them, and the
  record of every run, one line per node saying what it expected and found.
  Kept for the school year. A draft nobody decides within two weeks of its
  evening is closed as expired, and one she plans again over is closed as
  superseded, so one plan waits per evening. Her "too much" signals live here too, with
  any words she added, kept for a week and removable from her page.
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

## Running the planner for real

Copy `.env.example` to `.env`, put an Anthropic API key in
`ANTHROPIC_API_KEY`, start the app with `--env-file .env`, and use "Plan it"
on the parent's page. The model is `claude-opus-5`, at high effort for the
planner and medium for the critic, with each answer capped at 16,000 tokens.
The endpoint is fixed in code, so no shell variable can change where a prompt
is sent.

Each request carries the week's assignments with their courses, dates, and
confidence labels, the household's standing rules, and the planner's notes
about past plans; the critic also receives the proposed plan, and a revision
receives what was wrong with the last one. With the bundled fixtures, all of
that is synthetic. A run is one to six calls. The planner is one; only a plan
that passes the checks goes to the critic, which is another; and a plan that
fails the checks or the critic's review goes back to the planner, up to two
more times. A run the model cuts short ends with the call that failed. What
it costs depends on the week and the revisions; the API's usage page says
after a run. All of this happens before the plan reaches the page for a
decision, and approving the draft sends nothing anywhere.

## When uv cannot download

On a network that routes Python packages through an internal proxy or index,
`uv sync` can fail with `HandshakeFailure` or a TLS error while `pip` already
has the configuration it needs. The fallback is to build the environment with
pip. If `uv sync` created `.venv` before failing, that venv has no pip yet,
so bootstrap it first. Pip is always run through the venv's own Python,
because the executable it installs is named differently from one system to
the next. Uvicorn's own `--env-file` needs a package this project does not
install, so the fallback sets the two variables the first run needs in the
shell instead.

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

If there is no `.venv` yet, create one first with `python -m venv .venv`
using a Python 3.12 or 3.13 interpreter. Keep the version pins matching
`pyproject.toml`; a test checks that they do. Pip resolves the rest of the
dependencies on its own, so their versions can differ slightly from
`uv.lock`. Run the checks through the same Python, as
`.venv/bin/python -m pytest` and so on, rather than through `uv run`, which
would try the download that failed.

## Troubleshooting

**The app refuses to start and names `BLOSSOM_TIMEZONE`.** The household's
time zone has no default. Start with `--env-file .env.example`, or set
`BLOSSOM_TIMEZONE` to an IANA key such as `America/New_York` in `.env` or in
the shell.

**The weekly page shows only the syllabus.** The clock is not pinned, so
"this week" is the real week, and the only fixture in every week is the one
with no due date. Start the app with `--env-file .env.example`, or pin
`BLOSSOM_TODAY` to a day in the fixture week.

**The app refuses to start and says saved state may not live on a network
share or in a synced folder.** The repository is inside a folder that
OneDrive, Dropbox, iCloud Drive, or Google Drive syncs, or on a mapped drive,
and the files the app writes would go there with it. Point
`BLOSSOM_DATABASE_PATH`, `BLOSSOM_CHECKPOINT_PATH`, and `BLOSSOM_TRACE_PATH`
at an ordinary local folder instead, in `.env` or in the shell, for example
`C:\blossom-state\blossom.sqlite3` on Windows or
`~/blossom-state/blossom.sqlite3` elsewhere, and the same folder for the
other two.

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
