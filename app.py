from flask import Flask, render_template, request, redirect, url_for, session, g, flash, Response, abort
import os, io, csv, secrets, string, re, urllib.parse, base64
from datetime import datetime
from zoneinfo import ZoneInfo
import qrcode
import psycopg2
from functools import wraps
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY must be configured in production")
limiter = Limiter(key_func=get_remote_address, default_limits=["300 per hour"], storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"))
limiter.init_app(app)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1", SESSION_COOKIE_SAMESITE="Lax")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
SSL_MODE = os.environ.get("DB_SSLMODE", "require")

EVENT_NAME = "Bioelites Class of 26' Owambe Experience and Award Ceremony"
DEFAULT_TICKET_PRICE = 3500
DEFAULT_COMMISSION_RATE = 5  # percent
LAGOS_TZ = ZoneInfo("Africa/Lagos")
TICKET_ALPHABET = string.ascii_uppercase + string.digits

# Ticket categories: label -> number of guest seats it covers
CATEGORY_SEATS = {
    "Regular": 1,
    "Table of 4": 4,
    "Table of 5": 5,
    "Table of 6": 6,
    "Table of 7": 7,
    "Table of 8": 8,
}
# label -> app_settings key slug used for per-category pricing
CATEGORY_SLUGS = {
    "Regular": "regular",
    "Table of 4": "table4",
    "Table of 5": "table5",
    "Table of 6": "table6",
    "Table of 7": "table7",
    "Table of 8": "table8",
}
# Only table categories (seats > 1) are eligible for walk-in creation / upgrades.
WALKIN_CATEGORY_SLUGS = {label: slug for label, slug in CATEGORY_SLUGS.items() if CATEGORY_SEATS[label] > 1}

def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token
app.jinja_env.globals["csrf_token"] = csrf_token

@app.before_request
def protect_requests():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not supplied or not secrets.compare_digest(supplied, session.get("csrf_token", "")):
            abort(400, description="Invalid or missing CSRF token")

def generate_ticket_code():
    return "OW-" + "".join(secrets.choice(TICKET_ALPHABET) for _ in range(12))

def unique_ticket_code():
    code = generate_ticket_code()
    while query("SELECT 1 FROM tickets WHERE ticket_code = %s", (code,)).fetchone():
        code = generate_ticket_code()
    return code

def generate_table_id():
    return "TBL-" + "".join(secrets.choice(TICKET_ALPHABET) for _ in range(10))

def unique_table_id():
    tid = generate_table_id()
    while (query("SELECT 1 FROM tickets WHERE table_id = %s", (tid,)).fetchone()
           or query("SELECT 1 FROM walkin_tables WHERE table_ref = %s", (tid,)).fetchone()):
        tid = generate_table_id()
    return tid

def next_table_number():
    """Friendly sequential number ('Table 1', 'Table 2', ...) shown to
    handlers and guests. table_id stays internal/backend-only."""
    return query("SELECT nextval('table_number_seq') AS n").fetchone()["n"]

def lagos_now():
    return datetime.now(LAGOS_TZ)

def lagos_timestamp():
    return lagos_now().strftime("%Y-%m-%d %H:%M:%S")

def get_db():
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured.")
        g.db = psycopg2.connect(DATABASE_URL, sslmode=SSL_MODE, cursor_factory=psycopg2.extras.RealDictCursor)
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
    query("""CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT DEFAULT 'seller', active BOOLEAN NOT NULL DEFAULT TRUE)""")
    query("""CREATE TABLE IF NOT EXISTS tickets (id SERIAL PRIMARY KEY, ticket_code TEXT UNIQUE, name TEXT NOT NULL, whatsapp TEXT NOT NULL, seat TEXT DEFAULT 'General', category TEXT NOT NULL DEFAULT 'Regular', table_id TEXT, used BOOLEAN DEFAULT FALSE, created_at TEXT, used_at TEXT, sold_by INTEGER REFERENCES users (id), amount_paid INTEGER NOT NULL DEFAULT 3500, qr_data TEXT)""")
    query("""CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    query("""CREATE TABLE IF NOT EXISTS ticket_audit (id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets (id), action TEXT NOT NULL, performed_by INTEGER REFERENCES users (id), detail TEXT, created_at TEXT)""")
    query("""CREATE TABLE IF NOT EXISTS walkin_tables (id SERIAL PRIMARY KEY, table_ref TEXT UNIQUE NOT NULL, category TEXT NOT NULL, guest_names TEXT NOT NULL, amount_paid INTEGER NOT NULL DEFAULT 0, created_by INTEGER REFERENCES users(id), created_at TEXT)""")
    query("ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS amount_paid INTEGER NOT NULL DEFAULT 3500")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS qr_data TEXT")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS seat TEXT DEFAULT 'General'")
    query("CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_ticket_code ON tickets(ticket_code)")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS cancelled BOOLEAN NOT NULL DEFAULT FALSE")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS cancelled_at TEXT")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS cancelled_by INTEGER REFERENCES users(id)")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS cancel_reason TEXT")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS refunded BOOLEAN NOT NULL DEFAULT FALSE")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS refund_amount INTEGER")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS refunded_at TEXT")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS refunded_by INTEGER REFERENCES users(id)")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_sent_at TEXT")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS send_count INTEGER NOT NULL DEFAULT 0")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Regular'")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS table_id TEXT")
    query("CREATE INDEX IF NOT EXISTS idx_tickets_table_id ON tickets(table_id)")
    query("CREATE SEQUENCE IF NOT EXISTS table_number_seq START 1")
    query("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS table_number INTEGER")
    query("ALTER TABLE walkin_tables ADD COLUMN IF NOT EXISTS table_number INTEGER")
    missing = query("SELECT id FROM tickets WHERE ticket_code IS NULL OR ticket_code = ''").fetchall()
    for row in missing:
        code = generate_ticket_code()
        while query("SELECT 1 FROM tickets WHERE ticket_code = %s", (code,)).fetchone():
            code = generate_ticket_code()
        query("UPDATE tickets SET ticket_code = %s WHERE id = %s", (code, row["id"]))
    query("UPDATE tickets SET qr_data = ticket_code WHERE qr_data IS NULL OR qr_data = ''")
    # Assign friendly "Table N" numbers to any existing tables that predate
    # this feature, in chronological order, so numbering feels natural.
    unnumbered_ticket_tables = query(
        "SELECT table_id, MIN(created_at) AS first_created FROM tickets WHERE table_id IS NOT NULL AND table_number IS NULL GROUP BY table_id"
    ).fetchall()
    unnumbered_walkin_tables = query(
        "SELECT id, created_at FROM walkin_tables WHERE table_number IS NULL"
    ).fetchall()
    to_number = [("ticket", r["table_id"], r["first_created"] or "") for r in unnumbered_ticket_tables]
    to_number += [("walkin", r["id"], r["created_at"] or "") for r in unnumbered_walkin_tables]
    to_number.sort(key=lambda x: x[2])
    for kind, ref, _ in to_number:
        num = query("SELECT nextval('table_number_seq') AS n").fetchone()["n"]
        if kind == "ticket":
            query("UPDATE tickets SET table_number = %s WHERE table_id = %s", (num, ref))
        else:
            query("UPDATE walkin_tables SET table_number = %s WHERE id = %s", (num, ref))
    query("INSERT INTO app_settings (key, value) VALUES ('ticket_price', %s) ON CONFLICT (key) DO NOTHING", (str(DEFAULT_TICKET_PRICE),))
    for slug in CATEGORY_SLUGS.values():
        query("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (f"price_{slug}", str(DEFAULT_TICKET_PRICE)))
    query("INSERT INTO app_settings (key, value) VALUES ('commission_rate', %s) ON CONFLICT (key) DO NOTHING", (str(DEFAULT_COMMISSION_RATE),))
    for slug in WALKIN_CATEGORY_SLUGS.values():
        query("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (f"walkin_price_{slug}", str(DEFAULT_TICKET_PRICE)))
    query("INSERT INTO app_settings (key, value) VALUES ('max_tickets', %s) ON CONFLICT (key) DO NOTHING", ("",))  # empty = no cap
    db.commit()
    user = query("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if not user:
        query("INSERT INTO users (username, password_hash, role, active) VALUES (%s,%s,%s,%s)", ("admin", generate_password_hash("admin123"), "admin", True))
        db.commit()
        print("Default admin created: admin / admin123")

def get_ticket_prices():
    # FIX: the '%' in 'price_%' was being swallowed by psycopg2's %s-placeholder
    # parser because it was hardcoded into the SQL string with no matching param.
    # Passing the pattern as a bound parameter avoids that entirely.
    rows = query("SELECT key, value FROM app_settings WHERE key LIKE %s", ("price_%",)).fetchall()
    existing = {r["key"]: r["value"] for r in rows}
    prices = {}
    for label, slug in CATEGORY_SLUGS.items():
        try:
            prices[label] = int(existing.get(f"price_{slug}", DEFAULT_TICKET_PRICE))
        except (TypeError, ValueError):
            prices[label] = DEFAULT_TICKET_PRICE
    return prices

def get_ticket_price():
    # Kept for backward compatibility (home.html etc.) — returns the Regular price.
    return get_ticket_prices().get("Regular", DEFAULT_TICKET_PRICE)

def get_commission_rate():
    row = query("SELECT value FROM app_settings WHERE key = 'commission_rate'").fetchone()
    if not row:
        return DEFAULT_COMMISSION_RATE
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_COMMISSION_RATE

def get_walkin_prices():
    """Separate price list for admin-created walk-in tables — independent
    of the pre-sold ticket prices in get_ticket_prices()."""
    rows = query("SELECT key, value FROM app_settings WHERE key LIKE %s", ("walkin_price_%",)).fetchall()
    existing = {r["key"]: r["value"] for r in rows}
    prices = {}
    for label, slug in WALKIN_CATEGORY_SLUGS.items():
        try:
            prices[label] = int(existing.get(f"walkin_price_{slug}", DEFAULT_TICKET_PRICE))
        except (TypeError, ValueError):
            prices[label] = DEFAULT_TICKET_PRICE
    return prices

def get_max_tickets():
    """Overall cap on pre-sold tickets (Regular + table categories). Walk-in
    tables never count toward this. Returns None when there is no cap."""
    row = query("SELECT value FROM app_settings WHERE key = 'max_tickets'").fetchone()
    if not row or not row["value"].strip():
        return None
    try:
        val = int(row["value"])
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None

def get_active_ticket_count():
    row = query("SELECT COUNT(*) AS cnt FROM tickets WHERE cancelled = FALSE").fetchone()
    return row["cnt"]

def get_walkin_cash_total():
    row = query("SELECT COALESCE(SUM(amount_paid),0) AS total FROM walkin_tables").fetchone()
    return row["total"]

def log_audit(ticket_id, action, performed_by, detail=""):
    query("INSERT INTO ticket_audit (ticket_id, action, performed_by, detail, created_at) VALUES (%s,%s,%s,%s,%s)", (ticket_id, action, performed_by, detail, lagos_timestamp()))

def get_ticket_or_404(ticket_id):
    ticket = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.id = %s", (ticket_id,)).fetchone()
    if not ticket:
        abort(404)
    return ticket

def whatsapp_dial_number(raw):
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("234"):
        return digits
    if digits.startswith("0") and len(digits) == 11:
        return "234" + digits[1:]
    if len(digits) == 10:
        return "234" + digits
    return digits

def build_ticket_pdf(ticket):
    width, height = 105 * mm, 170 * mm
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=(width, height))

    # 1. BACKGROUND
    try:
        bg_img = ImageReader("static/img/owambe-flyer.jpg")
        c.drawImage(bg_img, 0, 0, width=width, height=height, mask="auto")
    except:
        c.setFillColor(HexColor("#0b1f2b"))
        c.rect(0, 0, width, height, fill=1, stroke=0)

    # 2. DARK OVERLAY
    c.setFillColor(HexColor("#0b1f2b"))
    c.setFillAlpha(0.88)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillAlpha(1.0)

    # 3. LOGO
    logo_w = 48 * mm
    logo_h = 16 * mm
    logo_x = (width - logo_w) / 2
    logo_y = height - logo_h - 5 * mm
    try:
        logo_img = ImageReader("static/img/owambe-logo.png")
        c.drawImage(logo_img, logo_x, logo_y, width=logo_w, height=logo_h, mask="auto")
    except:
        pass

    # 4. GOLD BORDER
    card_x = 6 * mm
    card_y = 7 * mm
    card_w = width - 12 * mm
    card_h = logo_y - card_y - 5 * mm
    c.setStrokeColor(HexColor("#D4AF37"))
    c.setLineWidth(2.2)
    c.roundRect(card_x, card_y, card_w, card_h, 14, fill=0, stroke=1)

    # 5. QR CODE
    qr_data = f"{request.host_url}t/{ticket['ticket_code']}/pdf"
    qr_img = qrcode.make(qr_data)
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_size = 43 * mm
    qr_y = card_y + card_h - qr_size - 7 * mm
    qr_box_x = (width - qr_size) / 2 - 3 * mm
    qr_box_y = qr_y - 3 * mm
    qr_box_w = qr_size + 6 * mm
    qr_box_h = qr_size + 6 * mm
    c.setFillColor(HexColor("#ffffff"))
    c.roundRect(qr_box_x, qr_box_y, qr_box_w, qr_box_h, 9, fill=1, stroke=0)
    c.setStrokeColor(HexColor("#D4AF37"))
    c.setLineWidth(1.6)
    c.roundRect(qr_box_x, qr_box_y, qr_box_w, qr_box_h, 9, fill=0, stroke=1)
    c.drawImage(ImageReader(qr_buf), (width - qr_size) / 2, qr_y, width=qr_size, height=qr_size, mask="auto")

    # 6. DETAILS BOX
    details_top = qr_y - 6 * mm
    details_height = 52 * mm
    c.setFillColor(HexColor("#000"))
    c.setFillAlpha(0.35)
    c.roundRect(10 * mm, details_top - details_height, width - 20 * mm, details_height, 8, fill=1, stroke=0)
    c.setFillAlpha(1.0)

    def field(y, label, value):
        c.setFillColor(HexColor("#42d7e9"))
        c.setFont("Helvetica", 6.8)
        c.drawString(14 * mm, y, label)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(14 * mm, y - 4.2 * mm, str(value)[:38])
        return y - 10.2 * mm

    category = ticket.get("category") or "Regular"
    if ticket.get("table_id"):
        table_label = f"Table {ticket.get('table_number')}" if ticket.get("table_number") else "Table"
        ticket_type_display = f"{category} \u2022 {table_label} \u2022 {ticket.get('seat', '')}"
    else:
        ticket_type_display = category

    y = details_top - 5.5 * mm
    y = field(y, "GUEST NAME", ticket["name"])
    y = field(y, "TICKET CODE", ticket["ticket_code"])
    y = field(y, "WHATSAPP", ticket["whatsapp"])
    y = field(y, "TICKET TYPE", ticket_type_display)
    y = field(y, "AMOUNT PAID", f"NGN {ticket['amount_paid']:,}")

    # 7. FOOTER - positioned just below the details box
    # Keep the dashed line below the full details area, then keep
    # all footer elements below that line at the same relative spacing.
    footer_divider_y = details_top - details_height - 4 * mm

    c.setStrokeColor(HexColor("#D4AF37"))
    c.setLineWidth(1)
    c.setDash(3, 3)
    c.line(10 * mm, footer_divider_y, width - 10 * mm, footer_divider_y)
    c.setDash()

    gate_y = footer_divider_y - 4 * mm

    c.setFillColor(HexColor("#D4AF37"))
    c.setFont("Helvetica-Bold", 7.5)
    c.drawCentredString(width / 2, gate_y, "PRESENT TICKET AT THE GATE")

    issued_y = gate_y - 5.5 * mm

    c.setFillColor(HexColor("#a8c1c8"))
    c.setFont("Helvetica", 5.8)
    c.drawCentredString(width / 2, issued_y, f"Issued: {ticket['created_at']} | Seller: {ticket.get('username', '')}")

    # Security warning
    warning_y = issued_y - 5 * mm
    c.setFillColor(HexColor("#ed5b63"))
    c.setFont("Helvetica-Bold", 5.7)
    c.drawCentredString(width / 2, warning_y, "WARNING: DO NOT SHARE YOUR TICKET DETAILS.")

    c.setFillColor(HexColor("#a8c1c8"))
    c.setFont("Helvetica", 5.1)
    c.drawCentredString(width / 2, warning_y - 3 * mm, "Sharing your ticket details may allow someone else to steal or use your ticket.")

    # 8. CANCELLED WATERMARK
    if ticket.get("cancelled"):
        c.saveState()
        c.setFillColor(HexColor("#ed5b63"))
        c.setFont("Helvetica-Bold", 32)
        c.translate(width / 2, height / 2)
        c.rotate(20)
        c.drawCentredString(0, 0, "CANCELLED")
        c.restoreState()

    c.showPage()
    c.save()
    buf.seek(0)
    return buf

with app.app_context():
    init_db()

def current_user():
    if "user_id" not in session:
        return None
    return query("SELECT * FROM users WHERE id = %s", (session["user_id"],)).fetchone()

def original_admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user["active"] or user["role"]!= "admin":
            session.clear() if not user or not user["active"] else None
            flash("Only the original administrator can access this feature.", "error")
            return redirect(url_for("home"))
        original = query("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not original or user["id"]!= original["id"]:
            flash("Only the original administrator can access this feature.", "error")
            return redirect(url_for("home"))
        g.user = user
        return f(*args, **kwargs)
    return wrapper

def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user or not user["active"]:
                session.clear()
                if user and not user["active"]:
                    flash("This seller account has been removed. Please contact an administrator.", "error")
                return redirect(url_for("login"))
            if role and user["role"]!= role:
                flash("You don't have permission for that page", "error")
                return redirect(url_for("home"))
            g.user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
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
        elif new!= confirm:
            flash("New password and confirmation don't match.", "error")
        else:
            db = get_db()
            query("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new), user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("home"))
    return render_template("change_password.html", event_name=EVENT_NAME, user=user)

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
            query("INSERT INTO users (username, password_hash, role, active) VALUES (%s,%s,%s,%s)", (username, generate_password_hash(password), role, True))
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

@app.route("/admin/remove_admin/<int:user_id>", methods=["POST"])
@original_admin_required
def remove_admin(user_id):
    admin = query("SELECT * FROM users WHERE id = %s AND role = 'admin'", (user_id,)).fetchone()
    original = query("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if not admin or admin["id"] == original["id"]:
        flash("That administrator cannot be removed.", "error")
    else:
        query("UPDATE users SET active = FALSE WHERE id = %s", (user_id,))
        get_db().commit()
        flash(f"Admin {admin['username']} was removed.", "success")
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
    updates = {}
    for label, slug in CATEGORY_SLUGS.items():
        raw = request.form.get(f"price_{slug}", "").replace(",", "").strip()
        if raw == "":
            continue
        try:
            price = int(raw)
            if price < 0:
                raise ValueError
        except ValueError:
            flash(f"Price for {label} must be a valid non-negative amount.", "error")
            return redirect(url_for("report"))
        updates[slug] = price
    if not updates:
        flash("No prices were submitted.", "error")
        return redirect(url_for("report"))
    for slug, price in updates.items():
        query("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"price_{slug}", str(price)))
    get_db().commit()
    flash("Ticket prices updated. New sales will use the new prices.", "success")
    return redirect(url_for("report"))

@app.route("/admin/commission", methods=["POST"])
@login_required(role="admin")
def update_commission():
    raw = request.form.get("commission_rate", "").strip()
    try:
        rate = float(raw)
        if rate < 0 or rate > 100:
            raise ValueError
    except ValueError:
        flash("Commission rate must be a number between 0 and 100.", "error")
        return redirect(url_for("report"))
    query("INSERT INTO app_settings (key, value) VALUES ('commission_rate', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(rate),))
    get_db().commit()
    flash("Commission rate updated.", "success")
    return redirect(url_for("report"))

@app.route("/admin/max_tickets", methods=["POST"])
@login_required(role="admin")
def update_max_tickets():
    raw = request.form.get("max_tickets", "").replace(",", "").strip()
    if raw == "" or raw == "0":
        query("INSERT INTO app_settings (key, value) VALUES ('max_tickets', '') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        get_db().commit()
        flash("Ticket cap removed — sales are now unlimited.", "success")
        return redirect(url_for("report"))
    try:
        val = int(raw)
        if val < 0:
            raise ValueError
    except ValueError:
        flash("Maximum tickets must be a non-negative number (leave blank or 0 for no cap).", "error")
        return redirect(url_for("report"))
    query("INSERT INTO app_settings (key, value) VALUES ('max_tickets', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(val),))
    get_db().commit()
    flash("Maximum ticket cap updated.", "success")
    return redirect(url_for("report"))

@app.route("/admin/walkin_price", methods=["POST"])
@login_required(role="admin")
def update_walkin_price():
    updates = {}
    for label, slug in WALKIN_CATEGORY_SLUGS.items():
        raw = request.form.get(f"walkin_price_{slug}", "").replace(",", "").strip()
        if raw == "":
            continue
        try:
            price = int(raw)
            if price < 0:
                raise ValueError
        except ValueError:
            flash(f"Walk-in price for {label} must be a valid non-negative amount.", "error")
            return redirect(url_for("report"))
        updates[slug] = price
    if not updates:
        flash("No walk-in prices were submitted.", "error")
        return redirect(url_for("report"))
    for slug, price in updates.items():
        query("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (f"walkin_price_{slug}", str(price)))
    get_db().commit()
    flash("Walk-in table prices updated.", "success")
    return redirect(url_for("report"))

@app.route("/admin/reset_tickets", methods=["POST"])
@original_admin_required
def reset_tickets():
    confirmation = request.form.get("confirmation", "").strip().upper()
    if confirmation!= "RESET":
        flash("Ticket history was not cleared. Type RESET to confirm.", "error")
        return redirect(url_for("report"))
    query("DELETE FROM tickets")
    get_db().commit()
    flash("All ticket history has been cleared. Users and seller accounts were preserved.", "success")
    return redirect(url_for("report"))

@app.route("/report")
@login_required(role="admin")
def report():
    prices = get_ticket_prices()
    commission_rate = get_commission_rate()
    # "sold"/"cash"/"gross" all exclude cancelled tickets consistently, so
    # these numbers match the category breakdown on the All Tickets page.
    sales = query("""SELECT u.id, u.username, u.role, u.active,
                             COUNT(CASE WHEN t.cancelled = FALSE THEN 1 END) AS tickets_sold,
                             COALESCE(SUM(CASE WHEN t.cancelled = FALSE THEN t.amount_paid - COALESCE(t.refund_amount,0) ELSE 0 END),0) AS total_cash,
                             COALESCE(SUM(CASE WHEN t.used THEN 1 ELSE 0 END),0) AS tickets_used,
                             COALESCE(SUM(CASE WHEN t.cancelled = FALSE THEN t.amount_paid ELSE 0 END),0) AS total_gross
                      FROM users u LEFT JOIN tickets t ON u.id = t.sold_by
                      GROUP BY u.id ORDER BY u.role, u.active DESC, u.username""").fetchall()
    # Commission = commission_rate% of each seller's total amount_paid (gross, before refunds, excluding cancelled).
    for s in sales:
        s["total_commission"] = round(s["total_gross"] * commission_rate / 100)
    users = query("SELECT id, username, role, active FROM users ORDER BY role, active DESC, username").fetchall()
    overall = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE cancelled = FALSE""").fetchone()
    overall["cash"] = overall["cash"] + get_walkin_cash_total()  # general cash-in includes walk-in tables, without counting them as tickets sold
    walkin_prices = get_walkin_prices()
    max_tickets = get_max_tickets()
    return render_template("report.html", sales=sales, users=users, overall=overall, prices=prices, categories=CATEGORY_SLUGS, event_name=EVENT_NAME, user=g.user, commission_rate=commission_rate, walkin_prices=walkin_prices, walkin_categories=WALKIN_CATEGORY_SLUGS, max_tickets=max_tickets)

@app.route("/")
@login_required()
def home():
    user = g.user
    overall = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE cancelled = FALSE""").fetchone()
    overall["cash"] = overall["cash"] + get_walkin_cash_total()
    mine = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE cancelled = FALSE AND sold_by = %s""", (user["id"],)).fetchone()
    max_tickets = get_max_tickets()
    active_count = get_active_ticket_count()
    remaining_tickets = (max_tickets - active_count) if max_tickets is not None else None
    return render_template("home.html", overall=overall, mine=mine, event_name=EVENT_NAME, user=user, ticket_price=get_ticket_price(), max_tickets=max_tickets, remaining_tickets=remaining_tickets)

