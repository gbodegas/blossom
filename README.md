<p align="center">
  <img src="blossom/static/mark.svg" alt="Blossom" width="200">
</p>

<h1 align="center">Blossom</h1>

<p align="center">
  A planning assistant for a student whose deadlines don't agree with each other.
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

The weekly view is real, end to end. The fixture set is a fictional
student's week as a school portal would show it: six items, including a form
to sign that has no due date and a book to cover whose date the portal gives
two different ways. Their sources are reconciled, and each one is labeled with
how well its due date is corroborated: one confirmed by two sources, one
resting on a single source, two where sources disagree, and one nothing
corroborates. Nothing is filtered out. The same set carries the household's
standing rules about how she works and one note the planner kept about a past
plan, so a run of the planner has both to read.

## What makes it different

- **It cannot send anything.** Everything Blossom writes stops in a draft that
  a person reads and sends by hand. This is not a rule the agent follows. There
  is no send tool for it to call. A rule can be argued around under a
  plausible-sounding situation. A tool that was never built cannot be called.
- **It shows disagreement instead of hiding it.** Reconciling sources that
  conflict is the core of the system, not an error path. Every due date on
  screen carries a label saying how well it is corroborated, including the
  ones nothing corroborates.
- **It never studies her.** The agent will reflect on its own performance,
  such as learning that its reminders land badly at a certain hour. It will
  be structurally unable to write a reflection about her.
- **Three principals, three views.** She sees her week. A parent sees a
  review page, where the planner's proposal waits to be approved or refused,
  and a checkpoint summary rather than a live feed. A verifier is designed to
  sit between anything the agent writes and anything that leaves the system.
  Their interests genuinely conflict, and the design keeps that conflict
  visible instead of resolving it silently.
- **It will say what it expects before it looks.** The agent's reasoning loop
  is designed to state what it expects from a tool call before making it.
  That is what turns a surprising result into a noticed contradiction rather
  than a confidently wrong answer. Today the scaffold records the expectation
  but cannot yet act on a mismatch.

## Status

Blossom is early, and it is built for one household. What runs today is the
student's weekly view, the source reconciliation behind it, and the plan graph:
a planner and a critic, each a single model call returning one typed value,
with deterministic checks between them and a human gate after them. A parent
starts a run from a page, reads the draft it produces, and approves or refuses
it there; the drafts and decisions are kept in a table that survives a restart.
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

Blossom needs Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/). If you
do not have a matching Python, uv downloads one on its own.

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

The fixtures carry due dates in the week of August 19, 2026, and "due this
week" is computed from the real clock, so on any other week the page is
legitimately empty. `.env.example` pins the clock to that week, so the
shortest first run is the same on every platform and shell:

```bash
uv run --env-file .env.example uvicorn blossom.app:app --reload
```

Then open <http://127.0.0.1:8000/student/due-this-week>.

Other things to look at while it runs:

- <http://127.0.0.1:8000/parent> is the parent's page: plan an evening, read
  the draft the planner produced, and approve or refuse it. Planning needs an
  API key; reading and deciding do not, so without one the page says a plan
  cannot start and everything else works.
- <http://127.0.0.1:8000/parent/approvals> is the same queue as JSON.
- <http://127.0.0.1:8000/parent/checkpoint> is the parent's checkpoint: a
  summary of status and conflicts, not a live feed. A placeholder, JSON for
  now.
- <http://127.0.0.1:8000/verifier/claims> is the verifier's view: each factual
  claim, the policy it was checked against, and the result. JSON for now.
- <http://127.0.0.1:8000/docs> is the interactive API page, where the parent's
  routes can be driven directly and the "too much" signal sent by posting to
  `/student/workload-signals` with no body; today it is acknowledged and
  discarded.

To run the planner for real, copy `.env.example` to `.env`, put an Anthropic
API key in `ANTHROPIC_API_KEY`, start the app with `--env-file .env`, and use
"Plan it" on the parent's page. Each run makes two model calls, a planner and
a critic, against the synthetic fixtures; it costs a few cents and sends only
the fixture assignments. The plan stops at the page for a decision. Nothing is
sent anywhere by approving it.

Every path the app uses is configurable through the `BLOSSOM_*` variables in
`.env.example`, and `--env-file .env` reads whatever is set there. Each path has a working default. One
variable has no default and must be set: `BLOSSOM_TIMEZONE`, the household's
IANA zone, because "due this week" means the days you live in and no value is
right for everyone. It is set in `.env.example`, which is why the first run
above needs nothing else. `BLOSSOM_TIMEZONE` and `BLOSSOM_TODAY` can also be
set as ordinary environment variables in your shell if you prefer.

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

**The weekly page is empty.** The clock is not pinned, so "this week" is the
real week and the fixtures fall outside it. Start the app with
`--env-file .env.example` as shown above.

**The parent's page says no API key is configured.** That is the state the
first run is meant to be in: the queue and the decisions work, and only
starting a new plan needs a key. To plan for real, follow the paragraph in
Getting started.

**`uv sync` fails with `HandshakeFailure` or a TLS error.** On a network that
routes Python packages through an internal proxy or index, `uv` may not pick
up the configuration that `pip` already has. The quickest fallback is to
build the environment with pip. If `uv sync` already created `.venv` before
failing, that venv has no pip yet, so bootstrap it first.

Windows:

```bash
.venv\Scripts\python -m ensurepip
.venv\Scripts\pip install -e .
.venv\Scripts\pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
.venv\Scripts\python -m uvicorn blossom.app:app --reload --env-file .env.example
```

macOS and Linux:

```bash
.venv/bin/python -m ensurepip
.venv/bin/pip install -e .
.venv/bin/pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
.venv/bin/python -m uvicorn blossom.app:app --reload --env-file .env.example
```

If there is no `.venv` yet, create one first with `python -m venv .venv` using
a Python 3.12 or 3.13 interpreter. Keep the version pins matching
`pyproject.toml`; a test checks that they do.

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
data that belongs here.

## Architecture

[docs/architecture.md](docs/architecture.md) describes what the code does
today: the three principals and their view models, the ways the missing send
path is enforced, how sources are reconciled, what a plan is and how the graph
that proposes one is bounded, and where the code still falls short of the
design. The design notes behind those decisions are kept outside this
repository.

## License

[MIT](LICENSE).
