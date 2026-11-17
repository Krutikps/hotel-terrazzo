# Deploying for free: Render + Neon

Result: `https://hotel-terrazzo.onrender.com` (the service name in `render.yaml`; Render adds a suffix if it is taken; a custom domain is optional) (guest site at `/`, staff portal at `/staff/`), backed by a free Postgres database.
No credit card is needed for Render's free web service or Neon's free plan at the time of writing, but free-tier terms change, so check both pricing pages first.

| Piece | Service | Free-tier behaviour |
|---|---|---|
| Django app | Render web service (free) | Sleeps after ~15 min without traffic; the first request then takes 30-60 s to wake it |
| Database | Neon Postgres (free) | Persistent; may pause when idle and resume on the next query |
| Scheduler | cron-job.org (free) | Releases expired payment holds and runs the daily jobs; its pings also keep Render awake |
| Code | GitHub (free) | Render builds from the repo |

## 1. Put the code on GitHub

Run these in the project folder. `.gitignore` keeps `.env`, `venv/`, the SQLite files, `drive_media/` (raw client files) and `CREDENTIALS.md` out of the repo.

```powershell
git init
git add .
git commit -m "Hotel Terrazzo Suites HMS"
git branch -M main
git remote add origin https://github.com/<you>/hotel-terrazzo.git   # create the empty repo on github.com first (private is fine)
git push -u origin main
```

## 2. Create the database (Neon)

1. Sign up at neon.tech, create a project (pick the region nearest your guests, for example Singapore or Mumbai if offered).
2. Copy the **connection string** (it looks like `postgresql://user:password@ep-xxxx-pooler.<region>.aws.neon.tech/neondb?sslmode=require`). Keep it secret.

## 3. Create the web service (Render)

1. Sign up at render.com and connect your GitHub account.
2. **New -> Blueprint**, choose the repo. Render reads `render.yaml`.
3. Fill in the values it asks for:
   - `DATABASE_URL`: the Neon string from step 2.
   - `ADMIN_EMAIL` and `ADMIN_PASSWORD`: your own staff login (use a strong password).
   - Leave the `RAZORPAY_*` values blank for now.
4. Click **Apply**. The first build takes a few minutes. It installs packages, collects static files, migrates the database and runs `bootstrap_site`, which creates the roles, your admin login and, because `SEED_DEMO=true`, the demo hotel with rooms, rates and sample bookings.
5. Open `https://<service>.onrender.com`. Staff sign in at `/staff/login/` with `ADMIN_EMAIL` / `ADMIN_PASSWORD`.
   The demo staff accounts (`reception@terrazzo.test`, `manager@terrazzo.test` and so on) use the same `ADMIN_PASSWORD`.
   To start with an empty hotel instead of demo data, set `SEED_DEMO=false` before the first deploy.

Render generates `DJANGO_SECRET_KEY` and `CRON_SECRET` for you. Find `CRON_SECRET` under the service's **Environment** tab.

## 4. Schedule the background jobs (cron-job.org)

Free hosts have no cron, so an external pinger calls two URLs. Create two jobs at cron-job.org:

| URL | Schedule |
|---|---|
| `https://<service>.onrender.com/api/cron/holds/?key=<CRON_SECRET>` | every 5 minutes |
| `https://<service>.onrender.com/api/cron/daily/?key=<CRON_SECRET>` | every day at 08:00 |

The holds job releases rooms from unpaid bookings, and the five-minute ping also stops Render's free tier from sleeping for long. Expired holds are also ignored by the availability check, so an expired hold never blocks a sale between sweeps.

## 5. Payments

- **Demo (the default in `render.yaml`):** `PAYMENT_PROVIDER=mock` and `ALLOW_MOCK_PAYMENTS=true`. The built-in sandbox gateway moves no real money. Use this for showing the site only.
- **Real payments:** in the Razorpay dashboard create API keys, then in Render set `PAYMENT_PROVIDER=razorpay`, `ALLOW_MOCK_PAYMENTS=false`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`. Add a webhook pointing at `https://<service>.onrender.com/api/payments/webhook/razorpay/` and put its secret in `RAZORPAY_WEBHOOK_SECRET`. Test with Razorpay's test keys before using live keys.

## 6. Email (optional)

By default, emails are only logged. For real confirmation emails set `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend` plus `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `DEFAULT_FROM_EMAIL` (Gmail app password, Brevo, Resend and similar SMTP services have free tiers).

## 7. Limits to know about

- **Uploads vanish on redeploy.** Render's free disk is wiped each deploy. The brand photos ship inside the repo (`hotel/static/brand`) and are safe. For new room photos use **Staff -> Room types -> Photos -> image URL** (host the image elsewhere), or add the file to `hotel/static/` in the repo. Direct file uploads need external storage such as Cloudinary.
- **Cold starts.** After idle time the first visit is slow. The cron ping reduces this, and any paid Render plan removes it.
- **Not a production setup for a live hotel taking real money.** Move to a paid always-on instance and a paid database with backups first.
- **Custom domain.** Render -> Settings -> Custom Domains. HTTPS is automatic. Add the domain to `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` and set `SITE_URL`.
- **Updating the site.** `git push` to `main` and Render redeploys. Migrations run automatically during the build.
- **Changing the admin password later.** Use Staff -> Users, or set `ADMIN_RESET_PASSWORD=true` for one deploy.

## Settings added for deployment

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (takes priority over the `DB_*` variables) |
| `CRON_SECRET` | Enables `/api/cron/<holds|daily>/`; leave empty to disable |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_RESET_PASSWORD` | Used by `bootstrap_site` |
| `SEED_DEMO` | Load demo data on an empty database |
| `RENDER`, `RENDER_EXTERNAL_HOSTNAME` | Set by Render; the host and HTTPS proxy handling are configured automatically |

`/healthz/` returns `ok` when the app and database respond, and is used as Render's health check.
