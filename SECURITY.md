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

On Elastic Beanstalk the database is copied to S3 every hour
(`.ebextensions/02-backups.config`), into the account's private
`elasticbeanstalk-<region>-<account>` bucket under `rung-backups/`. Those copies
are the same student records as the live database: never make that bucket or
prefix public, and delete old `daily/` copies once a semester's data is no
longer needed. Transcripts are purged before any backup can see them unless
`RETAIN_TRANSCRIPTS` is on.

## Identity boundary

A student handle labels progress; it does not authenticate a person. Anyone who
knows another handle could claim it from a new browser. That is acceptable for a
portfolio demo and informal practice, and it is not acceptable for official
grades or a class roster. Put a real classroom deployment behind campus SSO (or
add institution-approved accounts) before treating the handle as identity.

The instructor dashboard is disabled unless `RUNG_INSTRUCTOR_CODE` is set. The
code is compared server-side, stored only in the environment, rate-limited by
IP, and replaced in the browser by a signed session flag. It is a small-class
deployment control, not role-based institutional authentication.

## Student code

Student Python never runs on the Flask host. The editor loads Pyodide from the
versioned jsDelivr URL in `public/static/code-runner-worker.js` and executes in a
module Web Worker. Long-running code is stopped by terminating that Worker after
four seconds, which also keeps an infinite loop from freezing the chat and
timer. The final editor value is sent once for grading, capped server-side, and
is not retained after the grading call.

The problem's checks run in that same Worker (`rung/checks.py`), so "solved" is
reported by the student's browser rather than verified by the server. The
server validates the shape of the report (one boolean per check, recorded once),
but a student who edits the request in devtools can mark their own problem
solved. That is acceptable for a practice record, and it is why "solved" must
not feed a course grade. The alternative, running student code on the server,
turns the app into a remote-code-execution service. The checks' expected
outputs reach the browser for the same reason; the forbidden insight never does.

The Content Security Policy permits that versioned CDN for scripts, workers
(Pyodide starts a helper worker from its own URL), and runtime downloads, and
denies framing, plugins, and unexpected origins. A deployment with stricter
supply-chain requirements should self-host the Pyodide core files and remove the
CDN origin from the policy.

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

Per-IP limits only work if the app sees the real address. Behind CloudFront and
Elastic Beanstalk's nginx, every request arrives from 127.0.0.1, so
`RUNG_PROXY_HOPS=2` tells the app to take the client from `X-Forwarded-For`,
trusting exactly the two entries those proxies appended and nothing a client
wrote itself. That is only safe if nobody can reach nginx without going through
CloudFront, which is what `RUNG_ORIGIN_SECRET` enforces: CloudFront adds it as a
header, and any request without it gets a 403. The environment's own
`*.elasticbeanstalk.com` address therefore serves nothing but `/health`.

Three more controls close the gaps those leave, all enforced in the database so
they hold however many copies of the app a host runs:

- **One model call at a time per session.** The turn cap is read before each
  call, so parallel requests used to all pass it and all be billed. A turn now
  claims the session with an atomic conditional UPDATE (`_claim_turn` in
  `app/engine.py`); a second request while one is in flight gets a 429 and
  costs nothing. Tested live: 20 simultaneous messages, 2 billed. Finishing is
  claimed the same way, so a double-submitted finish grades once.
- **A site-wide daily ceiling.** `RUNG_GLOBAL_DAILY_DRILLS` (400) and
  `RUNG_GLOBAL_DAILY_INTERVIEWS` (60) cap sessions started per day across every
  handle and address. Rotating names and VPNs cannot get past it, so the worst
  day has a price: under $20 at the defaults.
- **A class code.** With `RUNG_ACCESS_CODE` set, starting a session needs the
  code once per browser. Only wrong guesses are rate-limited, so a lab behind
  one address can all enter it at once. This is what keeps the public internet
  out entirely; the ceiling above only bounds the damage if the code leaks.

What stays possible: someone with the class code can use their own daily share
(12 drills, 2 interviews) and can use many handles until the site-wide ceiling.
A student who wants to waste the class's budget can make the site "full for
today" for everyone, which is a nuisance with a hard limit rather than a bill.

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