@app.route("/sell", methods=["GET", "POST"])
@login_required()
def sell():
    user = g.user
    prices = get_ticket_prices()
    max_tickets = get_max_tickets()
    if request.method == "POST":
        category = request.form.get("category", "Regular").strip()
        if category not in CATEGORY_SEATS:
            flash("Please select a valid ticket category.", "error")
            return redirect(url_for("sell"))
        seats = CATEGORY_SEATS[category]

        if max_tickets is not None:
            current_count = get_active_ticket_count()
            if current_count + seats > max_tickets:
                remaining = max(max_tickets - current_count, 0)
                if remaining <= 0:
                    flash("Maximum number of tickets for this event has been reached. No more tickets can be sold.", "error")
                else:
                    flash(f"Only {remaining} ticket slot(s) remain — {category} needs {seats}. Not enough slots left for this sale.", "error")
                return redirect(url_for("sell"))

        price = prices.get(category, DEFAULT_TICKET_PRICE)
        created = lagos_timestamp()
        db = get_db()

        if seats == 1:
            name = request.form.get("name", "").strip()
            whatsapp = request.form.get("whatsapp", "").strip()
            if not name or not whatsapp:
                flash("Guest name and WhatsApp number are required.", "error")
                return redirect(url_for("sell"))
            ticket_code = unique_ticket_code()
            row = query(
                "INSERT INTO tickets (ticket_code, name, whatsapp, seat, category, table_id, created_at, sold_by, amount_paid, qr_data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (ticket_code, name, whatsapp, "General", category, None, created, user["id"], price, ticket_code),
            ).fetchone()
            db.commit()
            return redirect(url_for("ticket_issued", ticket_id=row["id"]))

        # Table variation: one form submission, N guests, N independent tickets sharing one table_id
        guests = []
        for i in range(1, seats + 1):
            g_name = request.form.get(f"guest_name_{i}", "").strip()
            g_whatsapp = request.form.get(f"guest_whatsapp_{i}", "").strip()
            if not g_name or not g_whatsapp:
                flash(f"Please provide the name and WhatsApp number for guest {i} of {seats}.", "error")
                return redirect(url_for("sell"))
            guests.append((g_name, g_whatsapp))

        table_id = unique_table_id()
        table_number = next_table_number()
        first_id = None
        for i, (g_name, g_whatsapp) in enumerate(guests, start=1):
            ticket_code = unique_ticket_code()
            row = query(
                "INSERT INTO tickets (ticket_code, name, whatsapp, seat, category, table_id, table_number, created_at, sold_by, amount_paid, qr_data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (ticket_code, g_name, g_whatsapp, f"Guest {i} of {seats}", category, table_id, table_number, created, user["id"], price, ticket_code),
            ).fetchone()
            if first_id is None:
                first_id = row["id"]
        db.commit()
        return redirect(url_for("ticket_issued", ticket_id=first_id))

    overall = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE cancelled = FALSE""").fetchone()
    overall["cash"] = overall["cash"] + get_walkin_cash_total()
    mine = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE cancelled = FALSE AND sold_by = %s""", (user["id"],)).fetchone()
    recent = query("""SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC LIMIT 2""").fetchall()
    active_count = get_active_ticket_count()
    remaining_tickets = (max_tickets - active_count) if max_tickets is not None else None
    return render_template(
        "sell.html",
        overall=overall, mine=mine, recent=recent,
        prices=prices, categories=CATEGORY_SEATS,
        event_name=EVENT_NAME, user=user,
        max_tickets=max_tickets, remaining_tickets=remaining_tickets,
    )

