"""
Runs a problem's checks against a student's code.

This module never executes on the server. The interview page hands its source
to the Pyodide Web Worker, which runs it beside the student's code in the
student's own browser; see public/static/code-runner-worker.js and the DECISIONS.md
entry "Python runs in a killable browser worker". It lives here rather than as
a string inside JavaScript so the offline suite can test exactly the code the
browser runs, under CPython.

Standard library only, and kept to syntax every supported Python accepts.

A bank test is a two-item list, [input, expected]. `input` is the argument for
a one-parameter function, or the list of positional arguments for a function
that takes several. The bank cannot say which, because [4, 9, 9, 1] is both
"one list" and "four numbers", so the student's own signature decides.

JSON has no tuples and no integer keys, so the bank stores (3, 4) as [3, 4] and
{1: ["x"]} as {"1": ["x"]}. The result goes through the same JSON round trip
before it is compared, which makes those equal without loosening anything else.
"""

import copy
import inspect
import json
import math


def _arity(fn):
    """(required, total) positional parameters, or None when unknowable."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return None
    required = total = 0
    for param in params:
        if param.kind == param.VAR_POSITIONAL:
            return None
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            total += 1
            if param.default is param.empty:
                required += 1
    return required, total


def _call(fn, given):
    # Unpack only for a function that genuinely needs several arguments. A
    # one-parameter function with an optional extra, second_largest(numbers,
    # k=2), must still receive [4, 9] as one list rather than as numbers=4.
    arity = _arity(fn)
    if (arity is not None and arity[0] >= 2 and isinstance(given, list)
            and arity[0] <= len(given) <= arity[1]):
        return fn(*given)
    return fn(given)


def _normalise(value):
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        # A set or a custom object has no JSON form. Compare it as it is, which
        # fails honestly against a JSON expectation rather than crashing.
        return value


def _same(actual, expected):
    if isinstance(actual, bool) or isinstance(expected, bool):
        # bool is an int subclass: without this, returning 1 would pass a
        # check that expects True.
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_same(a, e) for a, e in zip(actual, expected)))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(_same(actual[key], expected[key]) for key in expected))
    return type(actual) is type(expected) and actual == expected


def _describe(prefix, exc):
    return "%s: %s: %s" % (prefix, type(exc).__name__, exc)


def run_checks(code, function_name, tests):
    """Execute `code`, then call `function_name` once per test.

    Returns {"passed": [bool, ...], "error": str or None}. `error` is the first
    thing that went wrong, described without the input that caused it. During
    a session, working out which edge case breaks the code is the student's
    job; the debrief shows the failing inputs once the session is over.
    """
    failed = [False] * len(tests)
    namespace = {"__name__": "__main__"}
    try:
        exec(compile(code, "<your code>", "exec"), namespace)
    except BaseException as exc:  # noqa: BLE001 - SystemExit is still just feedback
        return {"passed": failed, "error": _describe("Your code did not run", exc)}

    fn = namespace.get(function_name)
    if not callable(fn):
        return {"passed": failed,
                "error": "Define a function named %s so the checks can call it."
                         % function_name}

    passed, error = [], None
    for index, case in enumerate(tests, start=1):
        given, expected = case
        try:
            # A fresh copy per check, so code that mutates its argument cannot
            # change the input a later check receives.
            ok = _same(_normalise(_call(fn, copy.deepcopy(given))), expected)
        except BaseException as exc:  # noqa: BLE001
            ok = False
            if error is None:
                error = _describe("Check %d raised" % index, exc)
        passed.append(ok)
    return {"passed": passed, "error": error}


def run_checks_json(code, function_name, tests_json):
    """The browser entry point: JSON in, JSON out, nothing shared with JS."""
    return json.dumps(run_checks(code, function_name, json.loads(tests_json)))
