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

## Why this exists

Blossom is a homework and deadline tracker I am building for my teenage
daughter. She named it.

I want it to help her see what is coming, make room for the work, and ask for
help before an evening becomes a scramble. Her parents are collaborators.
She should end up with her own way of planning, with support she can
understand and choose to use.

The information we have is often thin: a title, a date, and perhaps a note
to check a textbook. Sometimes the dates disagree. Blossom keeps those
differences visible and helps plan with the information available. Setting
aside time for an assignment does not mean it knows how long finishing it
will take.

I intend to define what success looks like with her rather than on her behalf.

## What works today

Blossom is an early project for one household. Assignments live in the
household's own database, put there from the school portal's own text pasted
by a parent, or typed by hand; the bundled synthetic records seed it for the
sample and the tests. It does not connect to the school platform or read a
mailbox.

**My week.** Browse the school week, see due dates and their sources, and keep
work assigned this week but due later in sight. Missing dates stay visible;
disagreements are marked without quietly replacing the recorded date.

**Plan today.** Ask for an evening plan with time set aside for work and a
reason for each choice. Blossom checks and reviews the proposal, with up to
two revisions. The plan is available immediately, including any unresolved
review concerns. A parent can review it without blocking her from using it.

<p align="center">
  <img src="docs/assets/student-week.png" alt="Blossom's student view with planning and help controls above the first assignment" width="640">
</p>

*A synthetic sample week with a prepared plan. Open "View today's plan" to
read the time set aside for the evening.*

**Too much right now.** One press, with no rating or explanation required.
It sets the shorter budget for the next plan and offers "Make a smaller
plan". The existing plan stays unchanged until she requests another one.
She can take the signal back.

**Ask for help.** Send a request to the family review page, with an optional
note. A parent can respond and mark it resolved. Refresh either page to see
updates; opening the parent's page does not count as a response.

<p align="center">
  <img src="docs/assets/family-help.png" alt="Family review showing a student's request for a signature and a parent's reply" width="640">
</p>

*A synthetic help request and reply. Family review puts help she asked for first.*

**Add assignments.** On the family page, paste the school portal's homework
page, its weekly summary, or its "Missing" email as text, or enter one
assignment by hand; only the course and title are required. A review page
shows what was read, week by week, Monday to Sunday, with a count of what is
new, what updates a saved assignment, and what is saved already, and each
card says exactly what saving would do: save it as new, put the pasted date
beside the saved one for her page to show, fill in a date or a note the
record lacks, or nothing. The record keeps where each fact came from, the
portal, the school's email, or a parent, so her page can say so. Work that
comes round again under the same name, a weekly practice, is asked about
rather than merged. The school's "Missing" email is kept as what the school
reported, with the day; the date the email writes beside an assignment is
kept as text, never as a due date. Lines the reader did not take are listed
as text that needs review. Nothing is written until "Save" is pressed,
pasting the same week twice adds nothing twice, and a plan waiting for
review says so when the week it was made from changes.

**Her own update.** On each card she can say Done, meaning she has finished
her part, or Not yet, with a note if she wants one. Done takes the assignment
out of the work to plan and folds its card under the active ones; it does not
turn work in, and the school's record stays separate. She can change or undo
an update, a fold under the card keeps the history of her updates and
corrections with their days, and a save from a page that another device has
moved past is shown the newer update first. Her updates and notes are visible
on the family page, and a Not yet on work being planned goes to the planner
with her note. A plan that includes work she reports as Done says so on both
pages, and where her Done stands beside a school report of Missing, from any
one of the school's channels, the family page lists it as worth checking
together, with what each channel says and the day it said it.

**Mark checked.** Where her Done stands beside a school report of Missing,
the family page offers Mark checked, with a note for her card if a parent
wants one. It records that a parent checked that discrepancy with her, here
and nowhere else: it does not turn work in, change her update, or change the
school's report, and it sends nothing to the school. The row folds under
Checked recently with the day, her card shows the day and the note, and if
the work turns out unfinished she changes her update to Not yet. Check again
reopens the check, and the earlier one stays in the record. A check is made
against what the row showed: her Done, the report that began it, and each
school statement of Missing. A Done after a Not yet, or a Missing the school
had not reported before, puts the row back among those worth checking and
says what differs; the same report pasted again does not, nor does a change
to her note alone. A check whose facts moved stays on its row as the check
that was made, with its day, its note, and what differs now. Two devices
acting on one row get one record and one refusal that shows what stands now,
the note on record and the note typed both; Check again is refused the same
way when her update or the school's report moved after its page was made.