@app.route("/tickets")
@login_required()
def all_tickets():
    q = request.args.get("q", "").strip()
    sql = """SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.name ILIKE %s OR t.whatsapp ILIKE %s OR COALESCE(t.ticket_code,'') ILIKE %s OR t.id::text ILIKE %s OR COALESCE(t.table_id,'') ILIKE %s ORDER BY t.id DESC"""
    like = f"%{q}%"
    tickets = query(sql, (like, like, like, like, like)).fetchall()

    # Category breakdown by seller + cumulative totals + tables to prepare.
    # Always reflects ALL tickets (independent of the search box above),
    # and excludes cancelled tickets so the venue plan matches tickets that
    # will actually be used.
    breakdown_rows = query(
        """SELECT COALESCE(u.username, 'Unknown / removed') AS username, t.category, COUNT(*) AS cnt
           FROM tickets t LEFT JOIN users u ON t.sold_by = u.id
           WHERE t.cancelled = FALSE
           GROUP BY COALESCE(u.username, 'Unknown / removed'), t.category"""
    ).fetchall()
    table_count_rows = query(
        """SELECT category, COUNT(DISTINCT table_id) AS table_count
           FROM tickets
           WHERE table_id IS NOT NULL AND cancelled = FALSE
           GROUP BY category"""
    ).fetchall()

    category_labels = list(CATEGORY_SEATS.keys())
    sellers = sorted({r["username"] for r in breakdown_rows})
    seller_breakdown = {s: {c: 0 for c in category_labels} for s in sellers}
    category_totals = {c: 0 for c in category_labels}
    for r in breakdown_rows:
        if r["category"] in category_totals:
            seller_breakdown[r["username"]][r["category"]] = r["cnt"]
            category_totals[r["category"]] += r["cnt"]
    seller_totals = {s: sum(seller_breakdown[s].values()) for s in sellers}
    grand_total = sum(category_totals.values())

    table_counts = {c: 0 for c in category_labels if CATEGORY_SEATS[c] > 1}
    for r in table_count_rows:
        if r["category"] in table_counts:
            table_counts[r["category"]] = r["table_count"]

    # Walk-in tables need physical seating too, even though they aren't
    # counted as "tickets sold" — fold them into the same table-prep counts.
    walkin_table_count_rows = query(
        "SELECT category, COUNT(*) AS table_count FROM walkin_tables GROUP BY category"
    ).fetchall()
    for r in walkin_table_count_rows:
        if r["category"] in table_counts:
            table_counts[r["category"]] += r["table_count"]

    return render_template(
        "tickets.html", tickets=tickets, q=q, event_name=EVENT_NAME, user=g.user,
        category_labels=category_labels, seller_breakdown=seller_breakdown,
        seller_totals=seller_totals, category_totals=category_totals,
        grand_total=grand_total, category_seats=CATEGORY_SEATS, table_counts=table_counts,
    )

