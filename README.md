# Hotel Terrazzo Suites — Hotel Management System & Booking Engine

A full-stack, database-backed hotel platform: a **guest website with a live booking engine** and a **reception / admin HMS portal**, sharing one backend. Everything — availability, prices, bookings, payments, room status — lives in the database; the browser only displays what the server returns.

- **Guest site** (`/`): rooms, photos, live availability search, rate plans with dynamic pricing, multi-step checkout, temporary room hold, online payment, confirmation (print / PDF / email), "My booking" (view, cancel).
- **Staff portal** (`/staff/`): dashboard, reservations, walk-in booking wizard, arrivals / check-in, departures / check-out + invoices, drag-and-drop availability calendar, rooms, room blocks, housekeeping, maintenance, guests, payments & refunds, invoices, pricing calendar + bulk updates, rate plans, pricing rules, coupons, services, reports (CSV / Excel / PDF), audit log, users & role permissions, settings.

## Architecture

```
Browser ──► Django views (server-rendered pages) ──┐
   │                                               ├──► services/  (ALL business logic)  ──► Database
   └──► JSON API  /api/…  (Alpine.js screens) ─────┘        availability · pricing · reservations ·
                                                            payments · notifications · reports · audit
Payment provider (Razorpay / sandbox) ──► verify (server-side) + webhook ──► services/payments
```

| Concern | Where | Notes |
|---|---|---|
| Availability | `hotel/services/availability.py` | `sellable rooms − blocked rooms − (confirmed + checked-in + live holds)` per night; the only place inventory is computed |
| Pricing & tax | `hotel/services/pricing.py` | One `quote()` used by search, checkout, modification and staff booking. Frontends never compute prices |
| Reservations | `hotel/services/reservations.py` | Create / hold / confirm / modify / cancel / assign / check-in / check-out / blocks / maintenance |
| Payments | `hotel/services/payments.py`, `hotel/payments/` | Provider interface (`base.py`), Razorpay, built-in sandbox (`mock.py`) |
| Notifications | `hotel/services/notifications.py` | Email / SMS / WhatsApp / in-app, all logged |
| Reports | `hotel/services/reports.py` | Uniform report dicts → screen, CSV, XLSX, PDF |
| RBAC | `accounts/permissions.py`, `User.can()` | Every staff view and API is permission-checked server-side |
| Audit | `hotel/services/audit.py` | Who / what / when / before / after |

**Stack:** Python 3.11, Django 5.2, SQLite (dev) / PostgreSQL (prod), Alpine.js + Chart.js (vendored in `static/vendor`, no build step), ReportLab (PDF), openpyxl (Excel), WhiteNoise, Gunicorn.

### How double-booking is prevented

Every inventory-changing operation (`create_reservation`, `modify`, `confirm_after_payment`, `assign_room`, `block_room`…) runs in **one transaction** that first **locks the room type** (`SELECT … FOR UPDATE` on PostgreSQL; SQLite runs `BEGIN IMMEDIATE`, which serialises writers) and then **re-validates availability server-side** before writing. Live payment holds (`PENDING_PAYMENT` with `hold_expires_at`) count against inventory; expired holds stop counting immediately and are flipped to `RELEASED` by the sweeper. The test `test_concurrency.py` fires simultaneous threads at the last rooms and asserts exactly the available number succeed.

### Booking & payment flow

1. Guest searches → `/api/availability/` (database inventory + `quote()` per rate plan).
2. Checkout collects details → `POST /api/reservations/hold/` creates a **`PENDING_PAYMENT`** reservation with an expiry *and* a provider order. Inventory is held.
3. Payment page opens the provider checkout. On completion the browser sends the provider's result to `/api/payments/verify/`; the **server verifies the signature** (and, for Razorpay, re-fetches the payment from Razorpay and checks order + amount) before anything is confirmed. A webhook is an independent second path (`/api/payments/webhook/razorpay/`). Both are idempotent.
4. `PENDING_PAYMENT → CONFIRMED`, booking number generated (`HTS-2026-000125`), confirmation e-mailed with PDF.
5. Payment failed / cancelled → `RELEASED` (inventory returns). Hold expired → `RELEASED`. If money arrives *after* expiry and the room is gone, the payment is **refunded automatically**.

## Quick start (local)

```powershell
# from the project folder (Windows PowerShell)
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py seed_demo          # demo hotel, rooms, rates, guests, bookings, payments…
.\venv\Scripts\python.exe manage.py runserver
```

