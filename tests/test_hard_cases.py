"""The hand-written hard cases are the regression suite: each one is a situation a finance clerk
recognises, with the outcome they would want."""

import pytest

from reconcile.hardcases import check, load_cases, run_case

SPEC = load_cases()


@pytest.mark.parametrize("case", SPEC["cases"], ids=[c["id"] for c in SPEC["cases"]])
def test_hard_case(case):
    result = run_case(SPEC, case)
    problems = check(case, result)
    assert not problems, (
        f"{case['id']}: {problems}; score={result.get('score')} reasons={result.get('reasons')}"
    )