@app.route("/ticket/<int:ticket_id>/pdf")
@login_required()
def ticket_pdf(ticket_id):
    ticket = get_ticket_or_404(ticket_id)
    buf = build_ticket_pdf(ticket)
    return Response(buf.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="owambe_{ticket["ticket_code"]}.pdf"'})

@app.route("/t/<path:ticket_code>/pdf")
@limiter.limit("30 per minute")
def public_ticket_pdf(ticket_code):
    ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (ticket_code,)).fetchone()
    if not ticket:
        abort(404)
    buf = build_ticket_pdf(ticket)
    return Response(buf.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="owambe_{ticket["ticket_code"]}.pdf"'})

@app.route("/ticket/<int:ticket_id>/whatsapp")
@login_required()
def send_ticket_whatsapp(ticket_id):
    ticket = get_ticket_or_404(ticket_id)
    db = get_db()
    query("UPDATE tickets SET send_count = send_count + 1, last_sent_at = %s WHERE id = %s", (lagos_timestamp(), ticket_id))
    log_audit(ticket_id, "whatsapp_sent", g.user["id"], f"To {ticket['whatsapp']}")
    db.commit()
    link = f"{request.host_url}t/{ticket['ticket_code']}/pdf"
    message = (
    f"Hi {ticket['name']}! 🎉\n\n"
    f"Your ticket for \"{EVENT_NAME}\" is ready.\n"
    f"Category: {ticket['category']}\n"
    f"Ticket code: {ticket['ticket_code']}\n"
    f"Download your ticket & QR here: {link}\n\n"
    f"⚠️ This link is yours alone — don't share it. Shared links get stolen and used by someone else at the gate.\n\n"
    f"Please present the QR code at the gate. See you there!"
    )
    dial = whatsapp_dial_number(ticket["whatsapp"])
    wa_url = f"https://wa.me/{dial}?text={urllib.parse.quote(message)}"
    return redirect(wa_url)

@app.route("/ticket/<int:ticket_id>/cancel", methods=["POST"])
@login_required()
def cancel_ticket(ticket_id):
    ticket = get_ticket_or_404(ticket_id)
    if ticket["cancelled"]:
        flash("This ticket is already cancelled.", "error")
        return redirect(url_for("ticket", ticket_id=ticket_id))
    if ticket["used"]:
        flash("Cannot cancel. Ticket was already used at the door.", "error")
        return redirect(url_for("ticket", ticket_id=ticket_id))
    reason = f"Cancelled by {g.user['username']} from ticket page"
    now = lagos_timestamp()
    db = get_db()
    query("UPDATE tickets SET cancelled = TRUE, cancelled_at = %s, cancelled_by = %s, cancel_reason = %s WHERE id = %s", (now, g.user["id"], reason, ticket_id))
    log_audit(ticket_id, "cancelled", g.user["id"], reason)
    db.commit()
    flash(f"Ticket {ticket['ticket_code']} was cancelled.", "success")
    return redirect(url_for("ticket", ticket_id=ticket_id))

@app.route("/scan")
@login_required()
def scan():
    return render_template("scan.html", event_name=EVENT_NAME, user=g.user)

@app.route("/qr/<path:ticket_code>.png")
@login_required()
def qr_image(ticket_code):
    ticket = query("SELECT ticket_code, qr_data FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (ticket_code,)).fetchone()
    if not ticket:
        return "Ticket not found", 404
    payload = ticket["qr_data"] or ticket["ticket_code"]
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png", headers={"Content-Disposition": f'inline; filename="owambe_{ticket["ticket_code"]}.png"'})

@app.route("/ticket_issued/<int:ticket_id>")
@login_required()
def ticket_issued(ticket_id):
    ticket = get_ticket_or_404(ticket_id)
    if ticket["table_id"]:
        tickets = query(
            "SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.table_id = %s ORDER BY t.id",
            (ticket["table_id"],),
        ).fetchall()
    else:
        tickets = [ticket]
    return render_template("ticket_issued.html", ticket=ticket, tickets=tickets, event_name=EVENT_NAME, user=g.user)

@app.route("/ticket/<int:ticket_id>")
@login_required()
def ticket(ticket_id):
    ticket = get_ticket_or_404(ticket_id)
    qr_data = f"{request.url_root}t/{ticket['ticket_code']}/pdf"
    qr = qrcode.make(qr_data)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode()
    return render_template("ticket.html", ticket=ticket, qr_base64=qr_base64, event_name=EVENT_NAME, user=g.user)

def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits

def _find_ticket_or_matches(raw):
    """Look up a ticket by ticket code, numeric id, or WhatsApp number.
    Returns (ticket, matches) where exactly one of the two is set:
    ticket is a single row, or matches is a list of 2+ rows sharing a phone number."""
    lookup = raw
    if "http://" in lookup or "https://" in lookup:
        lookup = lookup.rstrip("/").rsplit("/", 1)[-1]
    ticket = None
    if lookup:
        ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (lookup,)).fetchone()
    if not ticket and lookup.isdigit():
        ticket = query("SELECT * FROM tickets WHERE id = %s", (int(lookup),)).fetchone()
    if not ticket:
        target = normalize_phone(raw)
        if len(target) >= 7:
            all_rows = query("SELECT * FROM tickets").fetchall()
            found = [t for t in all_rows if normalize_phone(t["whatsapp"]) == target]
            if len(found) == 1:
                ticket = found[0]
            elif len(found) > 1:
                return None, found
    return ticket, None

def _ticket_status_payload(ticket):
    """Read-only status for a specific ticket — never mutates it."""
    if ticket["cancelled"]:
        reason = f" ({ticket['cancel_reason']})" if ticket["cancel_reason"] else ""
        return {"status": "CANCELLED", "msg": f"This ticket was cancelled{reason}. Entry denied.", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]}
    if ticket["used"]:
        return {"status": "ALREADY_USED", "msg": f"Already scanned at {ticket['used_at']} (Lagos time)", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"], "time": ticket["used_at"]}
    return {
        "status": "FOUND",
        "msg": "Ticket found. Review the details, then confirm check-in.",
        "name": ticket["name"],
        "whatsapp": ticket["whatsapp"],
        "ticket_code": ticket["ticket_code"],
        "category": ticket.get("category") or "Regular",
        "table_number": ticket.get("table_number"),
        "seat": ticket.get("seat"),
        "amount_paid": ticket["amount_paid"],
    }

@app.route("/api/data_version")
@login_required()
def api_data_version():
    """Lightweight signature the client polls to know when to auto-refresh.
    Changes whenever a ticket is sold/used/cancelled/refunded, a seller/admin
    is added or (de)activated, or a price/commission setting changes."""
    t = query(
        """SELECT COUNT(*) AS cnt, COALESCE(MAX(id),0) AS max_id,
                  COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used_cnt,
                  COALESCE(SUM(CASE WHEN cancelled THEN 1 ELSE 0 END),0) AS cancelled_cnt,
                  COALESCE(SUM(CASE WHEN refunded THEN 1 ELSE 0 END),0) AS refunded_cnt,
                  COALESCE(SUM(amount_paid),0) AS amount_sum,
                  COUNT(DISTINCT table_id) AS table_cnt
           FROM tickets"""
    ).fetchone()
    w = query("SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_paid),0) AS total FROM walkin_tables").fetchone()
    users_row = query(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(CASE WHEN active THEN 1 ELSE 0 END),0) AS active_cnt FROM users"
    ).fetchone()
    settings_rows = query("SELECT key, value FROM app_settings ORDER BY key").fetchall()
    settings_sig = "|".join(f"{r['key']}={r['value']}" for r in settings_rows)
    version = f"{t['cnt']}-{t['max_id']}-{t['used_cnt']}-{t['cancelled_cnt']}-{t['refunded_cnt']}-{t['amount_sum']}-{t['table_cnt']}-{w['cnt']}-{w['total']}-{users_row['cnt']}-{users_row['active_cnt']}-{settings_sig}"
    return {"version": version}

