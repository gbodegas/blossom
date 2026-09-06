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

I do not have a success metric yet. Success looks like the agent making her
own tracking legible to her rather than replacing it, and I intend to define
what that means with her rather than on her behalf.

## What works today

Blossom is early, and it is built for one household. Two things work end to
end.

**Her week.** A page lists everything due in the coming days, each item
labeled with how well its due date is corroborated: by several sources, by
one, by sources that disagree, or by nothing. Nothing is left off for being
doubtful. The bundled data is a fictional student's week with the awkward
cases included on purpose: a form to sign with no due date, a book to cover
whose date the portal gives two ways, and a quiz the family's record puts
next week while the portal puts it this Friday. When no source supports the
record, the page says the school disagrees, and a plan is held to the
earliest date anyone gives.

**A parent's review.** From a second page, a parent asks for an evening's
plan. A planner proposes one, deterministic checks hold it to the record, a
critic reviews it, and a plan that fails goes back for revision, up to two
more times. The draft then waits on the page with an account of how it was
made, and the parent approves or refuses it. Drafts, decisions, and the record
of each run survive a restart.

Around that sit the guardrails. Blossom has no tool for emailing a teacher,
messaging her, or submitting schoolwork, so there is nothing for a model to
call; everything it writes stops in a draft that a person sends by hand.
Planning does talk to a model, which is a request to Anthropic; the planner
section says what goes in it. The agent reflects on its own performance, and
the store refuses any reflection whose subject is not the system. That checks
the label, not the words: a note filed under the system could still be about
her, and nothing yet reads the text to tell.

Two things the design calls for are not built. There is no login: the
student, parent, and verifier views are separate pages, not separate people,
and anyone who can reach the server can open all of them, so run it on your
own machine. And the "too much" control is accepted and changes nothing yet.
[docs/architecture.md](docs/architecture.md) lists every gap between the
design and the code.

## Try it locally

You need Git, Python 3.12 or 3.13, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). No API key and
no school platform access are needed.

```bash
git clone https://github.com/gbodegas/blossom.git
cd blossom
uv sync --dev
uv run --env-file .env.example uvicorn blossom.app:app --reload
```

Then open <http://127.0.0.1:8000/student/due-this-week>. It should show seven
assignments, one with a banner saying the school disagrees with the record.

`.env.example` sets the household's time zone, which has no default because
"due this week" means the days you live in, and pins the clock to August 19,
2026, the week the fixtures are written for. The week is a rolling window,
that day and the six after it, plus anything with no due date; on the real
clock the page would show only the undated form.

The parent's page is at <http://127.0.0.1:8000/parent>, and
<http://127.0.0.1:8000/docs> lets you drive every route directly. Without an
API key the parent's page says a plan cannot start, and everything else
works.

The app writes a few files under `.local/`: the drafts and decisions, the
saved state of a run paused for review, and a trace of each run that holds
complete prompts and answers. What each holds, how long it is kept, and how
to reset the demo are in [docs/development.md](docs/development.md), with the
pip fallback, editor setup, and troubleshooting. If startup refuses because
the repository sits in a synced folder such as OneDrive, that guide says which
paths to point elsewhere.

## Using the planner

The planner is optional, and it costs money. To run it for real, copy
`.env.example` to `.env`, put an Anthropic API key in `ANTHROPIC_API_KEY`,
start the app with `--env-file .env` instead, and use "Plan it" on the
parent's page.

Each run sends requests to Anthropic. They carry the week's assignments with
their courses, dates, and confidence labels, the household's standing rules,
the planner's notes about past plans, and, on later calls, the proposed plan
and what was wrong with it. With the bundled fixtures all of that is
synthetic. A run is one to six calls depending on revisions, and the cost
varies with the week. All of it happens before the plan reaches the page, and
approving a draft sends nothing anywhere. The model, the call routing, and the
bounds on a run are in [docs/development.md](docs/development.md) and
[docs/architecture.md](docs/architecture.md).

## Contributing

Blossom is built for one household, and that shapes what fits. Bug fixes,
tests, accessibility improvements, and narrowly scoped engineering changes
are welcome. Features that push it toward a general family-management
platform are not the direction, and I would rather discuss an idea in an
issue first than review a large pull request cold. No contribution may
contain real student or family data; the fictional student in
`data/synthetic/` is the only data that belongs here. New third-party imports
are constrained on purpose: a test refuses any that is not on a justified
allowlist.

If you are reading this because you are considering something similar for
your own family, the part I would carry over is not the architecture. It is
the habit of asking, for every capability, whether the person it is built for
would consent to it existing.

To run the same checks CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy blossom tests
uv run pytest
```

## License

[MIT](LICENSE).
