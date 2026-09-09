from flask import Flask, render_template, request, redirect, url_for, session, g, flash, Response
import os, io, csv, secrets, string, re
from datetime import datetime
from zoneinfo import ZoneInfo
import qrcode
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-render")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
SSL_MODE = os.environ.get("DB_SSLMODE", "require")

EVENT_NAME = "BE Owambe"
DEFAULT_TICKET_PRICE = 3500
LAGOS_TZ = ZoneInfo("Africa/Lagos")
TICKET_ALPHABET = string.ascii_uppercase + string.digits

def generate_ticket_code():
    # Non-sequential, case-insensitive-safe code with enough entropy to resist guessing.
    return "OW-" + "".join(secrets.choice(TICKET_ALPHABET) for _ in range(12))


def unique_ticket_code():
    code = generate_ticket_code()
    while query("SELECT 1 FROM tickets WHERE ticket_code = %s", (code,)).fetchone():
        code = generate_ticket_code()
    return code




def lagos_now():
    return datetime.now(LAGOS_TZ)


def lagos_timestamp():
    return lagos_now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------ DB HELPERS ------------------
def get_db():
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured.")
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
            role TEXT DEFAULT 'seller',
            active BOOLEAN NOT NULL DEFAULT TRUE
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
            sold_by INTEGER REFERENCES users (id),
            amount_paid INTEGER NOT NULL DEFAULT 3500,
            qr_data TEXT
        )
    """)
    query("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Safe upgrades for installations created by earlier versions.
    query("ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS amount_paid INTEGER NOT NULL DEFAULT 3500")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS qr_data TEXT")
    query("CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_ticket_code ON tickets(ticket_code)")

    # Any legacy ticket without a code gets a secure code before the code is used operationally.
    missing = query("SELECT id FROM tickets WHERE ticket_code IS NULL OR ticket_code = ''").fetchall()
    for row in missing:
        code = generate_ticket_code()
        while query("SELECT 1 FROM tickets WHERE ticket_code = %s", (code,)).fetchone():
            code = generate_ticket_code()
        query("UPDATE tickets SET ticket_code = %s WHERE id = %s", (code, row["id"]))

    # Store the QR payload in PostgreSQL rather than storing QR image files on disk.
    # The payload is the unpredictable public ticket code; the PNG is generated on demand.
    query("UPDATE tickets SET qr_data = ticket_code WHERE qr_data IS NULL OR qr_data = ''")

    # Preserve the historical default price, but make it editable without a code deploy.
    query("""
        INSERT INTO app_settings (key, value) VALUES ('ticket_price', %s)
        ON CONFLICT (key) DO NOTHING
    """, (str(DEFAULT_TICKET_PRICE),))
    db.commit()

    user = query("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if not user:
        query(
            "INSERT INTO users (username, password_hash, role, active) VALUES (%s,%s,%s,%s)",
            ("admin", generate_password_hash("admin123"), "admin", True),
        )
        db.commit()
        print("Default admin created: admin / admin123")


def get_ticket_price():
    row = query("SELECT value FROM app_settings WHERE key = 'ticket_price'").fetchone()
    try:
        return int(row["value"]) if row else DEFAULT_TICKET_PRICE
    except (TypeError, ValueError):
        return DEFAULT_TICKET_PRICE


with app.app_context():
    init_db()



def current_user():
    if "user_id" not in session:
        return None
    return query("SELECT * FROM users WHERE id = %s", (session["user_id"],)).fetchone()


def login_required(role=None):
    def decorator(f):
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user or not user["active"]:
                session.clear()
                if user and not user["active"]:
                    flash("This seller account has been removed. Please contact an administrator.", "error")
                return redirect(url_for("login"))
            if role and user["role"] != role:
                flash("You don't have permission for that page", "error")
                return redirect(url_for("home"))
            g.user = user
            return f(*args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator


# ------------------ AUTH ROUTES ------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query("SELECT * FROM users WHERE username = %s", (username,)).fetchone()
        if user and user["active"] and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("home"))
        error = "Invalid username or password"
        if user and not user["active"]:
            error = "This seller account has been removed. Contact an administrator."
        return render_template("login.html", error=error, event_name=EVENT_NAME)
    return render_template("login.html", event_name=EVENT_NAME)


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
            query("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new), user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("home"))
    return render_template("change_password.html", event_name=EVENT_NAME, user=user)


# ------------------ ADMIN ROUTES ------------------
@app.route("/register", methods=["GET", "POST"])
@login_required(role="admin")
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "seller")
        if not username or len(password) < 6 or role not in ("seller", "admin"):
            flash("Enter a username, a password of at least 6 characters, and a valid role.", "error")
            return render_template("register.html", event_name=EVENT_NAME, user=g.user)
        db = get_db()
        try:
            query("INSERT INTO users (username, password_hash, role, active) VALUES (%s,%s,%s,%s)",
                  (username, generate_password_hash(password), role, True))
            db.commit()
            flash(f"User {username} created successfully", "success")
            return redirect(url_for("report"))
        except psycopg2.IntegrityError:
            db.rollback()
            flash("Username already exists", "error")
    return render_template("register.html", event_name=EVENT_NAME, user=g.user)


@app.route("/admin/remove_seller/<int:user_id>", methods=["POST"])
@login_required(role="admin")
def remove_seller(user_id):
    seller = query("SELECT * FROM users WHERE id = %s AND role = 'seller'", (user_id,)).fetchone()
    if not seller:
        flash("Seller not found.", "error")
    elif seller["id"] == g.user["id"]:
        flash("You cannot remove your own account from here.", "error")
    else:
        query("UPDATE users SET active = FALSE WHERE id = %s", (user_id,))
        get_db().commit()
        flash(f"Seller {seller['username']} was removed. Their records remain available.", "success")
    return redirect(url_for("report"))


@app.route("/admin/restore_seller/<int:user_id>", methods=["POST"])
@login_required(role="admin")
def restore_seller(user_id):
    seller = query("SELECT * FROM users WHERE id = %s AND role = 'seller'", (user_id,)).fetchone()
    if not seller:
        flash("Seller not found.", "error")
    else:
        query("UPDATE users SET active = TRUE WHERE id = %s", (user_id,))
        get_db().commit()
        flash(f"Seller {seller['username']} has been restored.", "success")
    return redirect(url_for("report"))


@app.route("/admin/price", methods=["POST"])
@login_required(role="admin")
def update_price():
    raw = request.form.get("ticket_price", "").replace(",", "").strip()
    try:
        price = int(raw)
        if price < 0:
            raise ValueError
    except ValueError:
        flash("Ticket price must be a valid non-negative amount.", "error")
        return redirect(url_for("report"))
    query("INSERT INTO app_settings (key, value) VALUES ('ticket_price', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(price),))
    get_db().commit()
    flash("Ticket price updated. New sales will use the new price.", "success")
    return redirect(url_for("report"))


@app.route("/admin/reset_tickets", methods=["POST"])
@login_required(role="admin")
def reset_tickets():
    confirmation = request.form.get("confirmation", "").strip().upper()
    if confirmation != "RESET":
        flash("Ticket history was not cleared. Type RESET to confirm.", "error")
        return redirect(url_for("report"))
    query("DELETE FROM tickets")
    get_db().commit()
    flash("All ticket history has been cleared. Users and seller accounts were preserved.", "success")
    return redirect(url_for("report"))


@app.route("/report")
@login_required(role="admin")
def report():
    price = get_ticket_price()
    sales = query("""
        SELECT u.id, u.username, u.role, u.active,
               COUNT(t.id) AS tickets_sold,
               COALESCE(SUM(t.amount_paid),0) AS total_cash,
               COALESCE(SUM(CASE WHEN t.used THEN 1 ELSE 0 END),0) AS tickets_used
        FROM users u
        LEFT JOIN tickets t ON u.id = t.sold_by
        GROUP BY u.id
        ORDER BY u.role, u.active DESC, u.username
    """).fetchall()
    users = query("SELECT id, username, role, active FROM users ORDER BY role, active DESC, username").fetchall()
    overall = query("""
        SELECT COUNT(*) AS sold,
               COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used,
               COALESCE(SUM(amount_paid),0) AS cash
        FROM tickets
    """).fetchone()
    return render_template("report.html", sales=sales, users=users, overall=overall,
                           ticket_price=price, event_name=EVENT_NAME, user=g.user)


# ------------------ MAIN APP ROUTES ------------------
@app.route("/")
@login_required()
def home():
    user = g.user
    overall = query("""
        SELECT COUNT(*) AS sold,
               COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used,
               COALESCE(SUM(amount_paid),0) AS cash
        FROM tickets
    """).fetchone()
    mine = query("""
        SELECT COUNT(*) AS sold,
               COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used,
               COALESCE(SUM(amount_paid),0) AS cash
        FROM tickets WHERE sold_by = %s
    """, (user["id"],)).fetchone()
    return render_template("home.html", overall=overall, mine=mine, event_name=EVENT_NAME, user=user,
                           ticket_price=get_ticket_price())


@app.route("/sell", methods=["GET", "POST"])
@login_required()
def sell():
    user = g.user
    price = get_ticket_price()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        whatsapp = request.form.get("whatsapp", "").strip()
        if not name or not whatsapp:
            flash("Guest name and WhatsApp number are required.", "error")
            return redirect(url_for("sell"))
        created = lagos_timestamp()
        ticket_code = unique_ticket_code()
        row = query(
            "INSERT INTO tickets (ticket_code, name, whatsapp, created_at, sold_by, amount_paid, qr_data) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (ticket_code, name, whatsapp, created, user["id"], price, ticket_code),
        ).fetchone()
        get_db().commit()
        return redirect(url_for("ticket_issued", ticket_id=row["id"]))

    overall = query("""
        SELECT COUNT(*) AS sold,
               COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used,
               COALESCE(SUM(amount_paid),0) AS cash
        FROM tickets
    """).fetchone()
    mine = query("""
        SELECT COUNT(*) AS sold,
               COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used,
               COALESCE(SUM(amount_paid),0) AS cash
        FROM tickets WHERE sold_by = %s
    """, (user["id"],)).fetchone()
    # Deliberately only the two most recent tickets for the quick view.
    recent = query("""
        SELECT t.*, u.username FROM tickets t
        LEFT JOIN users u ON t.sold_by = u.id
        ORDER BY t.id DESC LIMIT 2
    """).fetchall()
    return render_template("sell.html", overall=overall, mine=mine, recent=recent,
                           price=price, event_name=EVENT_NAME, user=user)


@app.route("/tickets")
@login_required()
def all_tickets():
    q = request.args.get("q", "").strip()
    # Every seller/admin can search the complete ticket catalogue, regardless of seller.
    sql = """
        SELECT t.*, u.username FROM tickets t
        LEFT JOIN users u ON t.sold_by = u.id
        WHERE t.name ILIKE %s
           OR t.whatsapp ILIKE %s
           OR COALESCE(t.ticket_code,'') ILIKE %s
           OR t.id::text ILIKE %s
        ORDER BY t.id DESC
    """
    like = f"%{q}%"
    tickets = query(sql, (like, like, like, like)).fetchall()
    return render_template("tickets.html", tickets=tickets, q=q, event_name=EVENT_NAME, user=g.user)


@app.route("/scan")
@login_required()
def scan():
    return render_template("scan.html", event_name=EVENT_NAME, user=g.user)


# Generate QR images on demand. The QR payload itself is stored in PostgreSQL
# (qr_data); no QR image files are written to static/qr or the server filesystem.
@app.route("/qr/<path:ticket_code>.png")
@login_required()
def qr_image(ticket_code):
    ticket = query(
        "SELECT ticket_code, qr_data FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)",
        (ticket_code,),
    ).fetchone()
    if not ticket:
        return "Ticket not found", 404

    payload = ticket["qr_data"] or ticket["ticket_code"]
    verify_url = f"{request.host_url}api/verify/{ticket['ticket_code']}"
    # The QR carries the ticket code directly. The scanner can validate it through
    # /check_ticket, while the verification URL remains useful for compatible readers.
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="image/png",
        headers={"Content-Disposition": f'inline; filename="owambe_{ticket["ticket_code"]}.png"'},
    )


@app.route("/ticket_issued/<int:ticket_id>")
@login_required()
def ticket_issued(ticket_id):
    ticket = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.id = %s", (ticket_id,)).fetchone()
    if not ticket:
        return "Ticket not found", 404
    return render_template("ticket_issued.html", ticket=ticket, event_name=EVENT_NAME,
                           price=ticket["amount_paid"], qr_exists=True, user=g.user)


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


@app.route("/check_ticket", methods=["POST"])
@login_required()
def check_ticket():
    raw = request.form.get("data", "").strip()
    lookup = raw
    if "http://" in lookup or "https://" in lookup:
        lookup = lookup.rstrip("/").rsplit("/", 1)[-1]

    ticket = None
    # QR/public-code lookup first. The numeric DB primary key is intentionally not the public ticket ID.
    if lookup:
        ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (lookup,)).fetchone()

    # Backwards-compatible manual lookup for an old numeric ID, without exposing it on new tickets.
    if not ticket and lookup.isdigit():
        ticket = query("SELECT * FROM tickets WHERE id = %s", (int(lookup),)).fetchone()

    # Phone-number lookup remains available for gate staff.
    if not ticket:
        target = normalize_phone(raw)
        if len(target) >= 7:
            all_rows = query("SELECT * FROM tickets").fetchall()
            matches = [t for t in all_rows if normalize_phone(t["whatsapp"]) == target]
            if len(matches) == 1:
                ticket = matches[0]
            elif len(matches) > 1:
                return {
                    "status": "MULTIPLE",
                    "msg": f"{len(matches)} tickets are registered to this number — pick the guest.",
                    "matches": [{"id": t["id"], "ticket_code": t["ticket_code"], "name": t["name"], "used": bool(t["used"])} for t in matches],
                }

    if not ticket:
        return {"status": "INVALID", "msg": f"No ticket found for \"{raw}\""}

    if ticket["used"]:
        return {
            "status": "ALREADY USED",
            "msg": f"Already scanned at {ticket['used_at']} (Lagos time)",
            "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]
        }

    used_time = lagos_timestamp()
    query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s", (used_time, ticket["id"]))
    get_db().commit()
    return {
        "status": "VALID", "msg": "Entry Approved", "name": ticket["name"],
        "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]
    }


@app.route("/api/verify/<path:ticket_code>")
@login_required()
def verify(ticket_code):
    db = get_db()
    ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (ticket_code,)).fetchone()
    if not ticket:
        return {"status": "invalid"}
    if ticket["used"]:
        return {"status": "already_used", "name": ticket["name"], "time": ticket["used_at"]}
    used_time = lagos_timestamp()
    query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s", (used_time, ticket["id"]))
    db.commit()
    return {"status": "ok", "name": ticket["name"], "ticket_code": ticket["ticket_code"]}


@app.route("/export")
@login_required(role="admin")
def export_csv():
    tickets = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC").fetchall()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["Ticket Code", "Name", "WhatsApp", "Sold By", "Amount Paid", "Created At", "Used", "Used At"])
    for t in tickets:
        cw.writerow([t["ticket_code"], t["name"], t["whatsapp"], t["username"], t["amount_paid"], t["created_at"], t["used"], t["used_at"]])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=owambe_tickets.csv"})


if __name__ == "__main__":
    app.run(debug=True)