@app.route("/api/lookup_ticket", methods=["POST"])
@login_required()
@limiter.limit("60 per minute")
def api_lookup_ticket():
    """Step 1 of check-in: find the ticket and return its details. Never marks it used."""
    raw = request.form.get("data", "").strip()
    ticket, matches = _find_ticket_or_matches(raw)
    if matches:
        return {"status": "MULTIPLE", "msg": f"{len(matches)} tickets are registered to this number — pick the guest.", "matches": [{"id": t["id"], "ticket_code": t["ticket_code"], "name": t["name"], "used": bool(t["used"])} for t in matches]}
    if not ticket:
        return {"status": "INVALID", "msg": f"No ticket found for \"{raw}\""}
    return _ticket_status_payload(ticket)

@app.route("/api/confirm_checkin", methods=["POST"])
@login_required()
@limiter.limit("60 per minute")
def api_confirm_checkin():
    """Step 2 of check-in: the handler has reviewed the details and explicitly
    confirmed. Only this route marks a ticket used."""
    ticket_code = request.form.get("ticket_code", "").strip()
    if not ticket_code:
        return {"status": "INVALID", "msg": "No ticket code supplied."}
    ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (ticket_code,)).fetchone()
    if not ticket:
        return {"status": "INVALID", "msg": f"No ticket found for \"{ticket_code}\""}
    if ticket["cancelled"]:
        reason = f" ({ticket['cancel_reason']})" if ticket["cancel_reason"] else ""
        return {"status": "CANCELLED", "msg": f"This ticket was cancelled{reason}. Entry denied.", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]}
    if ticket["used"]:
        return {"status": "ALREADY_USED", "msg": f"Already scanned at {ticket['used_at']} (Lagos time)", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"], "time": ticket["used_at"]}
    used_time = lagos_timestamp()
    updated = query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s AND used = FALSE RETURNING id", (used_time, ticket["id"])).fetchone()
    if not updated:
        # Someone else confirmed this exact ticket in the moment between lookup and confirm.
        get_db().rollback()
        return {"status": "ALREADY_USED", "msg": "This ticket was just checked in by someone else.", "name": ticket["name"], "ticket_code": ticket["ticket_code"]}
    get_db().commit()
    log_audit(ticket["id"], "checked_in", g.user["id"], "Confirmed at door")
    return {
        "status": "CHECKED_IN", "msg": "Entry approved.",
        "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"],
        "category": ticket.get("category") or "Regular", "table_number": ticket.get("table_number"), "seat": ticket.get("seat"),
    }