macOS / Linux: `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`, then the same `manage.py` commands.

- Guest website: <http://localhost:8000/>
- Staff portal: <http://localhost:8000/staff/>
- Re-seed from scratch: `python manage.py seed_demo --reset`

### Test accounts (created by `seed_demo`, password `Terrazzo@123` — change immediately outside local dev)

| Email | Role | Can do |
|---|---|---|
| `admin@terrazzo.test` | Super Admin | everything |
| `manager@terrazzo.test` | Hotel Manager | operations, rooms, rates, reservations, reports, settings (not user admin) |
| `reception@terrazzo.test` | Receptionist | reservations, guests, check-in/out, payments, availability, blocking, housekeeping |
| `housekeeping@terrazzo.test` | Housekeeping | room status, housekeeping, report maintenance issues |
| `accounts@terrazzo.test` | Accountant | payments, refunds, invoices, reports |

Roles and their permissions are editable in **Users & roles**. Create your own admin with `python manage.py createsuperuser`.

### Try the guest journey
1. Open `/`, pick dates → **Search availability**; choose a room and a rate plan; fill in the details; **Continue to payment**.
2. With `PAYMENT_PROVIDER=mock` you're sent to the built-in sandbox gateway: choose *success* or *declined*. (The result is signed and verified by the server exactly as a real gateway callback would be.)
3. Sign in to `/staff/` — the booking appears on the dashboard and in *Upcoming bookings* with guest, dates, room, purpose, special requests, tariff, paid and pending amounts.

## Configuration (environment variables)

All secrets come from environment variables / `.env` (see [`.env.example`](.env.example)). Nothing sensitive is stored in the database or sent to the browser.

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `SITE_URL` | Core settings (secret key is mandatory when `DEBUG=false`) |
| `DB_ENGINE` (`sqlite`/`postgres`), `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database |
| `PAYMENT_PROVIDER`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Payments |
| `ALLOW_MOCK_PAYMENTS` | The sandbox gateway is refused unless `DEBUG` or this is `true` — never enable on a live site |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | Email |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM`, `TWILIO_WHATSAPP_FROM` | SMS / WhatsApp |
| `REDIS_URL` | Shared cache for rate limiting across workers |
| `SECURE_COOKIES`, `SECURE_SSL_REDIRECT`, `BEHIND_PROXY` | HTTPS hardening |

Hotel profile, check-in/out times, hold minutes, online-payment %, currency, tax rules, payment provider choice and notification channels are edited in **Settings** (staff portal).

## Database

- Schema is created by migrations: `python manage.py migrate`. Models: `hotel/models.py`, `accounts/models.py`.
- Foreign keys, indexes, unique constraints (e.g. unique booking numbers, one price row per room-type/plan/date), and a `check_out > check_in` check constraint are enforced in the database.
- **PostgreSQL (production):** set `DB_ENGINE=postgres` and the `DB_*` variables, then `migrate` and (optionally) `seed_demo`.
- Money is `DecimalField`; reservation totals are derived from itemised `ReservationLine` rows and `Payment` rows (`recompute()`), so they cannot drift.

## Payments

**Razorpay** (recommended for INR; works with test keys as a sandbox):
1. Put `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` in `.env`; in **Settings → Payment** choose `razorpay` (or set `PAYMENT_PROVIDER=razorpay` before first `seed_demo`).
2. In the Razorpay dashboard add a webhook → `https://YOUR_DOMAIN/api/payments/webhook/razorpay/` with events `payment.captured`, `payment.failed`, and set `RAZORPAY_WEBHOOK_SECRET` to the same secret.
3. Card data never touches this server; only order ids, payment ids and signatures are stored.

Add another provider by subclassing `PaymentProvider` (`hotel/payments/base.py`) — implement `create_order`, `verify_payment`, `parse_webhook`, `refund` — and registering it in `hotel/payments/__init__.py`.

Offline payments (cash / card / UPI / bank transfer / other) are recorded by staff on any booking; refunds (full / partial) are issued from **Payments** (online ones go through the gateway).

## Scheduled jobs

| Command | Schedule | What it does |
|---|---|---|
| `python manage.py expire_holds` | every minute (or `--loop 30`) | Releases lapsed payment holds. Lapsed holds already stop blocking inventory instantly; this tidies status |
| `python manage.py daily_jobs` | daily ~08:00 | Pre-arrival & check-out reminders, no-show marking, hold sweep |

