# BE Owambe Ticket System

Flask ticketing app for selling tickets, generating non-sequential QR ticket codes, and checking guests in at the door.

## Highlights

- Owambe flyer used as a subtle site background.
- Owambe logo in the top-left header.
- Light/dark theme follows the handler device's system preference with `prefers-color-scheme`.
- Lagos/WAT timestamps via `Africa/Lagos`.
- Welcome/navigation landing page after login; no autofocus so mobile keyboards do not open unexpectedly.
- Ticket review confirmation before any ticket is issued.
- 12-character random public ticket codes (`OW-XXXXXXXXXXXX`) instead of sequential IDs.
- QR codes contain the public ticket code, not the database sequence ID.
- Full-screen scan result layer with explicit acknowledgement.
- Distinct valid tone vs invalid/already-used buzzing tone using the browser Web Audio API.
- Two-ticket quick recent-sales view.
- Every seller/admin can search every ticket and download its QR code.
- Global totals plus each user's personal totals.
- Admin-only seller removal/restore while retaining seller history.
- Admin-only ticket pricing controls; old tickets retain their recorded price.
- Admin-only developer/failsafe ticket-history reset that preserves users.
- Footer attribution: Powered by Oma.

## Deployment

Set `DATABASE_URL`, `SECRET_KEY`, and (if needed) `DB_SSLMODE` in the deployment environment. The app creates/updates its PostgreSQL tables on boot.