@app.route("/check_ticket", methods=["POST"])
@login_required()
@limiter.limit("60 per minute")
def check_ticket():
    """Deprecated: kept only for backward compatibility with old clients/bookmarks.
    No longer auto-marks tickets used — mirrors /api/lookup_ticket. Use the
    /api/lookup_ticket + /api/confirm_checkin flow for anything new."""
    raw = request.form.get("data", "").strip()
    ticket, matches = _find_ticket_or_matches(raw)
    if matches:
        return {"status": "MULTIPLE", "msg": f"{len(matches)} tickets are registered to this number — pick the guest.", "matches": [{"id": t["id"], "ticket_code": t["ticket_code"], "name": t["name"], "used": bool(t["used"])} for t in matches]}
    if not ticket:
        return {"status": "INVALID", "msg": f"No ticket found for \"{raw}\""}
    return _ticket_status_payload(ticket)

@app.route("/api/verify/<path:ticket_code>")
@login_required()
def verify(ticket_code):
    """Read-only status check. No longer auto-marks used — call
    /api/confirm_checkin to actually check a guest in."""
    ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (ticket_code,)).fetchone()
    if not ticket:
        return {"status": "invalid"}
    if ticket["cancelled"]:
        return {"status": "cancelled", "name": ticket["name"]}
    if ticket["used"]:
        return {"status": "already_used", "name": ticket["name"], "time": ticket["used_at"]}
    return {"status": "found", "name": ticket["name"], "ticket_code": ticket["ticket_code"]}