## Production deployment

**Free hosting (Render + Neon + cron-job.org): see [DEPLOY.md](DEPLOY.md).**

```bash
pip install -r requirements.txt
export DJANGO_DEBUG=false DJANGO_SECRET_KEY=… DJANGO_ALLOWED_HOSTS=hotel.example.com SITE_URL=https://hotel.example.com \
       DB_ENGINE=postgres DB_NAME=… DB_USER=… DB_PASSWORD=… SECURE_COOKIES=true BEHIND_PROXY=true
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn hms.wsgi:application --workers 3 --bind 0.0.0.0:8000
```

Put it behind HTTPS (nginx / Caddy / a platform load balancer) and forward `X-Forwarded-Proto`. Static files are served by WhiteNoise; uploaded media (`/media/`) should be served by your web server or object storage. Use Redis (`REDIS_URL`) so rate limits are shared between workers. Seed only the first time (or skip `seed_demo` and create your own hotel data in the portal).

## Tests

```bash
python manage.py test hotel
```

Covers: availability maths (incl. the 5-room example), capacity, pricing (rules, extra guests, meals, slabs, coupons, restrictions), taxes & discounts, booking creation, cancellation fees/refunds, modification, check-in/out, room blocking & maintenance, payment verification (forged signatures, idempotency, failure, expiry, paid-after-expiry refund, Razorpay signature / webhook), the full guest HTTP journey, role permissions, audit logging, every staff page/report/export, and **concurrent attempts on the last room** (`test_concurrency.py`).

## API overview

Public (`/api/`, JSON, rate-limited): `GET availability/`, `POST quote/`, `POST reservations/hold/`, `GET reservations/<token>/`, `POST reservations/<token>/{release,restart,cancel}/`, `POST payments/verify/`, `POST payments/failed/`, `POST payments/webhook/<provider>/`, `POST my-booking/lookup/`.
Staff (`/api/staff/`, session + permission checked): `pulse/`, `availability/`, `quote/`, `POST reservations/`, `POST reservations/<id>/modify/`, `POST reservations/<id>/move/`, `calendar/`, `pricing/`, `POST pricing/update/`, `POST pricing/bulk/`. Other staff actions (check-in, check-out, payments, refunds, blocks, status, tickets, CRUD) are permission-gated form posts under `/staff/`.
Errors are always `{"ok": false, "error": {"message": "...", "code": "..."}}` with user-friendly text; internals are logged, never returned.

## Security

Password hashing (PBKDF2), HTTP-only / SameSite session cookies (Secure when not DEBUG), CSRF on all state-changing browser requests (the payment webhook is the only exemption and is signature-verified), server-side validation everywhere, per-IP rate limits (login, search, booking, lookup, contact), role-based access on every route and API, audit trail, secrets only in the environment, no card data stored, the sandbox gateway refuses to run in production.

## Real-time sync

Staff screens poll `/api/staff/pulse/` every 8 s (cheap) and refresh when reservations, room statuses, blocks or notifications change; the toast announces new bookings. Guest availability is computed from the database on every search, so a block placed by reception disappears from the next search immediately, and a held/booked last room is reflected on the dashboard on the next pulse.

## Branding & photos

Placeholder imagery is generated by `/placeholder/<name>.svg`. Replace it from **Room types → Photos** (upload or paste URL), and the logo / hero image from **Settings → Hotel** (`logo`, `hero_image_url`). Colours and fonts live in `hotel/static/css/guest.css` (`:root` variables).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `database is locked` on SQLite | Only one writer at a time; use PostgreSQL for production |
| "Payment provider not configured" | Set Razorpay keys, or `PAYMENT_PROVIDER=mock` for local sandbox |
| "The sandbox payment provider is disabled in production" | Intended: configure Razorpay (or, for staging only, `ALLOW_MOCK_PAYMENTS=true`) |
| Emails only appear in the terminal | Default backend is console; set `EMAIL_BACKEND` + SMTP variables |
| CSRF errors behind a proxy | Set `DJANGO_CSRF_TRUSTED_ORIGINS=https://your.domain` and `BEHIND_PROXY=true` |
| Staff page says "No access" | Your role lacks the permission — edit it in **Users & roles** |
| Held room not released | Run `expire_holds` on a schedule (staff tabs also sweep every 8 s) |
