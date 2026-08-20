<p align="center">
  <img src="docs/assets/blossom-mark.png" alt="Blossom" width="160">
</p>

<h1 align="center">Blossom</h1>

<p align="center">
  A shared academic planning assistant built around three principals who do not have equal authority over it.
</p>

---

Blossom is a planning and coordination assistant for a family, and the primary user is my thirteen year old daughter, who also named it. That single fact is the constraint that shapes almost every design decision in here. A system that a family member depends on has to be trustworthy, and most of the interesting work has been figuring out what that means in code rather than in prose.

## What it does

The system tracks assignments, deadlines, project milestones, and application requirements across sources that routinely disagree with each other. A school LMS reports a submission flag, an email notification says something slightly different, a parent remembers a third thing, and the student has her own account of it. Reconciling those channels is not error handling bolted onto the side. It is the core problem, and treating it as a first class concern is what lets the agent notice a discrepancy rather than confidently assert a wrong deadline.

The second thing it does is signal workload without asking the user to articulate distress. A student in the middle of executive dysfunction does not have the capacity to open an app and rate her overwhelm on a scale. The signal has to be as frictionless as squeezing a stress ball, and the system treats that signal as authoritative when it arrives.

## The design in brief

**Three principals, not one user with role flags.** The student is the primary user. A parent gets a checkpoint view rather than a live feed. A verifier layer sits between generation and anything that leaves the system. These are separate view models with separate route trees, because collapsing them into a permission check on a shared view is the easier build and the wrong one. Their interests genuinely conflict, and the design should make that conflict visible rather than resolve it silently.

**There is no send tool.** The drafts only constraint is implemented as an absent capability rather than a behavioral rule. A rule can be argued around under a plausible sounding situation. A tool that was never built cannot be called. Everything outbound terminates in a draft that a human transmits by hand.

**Three stores, two retrieval mechanisms, one router.** Project state is small, structured, and queried by exact criteria, so retrieving a due date by semantic similarity would return the most similar assignment rather than the correct one. Operational support rules and accumulated self reflections are natural language guidance with no key to look them up by, so they are identifiable by resemblance rather than by field. The router selects the mechanism based on whether the query has a lookup key, and every result carries provenance and a timestamp.

**Expectation before action.** The reasoning loop requires the agent to state what it expects a tool call to return before making it. That is what turns an observation into a contradiction, and noticing a contradiction is mechanically what "noticing" means.

**Reflection is scoped to the system.** The agent reflects on its own performance and never on hers. It may learn that its own reminders land badly at a certain hour. It may not infer anything about her internal state.

## Status

Design phase, moving into implementation. Three checkpoint documents cover the reasoning loop, the memory architecture, and the retrieval layer. What lives in this repository right now is scaffolding rather than a working system, and the interfaces are more settled than the implementations behind them.

I do not yet have a defined success metric. The honest version is that success looks like the agent making her own tracking legible to her rather than replacing it, and I intend to define what that means with her rather than deciding it on her behalf.

## Running it locally

```bash
uv sync --dev
uv run uvicorn blossom.app:app --reload
```

`uv sync --dev` creates `.venv` and installs the pinned runtime and development
dependencies from `uv.lock`. There is no separate seed step: the synthetic
fixtures in `data/synthetic/` are loaded on demand, so the app serves
`/student/due-this-week` immediately.

No credentials are required. `.env.example` documents the paths the system will
use for local state once they are wired up, and copying it to `.env` is
optional. Nothing in the package reads an API key yet, because nothing calls a
model yet.

To run the same checks CI runs:

```bash
uv run ruff check .
uv run mypy blossom tests
uv run pytest
```

The default data adapter reads from fixtures, so nothing here depends on having school LMS access. That is deliberate. LMS APIs are usually restricted to administrators and scraping breaks when the UI changes, so the system is built to run without either and to treat any real connector as an optional source rather than a dependency.

## A note on data

No real family data belongs in this repository, and none is checked in. Everything in `data/synthetic/` describes a fictional student. The accommodations corpus is held as operational support rules rather than clinical descriptions, a decision made for privacy reasons that turned out to serve retrieval as well, since an instruction is a self contained unit of meaning and a clinical description is not.

If you are reading this because you are considering something similar for your own family, the part I would carry over is not the architecture. It is the habit of asking, for every capability, whether the person it is built for would consent to it existing.

## License

See [LICENSE](LICENSE). The license covers the code. The design documents in `docs/` are coursework and are not licensed for reuse.

