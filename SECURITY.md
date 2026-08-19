# Security

Short version of the threat model: the API key must never reach a browser, and
transcripts are student work rather than test data.

## The API key

The key lives in one place: the server's environment. It is read at runtime and
never written to a tracked file.

```python
api_key = os.environ.get("ANTHROPIC_API_KEY")
```

Students do not have keys and do not need Anthropic accounts. Their browser
posts to a Flask route, the server calls the API, and the reply comes back
through the server. The student never learns which provider is behind it.

**The mistake to avoid in phase 2:** calling `api.anthropic.com` directly from
JavaScript. That ships the key to every browser that loads the page, and the
users here are CS students who will open devtools. Anything the browser can
reach is public, including config values rendered into a template.

`tests/test_offline.py` scans every tracked file for key material and fails the build
if it finds any. Run it before pushing. Also turn on GitHub secret scanning with
push protection (Settings, Code security), which blocks the push before a
credential ever lands.

If a key does get committed: **revoke it in the console immediately.** Git
history is permanent, so deleting the line in a later commit does not remove it.
Rewriting history is a distant second and usually incomplete.

## Student data

Turn text is stored only while a session is running. It has to be: the API is
stateless, so those rows are the proctor's memory of the conversation, and the
grader reads them once at the end. `finish()` then purges them, leaving hint
depth, phase, token counts and the grade behind. `RETAIN_TRANSCRIPTS` in
`rung/config.py` keeps them instead, which is a conversation with the
department rather than a config change.

`results/` and `data/*.db` are gitignored, and the reason is privacy rather than
secrets. Treat them accordingly:

- Never commit them
- Store the minimum needed for the concept diagnostic: hint depth per tag, not
  the full text of what a student typed
- Talk to whoever owns the course before collecting anything. Student
  performance records have rules attached, and this is not a decision to make
  alone

## Abuse and cost

An attacker here is a bored student with a loop, not a nation state. Both of the
controls that matter are server-side, because a limit enforced in the browser is
a suggestion.

`rung/budget.py` ends a session on whichever ceiling hits first: turn count, token
total, or wall clock. It is checked before each call rather than after, since
checking afterwards means the call that broke the budget already got billed.

`rung/config.py` holds a per-student daily cap and a per-IP request ceiling for the
Flask layer. `DAILY_DRILL_LIMIT` is the main funding lever: it sets the worst
case, and `tools/cost_model.py` prints that ceiling directly.

Set a hard spend cap in the Anthropic console with auto-reload off. Worst case
becomes failed requests, which is embarrassing. The alternative worst case is a
bill.

## Prompt injection

Student messages are untrusted input that goes straight into a model prompt.
`evals/cases.py` includes an `authority_claim` category covering fake system overrides
and instructor-mode claims, and the system prompt states that the student cannot
authorise rule changes.

This is mitigation, not a guarantee. Rules in a system prompt are strong but not
absolute, which is exactly why `rung/judge.py` verifies the output afterwards instead
of trusting that the constraint held.
