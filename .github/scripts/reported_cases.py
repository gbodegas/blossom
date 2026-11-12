"""Check that a test run reported every collected case exactly once.

Usage: reported_cases.py COLLECTED RESULTS

COLLECTED is the output of ``pytest --collect-only -q``, one case to a line, such as
``tests/test_x.py::TestY::test_z[one]``. RESULTS is the JUnit file ``pytest --junitxml`` wrote
for the run. The check passes only when the cases named in the results are the collected
cases, each named once.

A passed, skipped, failed or errored case all count as reported. So this says that every case
has an outcome. It does not say that the case's body ran, and it does not say the outcome is a
pass: pytest's own exit status covers failures. What this adds is a run that ends without a
failure while cases are missing, which a worker lost during collection can cause.

Exit status 0 when the two agree, 1 when they differ or either file cannot be read, 2 on a
wrong command line.
"""

import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter

type Case = tuple[str, str]

CASE_LINE = re.compile(r"[^\s:]+\.py::\S")
OUTCOMES = ("failure", "error", "skipped")
SHOWN = 20


def say(line: str) -> None:
    sys.stdout.write(line + "\n")


def collected_case(line: str) -> Case:
    """A collected line as the results file names it: its module and classes, then the case."""
    path, bracket, parameters = line.partition("[")
    names = path.split("::")
    module = names[0].removesuffix(".py").replace("/", ".")
    return ".".join([module, *names[1:-1]]), names[-1] + bracket + parameters


def collected_cases(path: str) -> Counter[Case]:
    with open(path, encoding="utf-8") as listing:
        lines = [line.rstrip("\n") for line in listing]
    return Counter(collected_case(line) for line in lines if CASE_LINE.match(line))


def reported_cases(path: str) -> tuple[Counter[Case], Counter[str]]:
    # The file is this job's own pytest output, not data from outside it.
    root = ElementTree.parse(path).getroot()  # noqa: S314
    cases: Counter[Case] = Counter()
    outcomes: Counter[str] = Counter()
    for case in root.iter("testcase"):
        cases[(case.get("classname", ""), case.get("name", ""))] += 1
        outcomes[next((child.tag for child in case if child.tag in OUTCOMES), "passed")] += 1
    return cases, outcomes


def show(label: str, cases: Counter[Case]) -> None:
    say(f"{label}: {sum(cases.values())}")
    for (classname, name), count in sorted(cases.items())[:SHOWN]:
        say(f"  {classname}::{name}" + (f" ({count} times)" if count > 1 else ""))
    if len(cases) > SHOWN:
        say(f"  and {len(cases) - SHOWN} more")


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        say("usage: reported_cases.py COLLECTED RESULTS")
        return 2
    try:
        collected = collected_cases(arguments[0])
        reported, outcomes = reported_cases(arguments[1])
    except (OSError, UnicodeDecodeError, ElementTree.ParseError) as error:
        say(f"cannot read the collected cases or the results: {error}")
        return 1
    counts = ", ".join(f"{name} {count}" for name, count in sorted(outcomes.items()))
    say(f"collected {sum(collected.values())} cases; the results name {sum(reported.values())}")
    say(f"outcomes in the results: {counts or 'none'}")
    if not collected:
        say("no collected case was found, so there is nothing to hold the results to")
        return 1
    missing = collected - reported
    unexpected = reported - collected
    if missing:
        show("collected and not in the results", missing)
    if unexpected:
        show("in the results more often than collected", unexpected)
    if missing or unexpected:
        return 1
    say("every collected case is in the results exactly once")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
