from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, flash, g
import os
import sqlite3
import uuid
import csv
import io
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash # NEW

# ─────────────────────────────────────────────────────────────
# CONFIG — edit these for your event
# ─────────────────────────────────────────────────────────────
EVENT_NAME = "BE Owambe Experience and Award Ceremony"
TICKET_PRICE = 3500  # naira
# ADMIN_USERNAME and PASSWORD are now in DB. Set first admin below

# ─────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "be-owambe-" + uuid.uuid4().hex)

DB_PATH = os.path.join(BASE_DIR, "tickets.db")
QR_DIR = os.path.join(BASE_DIR, "static", "qr")
os.makedirs(QR_DIR, exist_ok=True)

try:
    import qrcode
    QR_ENABLED = True
except ImportError:
    QR_ENABLED = False

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    conn = get_db()
    # 1. NEW USERS TABLE
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'seller' -- 'admin' or 'seller'
        )
    """)
    
    # 2. UPDATE TICKETS TABLE - add sold_by
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            whatsapp TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at TEXT,
            sold_by INTEGER, -- NEW
            FOREIGN KEY(sold_by) REFERENCES users(id)
        )
    """)
    
    # 3. Create default admin if no users exist
    user = conn.execute("SELECT * FROM users WHERE username = ?", ('admin',)).fetchone()
    if not user:
        hashed = generate_password_hash("admin123") # CHANGE THIS AFTER FIRST LOGIN
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                     ('admin', hashed, 'admin'))
        flash("Default admin created: username=admin, password=admin123. Please change it!", "warning")
        
    conn.commit()

init_db()

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('user') is None or g.user['role'] != 'admin':
            flash("You must be an admin to access this page.", "error")
            return redirect(url_for("sell"))
        return view(*args, **kwargs)
    return wrapped

@app.before_request
def load_logged_in_user():
    user_id = session.get('user_id')
    if user_id is None:
        g.user = None
    else:
        g.user = get_db().execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if g.user:
        return redirect(url_for("sell"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("sell"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = get_db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if user is None or not check_password_hash(user['password_hash'], password):
            error = "Wrong username or password."
        else:
            session.clear()
            sessiondef get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            whatsapp TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if session.get("logged_in"):
        return redirect(url_for("sell"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("sell"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("sell"))
        error = "Wrong username or password."

    return render_template("login.html", event_name=EVENT_NAME, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/sell", methods=["GET", "POST"])
@login_required
def sell():
    conn = get_db()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        whatsapp = request.form.get("whatsapp", "").strip()

        if not name or not whatsapp:
            flash("Name and WhatsApp number are both required.", "error")
            return redirect(url_for("sell"))

        ticket_id = uuid.uuid4().hex[:8].upper()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            "INSERT INTO tickets (id, name, whatsapp, amount, used, created_at) VALUES (?,?,?,?,0,?)",
            (ticket_id, name, whatsapp, TICKET_PRICE, created_at),
        )
        conn.commit()

        if QR_ENABLED:
            verify_url = request.url_root.rstrip("/") + url_for("view_ticket", ticket_id=ticket_id)
            img = qrcode.make(verify_url)
            img.save(os.path.join(QR_DIR, f"{ticket_id}.png"))

        conn.close()
        return redirect(url_for("issued", ticket_id=ticket_id))

    total_sold = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    total_cash = conn.execute("SELECT COALESCE(SUM(amount),0) FROM tickets").fetchone()[0]
    total_used = conn.execute("SELECT COUNT(*) FROM tickets WHERE used=1").fetchone()[0]
    recent = conn.execute(
        "SELECT * FROM tickets ORDER BY created_at DESC LIMIT 8"
    ).fetchall()
    conn.close()

    return render_template(
        "sell.html",
        event_name=EVENT_NAME,
        price=TICKET_PRICE,
        username=session.get("username"),
        total_sold=total_sold,
        total_cash=total_cash,
        total_used=total_used,
        recent=recent,
    )


@app.route("/issued/<ticket_id>")
@login_required
def issued(ticket_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    conn.close()
    if not row:
        return redirect(url_for("sell"))

    qr_exists = QR_ENABLED and os.path.exists(os.path.join(QR_DIR, f"{ticket_id}.png"))
    return render_template(
        "ticket_issued.html",
        event_name=EVENT_NAME,
        price=TICKET_PRICE,
        ticket=row,
        qr_exists=qr_exists,
    )


@app.route("/tickets")
@login_required
def all_tickets():
    q = request.args.get("q", "").strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE name LIKE ? OR whatsapp LIKE ? OR id LIKE ? ORDER BY created_at DESC",
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("tickets.html", event_name=EVENT_NAME, tickets=rows, q=q)


@app.route("/export.csv")
@login_required
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tickets ORDER BY created_at").fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Ticket ID", "Name", "WhatsApp", "Amount", "Used", "Created At", "Used At"])
    for r in rows:
        writer.writerow([r["id"], r["name"], r["whatsapp"], r["amount"],
                          "Yes" if r["used"] else "No", r["created_at"], r["used_at"] or ""])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=owambe_tickets.csv"},
    )


@app.route("/scan")
@login_required
def scan():
    return render_template("scan.html", event_name=EVENT_NAME)


@app.route("/check_ticket", methods=["POST"])
@login_required
def check_ticket():
    ticket_id = (request.form.get("data") or "").strip().upper()

    # Allow pasting a full verification URL, not just the raw ID
    if "/ticket/" in ticket_id:
        ticket_id = ticket_id.rstrip("/").split("/ticket/")[-1].upper()

    conn = get_db()
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    if not row:
        conn.close()
        return jsonify(status="INVALID", msg="This ticket ID was not found. Not sold at the door.")

    if row["used"]:
        conn.close()
        return jsonify(
            status="ALREADY USED",
            msg=f"Already checked in at {row['used_at']}.",
            name=row["name"],
            whatsapp=row["whatsapp"],
        )

    used_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE tickets SET used=1, used_at=? WHERE id=?", (used_at, ticket_id))
    conn.commit()
    conn.close()

    return jsonify(
        status="VALID",
        msg="Checked in successfully.",
        name=row["name"],
        whatsapp=row["whatsapp"],
    )


@app.route("/ticket/<ticket_id>")
def view_ticket(ticket_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id.upper(),)).fetchone()
    conn.close()
    return render_template("ticket.html", event_name=EVENT_NAME, ticket=row, ticket_id=ticket_id.upper())


if __name__ == "__main__":
    app.run(debug=True)
