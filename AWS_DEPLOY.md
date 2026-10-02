# Deploying Rung to AWS

One Elastic Beanstalk instance running the app, CloudFront in front of it for
HTTPS, and hourly SQLite backups to S3. Roughly $9-10 a month, less while free
tier or credits apply. `render.yaml` and [DEPLOY.md](DEPLOY.md) cover the Render
path instead.

```
students ──https──> CloudFront ──http + X-Rung-Origin──> nginx ──> gunicorn/Flask ──> SQLite
                    (TLS, no caching)                     (EB instance)                  │
                                                                              hourly ──> S3
```

## If you tried this before (September 2026)

The earlier attempt failed or would have failed for four reasons, all fixed in
the repository now:

1. **The Procfile had comment lines.** Elastic Beanstalk's Procfile parser
   rejects them ("Procfile could not be parsed"). It is one line now, and
   `tests/test_offline.py` keeps it that way.
2. **Every stylesheet and the Python runner returned 404.** The Python
   platform's nginx serves `/static` itself from a folder named `static` at the
   bundle root. The app's files live in `public/static`, so
   `.ebextensions/01-rung.config` now maps `/static` there.
3. **The CloudFront console steps left the app unusable.** The console's default
   cache policy strips cookies (nobody stays signed in), caches pages (one
   student's picker could be served to another), and only allows GET, so every
   form post fails. `deploy/cloudfront.yaml` sets all of this correctly.
4. **The whole class shared one "5 sessions per hour" limit.** Behind CloudFront
   and nginx every request looked like it came from 127.0.0.1. `RUNG_PROXY_HOPS`
   fixes that.

The environment from that attempt (`rung-prod` in `us-east-1`) may still exist
and may still be running. Step 3 checks.

## 1. Tools on a fresh Windows machine

```bash
winget install -e --id Python.Python.3.12
```

```bash
winget install -e --id Amazon.AWSCLI
```

Open a new terminal so both are on `PATH`, then:

```bash
pip install awsebcli
```

Use the virtual environment of your choice if you prefer; the EB CLI only has
to be on `PATH` when you run `eb`.

## 2. Credentials

`.elasticbeanstalk/config.yml` tells the EB CLI to use an AWS profile named
`eb-cli`. After a wiped drive that profile is gone, so recreate it with the
access key you already have:

```bash
aws configure --profile eb-cli
```

Region `us-east-1`, output `json`. The same profile is used for the CloudFront
step, so the IAM user needs Elastic Beanstalk, CloudFormation and CloudFront
permissions. An administrator user has all three.

## 3. Test, then find or create the environment

From the repository root:

```bash
python -m pip install -r requirements.txt
```

```bash
python -m tests.test_offline
```

```bash
python -m tests.test_app
```

Then see whether the September environment is still there:

```bash
eb status
```

If it prints an environment with a `CNAME`, keep it and skip to step 4. If it
says the environment does not exist, create one:

```bash
eb create rung-prod --single --instance-type t3.micro
```

`--single` means one instance and no load balancer. That keeps it inside the
free tier; an Application Load Balancer alone costs $16-20 a month. The first
deploy of a new environment reports errors until step 4 sets the secret key.
That is expected: the app refuses to start in production without one.

Write down the `CNAME` from `eb status`. It looks like
`rung-prod.eba-abcd1234.us-east-1.elasticbeanstalk.com`.

## 4. Secrets

Generate two long random values, one to sign cookies and one that only
CloudFront will know. Run each line in PowerShell and keep the output somewhere
safe (a password manager, not a file in this folder):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Run it twice. Then set them, with your Anthropic key, in one command:

```bash
eb setenv RUNG_SECRET_KEY=<first value> RUNG_ORIGIN_SECRET=<second value> ANTHROPIC_API_KEY=<your key>
```

Use a separate Anthropic key from your development one, and set a spend limit
on the Anthropic account with auto-reload off. That limit is the real cost
backstop; everything in the app only keeps it from being reached.

Once `RUNG_ORIGIN_SECRET` is set, the `*.elasticbeanstalk.com` address answers
403 to everything except `/health`. That is the point: students reach the app
only through CloudFront.

Everything non-secret (`RUNG_ENV`, `RUNG_DB`, `RUNG_PROXY_HOPS`, `TZ`, the
static files mapping) is in `.ebextensions/01-rung.config` and applies on every
deploy.

## 5. Deploy the code

```bash
eb deploy
```

`.ebignore` decides what goes into the bundle. Because it exists, the EB CLI
zips this folder rather than the last git commit, which is how the gitignored
course bank `data/problems.json` reaches the server. Your local `data/rung.db`
stays behind.

Check it came up:

```bash
eb health
```

## 6. HTTPS with CloudFront

One command creates the distribution from `deploy/cloudfront.yaml`. Use the
CNAME from step 3 and the **second** secret from step 4:

```bash
aws cloudformation deploy --stack-name rung-cloudfront --template-file deploy/cloudfront.yaml --parameter-overrides OriginDomain=<CNAME> OriginSecret=<second value> --profile eb-cli --region us-east-1
```

It takes five to ten minutes. Then get the address:

```bash
aws cloudformation describe-stacks --stack-name rung-cloudfront --query "Stacks[0].Outputs" --output table --profile eb-cli --region us-east-1
```

The `URL` output, `https://dXXXXXXXXXXXXX.cloudfront.net`, is the link for
students. Give it to the app so social previews resolve:

```bash
eb setenv RUNG_PUBLIC_ORIGIN=https://dXXXXXXXXXXXXX.cloudfront.net
```

No AWS CLI? In the CloudFormation console choose **Create stack → With new
resources → Upload a template file**, pick `deploy/cloudfront.yaml`, and fill in
the same two parameters.

A custom domain is optional: request a free ACM certificate in `us-east-1`,
attach it to the distribution as an alternate domain name, and point a DNS
record at the distribution.

## 7. Classroom settings

Optional, all through `eb setenv`:

| Variable | What it does |
|---|---|
| `RUNG_CURRENT_UNIT=2` | Opens units 1-2; later units show as "Later" and cannot be started |
| `RUNG_INSTRUCTOR_CODE=<long random value>` | Turns on `/instructor`: class concept signals and per-student solved counts |
| `RUNG_BACKUP_PREFIX=rung-backups-staging` | Only if you run a second environment in the same account and region |

## 8. Check it end to end

On the CloudFront URL:

1. `/health` returns `{"status": "ok"}`.
2. The home page is styled, and the picker lists problems by topic.
3. Start a drill, explain an approach twice, and the editor opens with **Run
   code** and **Run checks**. Python loads in a few seconds.
4. Finish, self-assess, and the debrief shows the check results. The problem
   shows as solved on **My progress** if every check passed.

## Redeploying

```bash
eb deploy
```

Nothing watches a repository, so nothing deploys on its own. Edit
`data/problems.json`, then `eb deploy`: the bank is re-seeded at startup, and
removed problems become inactive without losing anyone's history.

## Backups

`.ebextensions/02-backups.config` installs an hourly cron job on the instance.
It snapshots the live database with SQLite's online backup API, which is safe
while the app is writing, and uploads it to the bucket Elastic Beanstalk already
made for your account:

```
s3://elasticbeanstalk-us-east-1-<account-id>/rung-backups/latest.db.gz
s3://elasticbeanstalk-us-east-1-<account-id>/rung-backups/daily/YYYY-MM-DD.db.gz
```

The default instance role can already write to that bucket, so there is nothing
to configure. If an instance is replaced (a failed health check, a platform
update, a rebuilt environment), the next deploy finds no database, restores
`latest.db.gz`, checks its integrity, and starts from there. At most an hour of
activity is lost. With no backup yet, it starts empty, as before.

Backup errors land in `/var/log/rung-backup.log`, which `eb logs` includes. A
quiet log means it is working. To restore an older day on purpose, download that
`daily/` file, then:

```bash
eb ssh
```

On the instance, stop the app (`sudo systemctl stop web`), replace
`/var/rung-data/rung.db` with the unzipped file (owned by `webapp`), delete any
`rung.db-wal` and `rung.db-shm` beside it, and start the app again. `eb ssh`
needs a key pair on the environment; `eb ssh --setup` adds one.

The backups are student records. Keep the bucket private, and delete the
`daily/` copies when a semester's data is no longer needed.

## What this costs

**Roughly $9-10 a month**: about $7-8 for the `t3.micro` running continuously,
a dollar or so of disk, and cents for S3 backups and CloudFront at class
traffic. Accounts opened before July 15, 2025 get a `t3.micro` free for their
first 12 months; newer accounts get AWS credits instead, which cover this for a
while. Check **Billing → Free tier** in the console to see which applies. The Anthropic
bill is separate and set by the daily caps in `rung/config.py`; run
`python -m tools.cost_model` for the worst case.

The instance runs continuously, so there is no cold start: the first student of
the morning does not wait 30-60 seconds the way Render's free tier makes them.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403 Forbidden` on the `elasticbeanstalk.com` address | Expected once `RUNG_ORIGIN_SECRET` is set. Use the CloudFront URL. |
| `403 Forbidden` on the CloudFront URL | The stack's `OriginSecret` does not match `RUNG_ORIGIN_SECRET`. Update one to match the other. |
| Pages load without styling | The static files mapping did not apply. Run `eb deploy` again; check `eb config` for `aws:elasticbeanstalk:environment:proxy:staticfiles`. |
| "Too many sessions started from this address" for everyone | `RUNG_PROXY_HOPS` is not 2, or traffic is not coming through CloudFront. |
| Students are signed out on every page | Visiting over `http://`, or the CloudFront cache policy was changed from CachingDisabled. |
| Code runner says "Runner unavailable" | The browser blocked jsDelivr (some school networks do), or the CSP was edited. |
| 502 right after a new environment is created | `RUNG_SECRET_KEY` is not set yet (step 4). |

## One instance, one worker

`Procfile` runs a single gunicorn worker with four threads. SQLite handles one
writer at a time and the rate limiter in `app/server.py` counts per process, so
a second worker or a second instance would quietly double the limits. For a
class, one small instance is plenty: each request spends almost all of its time
waiting on the Anthropic API, not computing.

`DATABASE_URL` can point the app at Postgres instead (RDS, or Supabase as in
[VERCEL_DEPLOY.md](VERCEL_DEPLOY.md)); the app suite runs against Postgres in
CI. On this one-instance setup SQLite plus the hourly backups is simpler and
cheaper, so it stays the default here.

## Running a real class

1. Student handles label progress; they do not authenticate anyone. Anyone who
   knows a handle can use it. Put the app behind campus SSO before it holds
   anything that counts toward a grade. See SECURITY.md.
2. "Solved" is reported by the student's browser, because student code never
   runs on the server. It is a practice record, not an assessment.
3. Transcripts are deleted after grading unless `RETAIN_TRANSCRIPTS` is turned
   on, and turning it on is a conversation with the department first.
