# Deploying Rung to Vercel + Supabase

Vercel runs the Flask app; Supabase holds the database (Postgres). No servers
to manage, HTTPS and the custom domain certificate are automatic, and both have
free tiers. [AWS_DEPLOY.md](AWS_DEPLOY.md) is the alternative.

```
students ──https──> Vercel CDN ──> public/static/*        (CSS, Python runner)
                         └───────> wsgi.py (Flask function) ──> Supabase Postgres
```

## 1. Supabase: create the database

1. At supabase.com, **New project**. Region **East US (North Virginia)**: Vercel
   runs this app in Washington, D.C. (`iad1` in `vercel.json`), so every query
   stays inside the same area.
2. Pick a database password made of **letters and numbers only**. Symbols have
   to be percent-encoded in a URL, which is an easy way to lose an hour.
3. When the project is ready, click **Connect** at the top, choose
   **Transaction pooler**, and copy the URI. It looks like
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:6543/postgres`.
   Put your password in place of `[YOUR-PASSWORD]`, brackets included. That
   full string is your `DATABASE_URL`. Treat it like a password.

Use the transaction pooler (port 6543), not the direct connection: it is the one
Supabase supports for serverless hosts, and the app turns off the prepared
statements it cannot handle.

## 2. Load the problem bank into Supabase (once, from this computer)

In a terminal in this folder, with your `DATABASE_URL` from step 1:

```bash
python -m pip install -r requirements.txt
```

```bash
$env:DATABASE_URL = "postgresql://postgres.abcdefgh:PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
```

```bash
python -m tools.init_db
```

It creates the tables and loads all 40 problems from `data/problems.json`. It
prints `problems 40` and the database host, never the password. Run it again
whenever you edit the bank; it updates rather than duplicates.

## 3. Vercel: sign in and create the project

```bash
vercel login
```

Then, from this folder:

```bash
vercel link --yes --project rung
```

That creates a project named `rung` in your Vercel account and remembers it in
a local `.vercel/` folder (gitignored). It also writes a `.env.local` holding a
short-lived Vercel token; `.gitignore` and `.vercelignore` keep it out of git
and out of uploads.

The CLI treats the app as a Vercel **service**. `vercel.json` already has the
service configuration it needs: the `wsgi:app` entrypoint, a 60 second limit,
and `includeFiles: "public/**"`. That last one matters: in services mode every
request goes to Flask, and without it the stylesheets and the Python runner
are missing from the deployed bundle. Do not add a top-level `functions` key;
services mode rejects the whole file if you do.

## 4. Environment variables

In the Vercel dashboard: **rung → Settings → Environment Variables**. Add each
of these for the **Production** environment:

| Name | Value |
|---|---|
| `DATABASE_URL` | the Supabase transaction-pooler URI from step 1 |
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `RUNG_SECRET_KEY` | output of `python -c "import secrets; print(secrets.token_hex(32))"` |
| `RUNG_PROXY_HOPS` | `1` |
| `RUNG_SKIP_DB_INIT` | `1` |
| `RUNG_ACCESS_CODE` | recommended: a class code students enter once; without it anyone with the link can start sessions |
| `RUNG_GLOBAL_DAILY_DRILLS` | optional: site-wide drills per day, default `400` |
| `RUNG_GLOBAL_DAILY_INTERVIEWS` | optional: site-wide interviews per day, default `60` |
| `RUNG_INSTRUCTOR_CODE` | optional: a long random value, turns on `/instructor` |
| `RUNG_CURRENT_UNIT` | optional: e.g. `2` opens units 1-2 only |

Use the dashboard for these, not `"1" | vercel env add ...` in PowerShell:
PowerShell can slip an invisible byte-order mark in front of a piped value,
and the app refuses to start with `RUNG_PROXY_HOPS` set to anything but a
plain number (every page returns 500 and `vercel logs` shows why).

`RUNG_PROXY_HOPS=1` makes the per-IP limits see each student's real address;
Vercel overwrites `X-Forwarded-For` with it, so it cannot be forged.
`RUNG_SKIP_DB_INIT=1` stops every cold start from re-seeding the bank, which
step 2 already did.

Set a spend limit on the Anthropic account with auto-reload off. That is the
real cost backstop.

## 5. Deploy

```bash
vercel deploy --prod
```

It prints the production address; this project's is
**https://rung-kohl.vercel.app**. `.vercelignore` decides what is uploaded: the
course bank goes up, your local database, `.env` files, the old Elastic
Beanstalk bundles and the zip file do not. Only `/static/...` is served as
files; `data/problems.json` and the source code are read by the app and return
404 from outside.

If every page returns 500, the app refused to start. `vercel logs <deployment
URL> --no-follow` shows the reason, usually a missing or malformed environment
variable.

Check it:

1. `/health` returns `{"status": "ok"}` (the app reached Supabase).
2. The home page is styled and lists problems by topic.
3. Start a drill, explain an approach twice, and **Run checks** appears once the
   editor opens.

Changed an environment variable? Run `vercel deploy --prod` again; running
deployments keep the values they started with.

## 6. Your domain (rungcs.com on Namecheap)

1. Vercel: **rung → Settings → Domains → Add**, enter `rungcs.com`, and accept
   the suggestion to add `www.rungcs.com` too.
2. Vercel then shows the exact records to create. Copy them from that page.
   They are usually an **A record** for the bare domain and a **CNAME** for
   `www`.
3. Namecheap: **Domain List → Manage → Advanced DNS**.
   - Delete the parking-page records Namecheap added (a CNAME `www` to
     `parkingpage.namecheap.com`, a URL Redirect for `@`).
   - Delete the `_...` CNAME records you added for the AWS certificate. Vercel
     does not need them.
   - **A Record**: Host `@`, Value the IP Vercel showed.
   - **CNAME Record**: Host `www`, Value the target Vercel showed.
   - Type the hosts (`@`, `www`) rather than pasting them; that is what froze
     the page before.
4. Back in Vercel the domains turn green within minutes to an hour, and Vercel
   issues the HTTPS certificate itself.
5. Add `RUNG_PUBLIC_ORIGIN` = `https://rungcs.com` (step 4) and deploy again.

## Redeploying

```bash
vercel deploy --prod
```

Edited `data/problems.json`? Run step 2's `python -m tools.init_db` first, then
deploy.

Deploy from this folder with the CLI rather than connecting the GitHub repo:
the course bank is gitignored, so a deploy built from GitHub would only have
the 8-problem sample bank.

## What is different from the AWS setup

- **The data lives in Supabase**, not on one server's disk, so redeploying or
  Vercel replacing an instance loses nothing.
- **Backups depend on the Supabase plan.** Paid plans include daily backups.
  On the free plan, take your own before anything risky (semester end, a bank
  overhaul): **Database → Backups** in the dashboard, or `pg_dump` with the
  session-pooler URI.
- **Free Supabase projects pause after a week with no activity.** During the
  semester students keep it awake; after a break, open the Supabase dashboard
  and click **Restore** before class, or the app's `/health` will fail.
- **Request limits are per instance.** The per-minute and per-hour limits in
  `app/server.py` count in memory, and Vercel may run more than one copy of the
  app at busy times, each with its own count. Daily per-student caps are in the
  database and hold exactly. The Anthropic spend limit is the backstop.
- **Free-plan terms.** Vercel's Hobby plan is for non-commercial use. A free
  practice tool for a class fits that; if the department ever charges for it
  or runs it as a department service, move to Pro.
- **Cold starts.** After a quiet spell the first request takes a second or two
  longer while Vercel starts the function.