**A saved plan she can still follow.** A plan is saved as the text she reads
and, beside it, as data: the plan itself with the title, course, and due date
of each assignment as the run read them. Today's saved plan is unfolded on
her page on every visit, ahead of everything about help, which stays one link
away from the controls. Both pages read a plan by its rows, each naming its
assignment by id and linking to that assignment's details. When she reports
an assignment Done, every block for it on today's plan says she can skip it,
with the day of her report, and an item that was put off says it is out of
work to plan. The times, the reasons, and a parent's review stay as they
were: nothing is rescheduled, no model is asked, and no new plan is made. A
change back to Not yet takes the marks away. Only today's latest plan shows
her updates, whatever a parent decided; every other plan is history and reads
as it was saved, with the text as composed kept in a fold. A plan made before
plans were saved as data reads as its saved text, without links or marks. An
assignment's details show its record as it stands now: what it is and its
date first, then her update through the same form her cards use, then the
longer evidence, with every claim a source has made about its date, and the
history. What a save did is said at the update, with a way back to the week,
the plan, or the family page she came from, and the way back to today's plan
lands on whichever plan is today's when it is followed; a parent reads them
and cannot report in her name.

**Household sign-in.** Two passphrases in `.env`, hers and a parent's, guard
the pages: hers opens her week, a parent's opens both pages, and a device
stays signed in for a month or until "Sign out". The sign-in is a cookie
signed with a secret Blossom keeps beside its database. With neither
passphrase set the pages are open, which is right for trying it on your own
machine and nowhere else. The
[development guide](docs/development.md#running-for-the-household) covers
running it for the household's devices on the home network.

Blossom does not contact the school or submit work. The
[architecture notes](docs/architecture.md) describe the design and what is
still unimplemented.

## Try it locally

You need Git, Python 3.12 or 3.13, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Browsing the
sample week and trying the help flow need no API key or school account.
Generating a plan needs an Anthropic key; the screenshot above shows a
prepared scenario.

```bash
git clone https://github.com/gbodegas/blossom.git
cd blossom
uv sync --dev
uv run --env-file .env.example --env-file data/sample/sample.env uvicorn blossom.app:app --reload
```

Open [My week](http://127.0.0.1:8000/student/due-this-week) or
[Family review](http://127.0.0.1:8000/parent). The sample is pinned to September
7, 2026: three assignments are due that week, and a reading log is assigned
that week but due the next. Both pages say "Sample week".

The sample's saved state stays under `.local/sample/`. The launch files also
set an example household time zone and the normal and shorter evening
budgets. The [development guide](docs/development.md#the-sample-week) covers
these settings, the separate fixtures with conflicting dates, resetting the
demo, and a clearly labeled written plan for showing the idea without a key.
It also covers installation troubleshooting and the pip fallback.

## Using the planner

Planning is optional and makes paid requests to Anthropic. Copy `.env.example`
to `.env`, put your key in `ANTHROPIC_API_KEY`, and load it instead of the
example file. Keep the sample file last to continue using synthetic data:

```bash
uv run --env-file .env --env-file data/sample/sample.env uvicorn blossom.app:app --reload
```

Press "Plan today" on her page. Requests include the assignments still to
do and their date sources, her Not yet updates on that work with any note she
wrote, household rules, the planner's notes about earlier plans, and whether
she has said the evening is too much; work she has reported done is left out.
Later calls also include the proposed plan and feedback on it. A run that
reaches the planner makes one to six model calls. A run that finds nothing
left to schedule when it reads the week, everything in its window reported
done, makes none, and the pages refuse such an evening before a run starts
whenever they can. Approving a plan makes no additional model request.

Local traces retain complete prompts and answers. The
[development guide](docs/development.md) explains storage, retention, and
configuration; the [architecture notes](docs/architecture.md) describe the
checks and their limits.

## Contributing

Bug fixes, tests, accessibility improvements, and small engineering changes
are welcome. Blossom is built for one household. Please discuss larger
features in an issue first.

Use synthetic data only. Fixtures live in `data/sample/` and
`data/synthetic/`; never include real student or family data. New third-party
imports need a justification in the project's allowlist.

Run the same checks as CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy blossom tests
uv run pytest
```

If you are considering something similar for your own family, the part I
would carry over is the habit of asking, for every capability, whether the
person it is built for would consent to it existing.

## License

[MIT](LICENSE).