@app.route("/tables/create", methods=["GET", "POST"])
@login_required(role="admin")
def create_table():
    """Admin-only: record a walk-in table for guests already at the event.
    No tickets or QR codes are generated — just the record itself."""
    walkin_prices = get_walkin_prices()
    if request.method == "POST":
        category = request.form.get("category", "").strip()
        if category not in WALKIN_CATEGORY_SLUGS:
            flash("Please select a valid table category.", "error")
            return redirect(url_for("create_table"))
        seats = CATEGORY_SEATS[category]
        names = []
        for i in range(1, seats + 1):
            name = request.form.get(f"guest_name_{i}", "").strip()
            if not name:
                flash(f"Please provide the name for guest {i} of {seats}.", "error")
                return redirect(url_for("create_table"))
            names.append(name)
        raw_amount = request.form.get("amount_paid", "").replace(",", "").strip()
        try:
            amount = int(raw_amount)
            if amount < 0:
                raise ValueError
        except ValueError:
            flash("Amount paid must be a valid non-negative number.", "error")
            return redirect(url_for("create_table"))
        table_ref = unique_table_id()
        table_number = next_table_number()
        query(
            "INSERT INTO walkin_tables (table_ref, category, guest_names, amount_paid, created_by, created_at, table_number) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (table_ref, category, "\n".join(names), amount, g.user["id"], lagos_timestamp(), table_number),
        )
        get_db().commit()
        flash(f"Walk-in Table {table_number} recorded for {category} — ₦{amount:,}.", "success")
        return redirect(url_for("tables"))
    return render_template(
        "create_table.html", event_name=EVENT_NAME, user=g.user,
        categories=WALKIN_CATEGORY_SLUGS, category_seats=CATEGORY_SEATS, walkin_prices=walkin_prices,
    )

