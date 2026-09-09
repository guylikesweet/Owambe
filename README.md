# BE Owambe Ticket System

Flask ticketing app — sell tickets, generate QR codes, check guests in at the door.
Runs on Postgres so data survives Render sleeping/restarting the free-tier disk.

## Environment variables (set these in Render's Environment tab)

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | From your Render Postgres instance. Either the Internal or External URL works. |
| `SECRET_KEY` | Recommended | Any random string. Used to sign session cookies. |
| `DB_SSLMODE` | Only if needed | Defaults to `require`. Set to `disable` if you're using the Internal Database URL and get an SSL connection error. |

## Local development
```
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/owambe
export DB_SSLMODE=disable   # only needed for a local, non-SSL Postgres
python app.py
```

## Default login
Username: `admin` / Password: `admin123` — **change this immediately** via
the Change Password page once deployed, or by registering a new admin and
retiring the default account.

## What's in here
- Login (admin/seller roles)
- Sell tickets → auto QR code (generated on the fly, never written to disk —
  this is what makes it safe on Render's ephemeral filesystem)
- Door check-in via QR scan, manual ticket ID, or guest WhatsApp number
- Downloadable QR for any ticket, any time, from the All Tickets page
- Sales report by seller (admin only)
- Self-service password change for any logged-in user
- CSV export of all tickets

## Notes on the Postgres migration
- Tables are created automatically on first boot (`CREATE TABLE IF NOT EXISTS`),
  so you don't need to run any migration script — just set `DATABASE_URL` and deploy.
- If you were previously on SQLite and want your old ticket data, you'd need to
  export it from `tickets.db` and re-insert it into Postgres manually — there's
  no automatic import here, since a Render SQLite file is likely already gone
  by the time you're reading this.
