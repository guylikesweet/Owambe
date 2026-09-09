from flask import Flask, render_template, request, redirect, url_for, session, g, flash, Response
import os, io, csv, secrets, string, re
from datetime import datetime
import qrcode
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-render")

# Render gives you a postgres:// URL — psycopg2 accepts that fine, but
# normalizing to postgresql:// avoids surprises if you ever add SQLAlchemy.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Render's free/internal Postgres works fine with SSL required; override with
# DB_SSLMODE=disable in your env vars if you're on an internal-only connection
# that rejects it.
SSL_MODE = os.environ.get("DB_SSLMODE", "require")

EVENT_NAME = "BE Owambe"
TICKET_PRICE = 3500

# ------------------ DB HELPERS ------------------
def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            DATABASE_URL,
            sslmode=SSL_MODE,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=()):
    """sqlite3-style shortcut: db.cursor().execute(...) in one call.
    Rolls back automatically on error so the connection stays usable
    for the rest of the request (Postgres aborts the whole transaction
    on any failed statement, unlike SQLite)."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(sql, params)
    except Exception:
        db.rollback()
        raise
    return cur


def init_db():
    db = get_db()
    query("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'seller'
        )
    """)
    query("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            ticket_code TEXT UNIQUE,
            name TEXT NOT NULL,
            whatsapp TEXT NOT NULL,
            used BOOLEAN DEFAULT FALSE,
            created_at TEXT,
            used_at TEXT,
            sold_by INTEGER REFERENCES users (id)
        )
    """)
    db.commit()

    user = query("SELECT * FROM users").fetchone()
    if not user:
        try:
            query(
                "INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)",
                ("admin", generate_password_hash("admin123"), "admin"),
            )
            db.commit()
            print("Default admin created: admin / admin123")
        except psycopg2.IntegrityError:
            db.rollback()  # another worker created it first — fine


with app.app_context():
    init_db()


def generate_ticket_code():
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    code2 = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"BE-{code}-{code2}"


def current_user():
    if "user_id" not in session:
        return None
    return query("SELECT * FROM users WHERE id = %s", (session["user_id"],)).fetchone()


def login_required(role=None):
    def decorator(f):
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if role and user["role"] != role:
                flash("You don't have permission for that page", "error")
                return redirect(url_for("sell"))
            g.user = user
            return f(*args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator

# ------------------ AUTH ROUTES ------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = query("SELECT * FROM users WHERE username = %s", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("sell"))
        else:
            error = "Invalid username or password"
            return render_template("login.html", error=error)
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/change_password", methods=["GET", "POST"])
@login_required()
def change_password():
    user = g.user
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not check_password_hash(user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new != confirm:
            flash("New password and confirmation don't match.", "error")
        else:
            db = get_db()
            query("UPDATE users SET password_hash = %s WHERE id = %s",
                  (generate_password_hash(new), user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("sell"))

    return render_template("change_password.html", event_name=EVENT_NAME, user=user)

# ------------------ ADMIN ROUTES ------------------
@app.route("/register", methods=["GET", "POST"])
@login_required(role="admin")
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        role = request.form["role"]
        db = get_db()
        try:
            query("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)",
                  (username, generate_password_hash(password), role))
            db.commit()
            flash(f"User {username} created successfully", "success")
            return redirect(url_for("report"))
        except psycopg2.IntegrityError:
            db.rollback()
            flash("Username already exists", "error")
    return render_template("register.html", event_name=EVENT_NAME, user=g.user)

@app.route("/report")
@login_required(role="admin")
def report():
    sales = query("""
        SELECT u.username, u.role, COUNT(t.id) as tickets_sold,
               COUNT(t.id) * %s as total_cash
        FROM users u
        LEFT JOIN tickets t ON u.id = t.sold_by
        GROUP BY u.id
        ORDER BY total_cash DESC
    """, (TICKET_PRICE,)).fetchall()
    users = query("SELECT * FROM users ORDER BY role, username").fetchall()
    return render_template("report.html", sales=sales, users=users, event_name=EVENT_NAME, user=g.user)

# ------------------ MAIN APP ROUTES ------------------
@app.route("/", methods=["GET", "POST"])
@login_required()
def sell():
    db = get_db()
    user = g.user
    if request.method == "POST":
        name = request.form["name"]
        whatsapp = request.form["whatsapp"]
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticket_code = generate_ticket_code()
        row = query(
            "INSERT INTO tickets (ticket_code, name, whatsapp, created_at, sold_by) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (ticket_code, name, whatsapp, created, user["id"]),
        ).fetchone()
        db.commit()
        ticket_id = row["id"]
        return redirect(url_for("ticket_issued", ticket_id=ticket_id))

    if user["role"] == "admin":
        stats = query("SELECT COUNT(*) AS c, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used_c, COUNT(*) * %s AS cash FROM tickets", (TICKET_PRICE,)).fetchone()
        recent = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC LIMIT 10").fetchall()
    else:
        stats = query("SELECT COUNT(*) AS c, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used_c, COUNT(*) * %s AS cash FROM tickets WHERE sold_by = %s", (TICKET_PRICE, user["id"])).fetchone()
        recent = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.sold_by = %s ORDER BY t.id DESC LIMIT 10", (user["id"],)).fetchall()

    return render_template("sell.html",
        total_sold=stats["c"] or 0, total_cash=stats["cash"] or 0, total_used=stats["used_c"] or 0,
        recent=recent, price=TICKET_PRICE, event_name=EVENT_NAME, user=user)

@app.route("/tickets")
@login_required()
def all_tickets():
    user = g.user
    q = request.args.get("q", "")
    if user["role"] == "admin":
        sql = "SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.name ILIKE %s OR t.whatsapp ILIKE %s OR t.id::text ILIKE %s ORDER BY t.id DESC"
        tickets = query(sql, (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    else:
        sql = "SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.sold_by = %s AND (t.name ILIKE %s OR t.whatsapp ILIKE %s OR t.id::text ILIKE %s) ORDER BY t.id DESC"
        tickets = query(sql, (user["id"], f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    return render_template("tickets.html", tickets=tickets, q=q, event_name=EVENT_NAME, user=user)

@app.route("/scan")
@login_required()
def scan():
    return render_template("scan.html", event_name=EVENT_NAME, user=g.user)

# Generate QR on the fly, no file save — works fine on Render's ephemeral disk
# since nothing is ever written to it.
@app.route("/qr/<int:ticket_id>.png")
@login_required()
def qr_image(ticket_id):
    verify_url = f"{request.host_url}api/verify/{ticket_id}"
    img = qrcode.make(verify_url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")

@app.route("/ticket_issued/<int:ticket_id>")
@login_required()
def ticket_issued(ticket_id):
    ticket = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.id = %s", (ticket_id,)).fetchone()
    if not ticket:
        return "Ticket not found", 404

    return render_template("ticket_issued.html",
                           ticket=ticket,
                           event_name=EVENT_NAME,
                           price=TICKET_PRICE,
                           qr_exists=True,
                           user=g.user)

def normalize_phone(raw):
    """Keep only digits, and compare on the last 10 (drops leading 0 / +234 / country code differences)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