@app.route("/tables")
@login_required()
def tables():
    """Overview of every table — whether from a pre-sold ticket purchase,
    a ticket upgrade, or an admin-created walk-in — with its guest list."""
    ticket_rows = query(
        """SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id
           WHERE t.table_id IS NOT NULL AND t.cancelled = FALSE
           ORDER BY t.table_id, t.id"""
    ).fetchall()
    ticket_tables = {}
    for t in ticket_rows:
        tid = t["table_id"]
        ticket_tables.setdefault(tid, {
            "table_ref": tid, "table_number": t["table_number"], "category": t["category"], "source": "ticket",
            "guests": [], "amount_total": 0, "sold_by": t["username"], "created_at": t["created_at"],
        })
        ticket_tables[tid]["guests"].append({
            "name": t["name"], "whatsapp": t["whatsapp"], "ticket_code": t["ticket_code"], "used": t["used"],
        })
        ticket_tables[tid]["amount_total"] += t["amount_paid"]

    walkin_rows = query(
        "SELECT w.*, u.username FROM walkin_tables w LEFT JOIN users u ON w.created_by = u.id ORDER BY w.id DESC"
    ).fetchall()
    walkin_list = [{
        "table_ref": w["table_ref"], "table_number": w["table_number"], "category": w["category"], "source": "walkin",
        "guests": [{"name": n} for n in (w["guest_names"] or "").split("\n") if n],
        "amount_total": w["amount_paid"], "sold_by": w["username"], "created_at": w["created_at"],
    } for w in walkin_rows]

    all_tables = list(ticket_tables.values()) + walkin_list
    all_tables.sort(key=lambda x: x["table_number"] or 0)
    return render_template("tables.html", event_name=EVENT_NAME, user=g.user, all_tables=all_tables)

@app.route("/upgrade", methods=["GET", "POST"])
@login_required()
def upgrade_ticket():
    """Merge 2+ existing Regular tickets into a table. The tickets keep their
    ticket codes but get a shared table_id, their category flips to the table
    category, and their recorded amount_paid updates so the total matches the
    table price — the difference is the balance the handler collects."""
    if request.method == "POST":
        target_category = request.form.get("category", "").strip()
        if target_category not in WALKIN_CATEGORY_SLUGS:
            flash("Please select a valid table category to upgrade to.", "error")
            return redirect(url_for("upgrade_ticket"))
        seats = CATEGORY_SEATS[target_category]
        selected_ids = request.form.getlist("ticket_ids")
        if len(selected_ids) != seats:
            flash(f"{target_category} needs exactly {seats} tickets selected — you selected {len(selected_ids)}.", "error")
            return redirect(url_for("upgrade_ticket"))
        try:
            ids = [int(i) for i in selected_ids]
        except ValueError:
            flash("Invalid ticket selection.", "error")
            return redirect(url_for("upgrade_ticket"))

        tickets_to_upgrade = []
        for tid in ids:
            t = query("SELECT * FROM tickets WHERE id = %s", (tid,)).fetchone()
            if not t or t["cancelled"] or t["table_id"]:
                flash("One of the selected tickets is no longer eligible (cancelled or already on a table).", "error")
                return redirect(url_for("upgrade_ticket"))
            tickets_to_upgrade.append(t)

        prices = get_ticket_prices()
        target_total = prices.get(target_category, DEFAULT_TICKET_PRICE) * seats
        already_paid = sum(t["amount_paid"] for t in tickets_to_upgrade)
        balance_due = max(target_total - already_paid, 0)

        # Split the table's total price evenly across the seats, putting any
        # rounding remainder on the first ticket so the sum stays exact.
        per_head_base = target_total // seats
        remainder = target_total - per_head_base * seats

        new_table_id = unique_table_id()
        new_table_number = next_table_number()
        for i, t in enumerate(tickets_to_upgrade, start=1):
            per_head_price = per_head_base + (remainder if i == 1 else 0)
            query(
                "UPDATE tickets SET category = %s, table_id = %s, table_number = %s, seat = %s, amount_paid = %s WHERE id = %s",
                (target_category, new_table_id, new_table_number, f"Guest {i} of {seats}", per_head_price, t["id"]),
            )
            log_audit(
                t["id"], "upgraded", g.user["id"],
                f"Upgraded from {t['category']} (₦{t['amount_paid']:,}) to {target_category} (₦{per_head_price:,}); "
                f"Table {new_table_number}; total balance collected for group: ₦{balance_due:,}",
            )
        get_db().commit()
        flash(f"Upgraded {seats} tickets to {target_category} (Table {new_table_number}). Balance collected: ₦{balance_due:,}.", "success")
        return redirect(url_for("tables"))

    q = request.args.get("q", "").strip()
    candidates = []
    if q:
        like = f"%{q}%"
        candidates = query(
            """SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id
               WHERE t.table_id IS NULL AND t.cancelled = FALSE
               AND (t.name ILIKE %s OR t.whatsapp ILIKE %s OR COALESCE(t.ticket_code,'') ILIKE %s)
               ORDER BY t.id DESC""",
            (like, like, like),
        ).fetchall()
    prices = get_ticket_prices()
    return render_template(
        "upgrade.html", event_name=EVENT_NAME, user=g.user, q=q, candidates=candidates,
        categories=WALKIN_CATEGORY_SLUGS, category_seats=CATEGORY_SEATS, prices=prices,
    )

@app.route("/export")
@login_required(role="admin")
def export_csv():
    tickets = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC").fetchall()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["Ticket Code", "Name", "WhatsApp", "Seat", "Category", "Table ID", "Sold By", "Amount Paid", "Created At", "Used", "Used At", "Cancelled", "Cancel Reason", "Refunded", "Refund Amount"])
    for t in tickets:
        cw.writerow([t["ticket_code"], t["name"], t["whatsapp"], t.get("seat","General"), t.get("category","Regular"), t.get("table_id") or "", t["username"], t["amount_paid"], t["created_at"], t["used"], t["used_at"], t["cancelled"], t["cancel_reason"] or "", t["refunded"], t["refund_amount"] or ""])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=owambe_tickets.csv"})

if __name__ == "__main__":
    app.run(debug=True)
