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

The interfaces are more settled than the implementations behind them. The design notes that cover the reasoning loop, the memory architecture, the retrieval layer, and search based replanning are kept outside this repository.

It is built for one household, and it will keep changing for as long as it is useful there.

No model is called yet. There is no agent.

### What works

The student's weekly view is real end to end. Assignments load from the
structured store, source records are reconciled without a winner being chosen,
and every assignment in the window reaches the page labeled with how well its
due date is corroborated. Nothing is filtered out, including assignments
nothing corroborates.

Around that: configuration and time are injectable, stores are opened once at
startup and injected into routes, reflections structurally cannot be written
about the student, the tool registry structurally cannot hold a tool that
sends, the import allowlist confines each network-capable dependency to one
file, and the model framework's tools pass through a two-layer boundary: only
one module can construct them, and middleware refuses any call the registry
does not know. An approval gate pauses the graph with a draft and resumes only
on a person's decision.

### What does not work yet

- **The contradiction check is inert.** `AgentStep` requires an expectation,
  but the comparison is a substring test, and where it runs it compares a
  lookup key against itself. It cannot currently register a contradiction, and
  its result is discarded.
- **No agent trace is persisted.** Steps are built and dropped.
- **The workload signal is discarded.** The endpoint accepts it and stores
  nothing; no plan is reduced, and nothing becomes visible to her.
- **The semantic half of retrieval is a stub** in the running system, and the
  support-rules store is neither wired nor tested. Of the three stores, one is
  live.
- **Tier-one verification cannot pass.** The policy check reports itself as
  not implemented.
- **There is no visibility policy layer.** Separate view models prevent
  accidental leaks; they are not the architectural boundary the design calls
  for.
- **Nothing is durable.** Project state is in memory.

`docs/architecture.md` covers each of these in context.

I do not yet have a defined success metric. The honest version is that success looks like the agent making her own tracking legible to her rather than replacing it, and I intend to define what that means with her rather than deciding it on her behalf.

## Running it locally

```bash
uv sync --dev
uv run uvicorn blossom.app:app --reload
```

`uv sync --dev` creates `.venv` and installs the pinned runtime and development
dependencies from `uv.lock`. There is no separate seed step: the synthetic
fixtures in `data/synthetic/` are loaded once at startup, so the app serves
`/student/due-this-week` immediately.

The default data adapter reads from fixtures, so nothing here depends on having school LMS access. That is deliberate. LMS APIs are usually restricted to administrators and scraping breaks when the UI changes, so the system is built to run without either and to treat any real connector as an optional source rather than a dependency.

Those fixtures carry fixed August 2026 due dates, and "due this week" is
computed from the real clock, so outside that week the page is legitimately
empty. To see the fixture data, pin the clock:

```bash
BLOSSOM_TODAY=2026-08-19 uv run uvicorn blossom.app:app --reload
```

No credentials are required for anything the app does today. It reads
`ANTHROPIC_API_KEY` from the environment when present, for the model calls that
arrive with the agent, and runs every fixture-backed page without it. Hosted
tracing for the model framework is forced off at startup, whatever the
environment says.

Every filesystem location is configurable through the `BLOSSOM_*` variables
documented in `.env.example`, and all of them have working defaults, so
copying it to `.env` is optional. Relative values in that file are resolved
against the repository root rather than the working directory, which means the
app starts correctly from anywhere. To load the file:

```bash
uv run --env-file .env uvicorn blossom.app:app --reload
```

To run the same checks CI runs:

```bash
uv run ruff check .
uv run mypy blossom tests
uv run pytest
```

### If your network blocks PyPI

`uv` downloads from `files.pythonhosted.org` directly, so on a network that
only permits an internal package proxy it fails at the TLS handshake even
though `pip` still works. Build the environment with pip instead:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
pip install mypy==1.17.1 ruff==0.12.9 pytest==8.4.1 httpx2==2.10.0
```

On macOS or Linux the second line is `source .venv/bin/activate`.

Two install commands rather than one because the development dependencies live
in a PEP 735 `[dependency-groups]` table, which pip could not install from
until 25.1 added `pip install --group dev`. Keep those pins matching
`pyproject.toml`. A test enforces it, because an editor running a different
mypy than CI will disagree with CI about what passes.

Creating the environment at `.venv` inside the repository also lets editors
find it without configuration. If your editor reports that `fastapi` or
`pydantic` cannot be found, it is resolving imports against a different
interpreter rather than finding a fault in the code.

`.vscode/settings.json` points the mypy and ruff extensions at the versions
installed in that environment rather than the ones they bundle, so the editor
and CI check the same things. In VS Code, pick the interpreter with
**Python: Select Interpreter** from the command palette.

## A note on data

No real family data belongs in this repository, and none is checked in. Everything in `data/synthetic/` describes a fictional student. The accommodations corpus is held as operational support rules rather than clinical descriptions, a decision made for privacy reasons that turned out to serve retrieval as well, since an instruction is a self contained unit of meaning and a clinical description is not.

If you are reading this because you are considering something similar for your own family, the part I would carry over is not the architecture. It is the habit of asking, for every capability, whether the person it is built for would consent to it existing.

## License

See [LICENSE](LICENSE).