@app.route("/check_ticket", methods=["POST"])
@login_required()
def check_ticket():
    raw = request.form.get("data", "").strip()
    db = get_db()

    lookup = raw
    if "://" in lookup:  # a full URL was scanned instead of a bare ID
        lookup = lookup.rstrip("/").rsplit("/", 1)[-1]

    ticket = None

    # 1) Try as a numeric ticket ID first
    if lookup.isdigit():
        ticket = query("SELECT * FROM tickets WHERE id = %s", (int(lookup),)).fetchone()

    # 2) Fall back to a phone-number lookup if no ID match
    if not ticket:
        target = normalize_phone(raw)
        if len(target) >= 7:  # avoid matching on tiny/garbage input
            all_tickets = query("SELECT * FROM tickets").fetchall()
            matches = [t for t in all_tickets if normalize_phone(t["whatsapp"]) == target]
            if len(matches) == 1:
                ticket = matches[0]
            elif len(matches) > 1:
                return {
                    "status": "MULTIPLE",
                    "msg": f"{len(matches)} tickets are registered to this number — pick the guest.",
                    "matches": [
                        {"id": t["id"], "name": t["name"], "used": bool(t["used"])} for t in matches
                    ],
                }

    if not ticket:
        return {"status": "INVALID", "msg": f"No ticket found for \"{raw}\""}

    if ticket["used"]:
        return {"status": "ALREADY USED", "msg": f"Already scanned at {ticket['used_at']}", "name": ticket["name"], "whatsapp": ticket["whatsapp"]}

    used_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s", (used_time, ticket["id"]))
    db.commit()
    return {"status": "VALID", "msg": "Entry Approved", "name": ticket["name"], "whatsapp": ticket["whatsapp"]}

@app.route("/api/verify/<int:ticket_id>")
@login_required()
def verify(ticket_id):
    db = get_db()
    ticket = query("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
    if not ticket:
        return {"status": "invalid"}
    if ticket["used"]:
        return {"status": "already_used", "name": ticket["name"], "time": ticket["used_at"]}
    used_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s", (used_time, ticket_id))
    db.commit()
    return {"status": "ok", "name": ticket["name"]}

@app.route("/export")
@login_required(role="admin")
def export_csv():
    tickets = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC").fetchall()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["ID", "Name", "WhatsApp", "Sold By", "Created At", "Used", "Used At"])
    for t in tickets:
        cw.writerow([t["id"], t["name"], t["whatsapp"], t["username"], t["created_at"], t["used"], t["used_at"]])
    output = si.getvalue()
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=tickets.csv"})

if __name__ == "__main__":
    app.run(debug=True)
