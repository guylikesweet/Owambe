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
LAGOS_TZ = ZoneInfo("Africa/Lagos")
TICKET_ALPHABET = string.ascii_uppercase + string.digits

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
    query("""CREATE TABLE IF NOT EXISTS tickets (id SERIAL PRIMARY KEY, ticket_code TEXT UNIQUE, name TEXT NOT NULL, whatsapp TEXT NOT NULL, seat TEXT DEFAULT 'General', used BOOLEAN DEFAULT FALSE, created_at TEXT, used_at TEXT, sold_by INTEGER REFERENCES users (id), amount_paid INTEGER NOT NULL DEFAULT 3500, qr_data TEXT)""")
    query("""CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    query("""CREATE TABLE IF NOT EXISTS ticket_audit (id SERIAL PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets (id), action TEXT NOT NULL, performed_by INTEGER REFERENCES users (id), detail TEXT, created_at TEXT)""")
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
    missing = query("SELECT id FROM tickets WHERE ticket_code IS NULL OR ticket_code = ''").fetchall()
    for row in missing:
        code = generate_ticket_code()
        while query("SELECT 1 FROM tickets WHERE ticket_code = %s", (code,)).fetchone():
            code = generate_ticket_code()
        query("UPDATE tickets SET ticket_code = %s WHERE id = %s", (code, row["id"]))
    query("UPDATE tickets SET qr_data = ticket_code WHERE qr_data IS NULL OR qr_data = ''")
    query("INSERT INTO app_settings (key, value) VALUES ('ticket_price', %s) ON CONFLICT (key) DO NOTHING", (str(DEFAULT_TICKET_PRICE),))
    db.commit()
    user = query("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if not user:
        query("INSERT INTO users (username, password_hash, role, active) VALUES (%s,%s,%s,%s)", ("admin", generate_password_hash("admin123"), "admin", True))
        db.commit()
        print("Default admin created: admin / admin123")

def get_ticket_price():
    row = query("SELECT value FROM app_settings WHERE key = 'ticket_price'").fetchone()
    try:
        return int(row["value"]) if row else DEFAULT_TICKET_PRICE
    except (TypeError, ValueError):
        return DEFAULT_TICKET_PRICE

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

    y = details_top - 5.5 * mm
    y = field(y, "GUEST NAME", ticket["name"])
    y = field(y, "TICKET CODE", ticket["ticket_code"])
    y = field(y, "WHATSAPP", ticket["whatsapp"])
    y = field(y, "SEAT", ticket.get("seat", "General"))
    y = field(y, "AMOUNT PAID", f"NGN {ticket['amount_paid']:,}")

    # 7. FOOTER - MOVED DOWN 6MM
    footer_divider_y = card_y + 37 * mm # was 31 * mm

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
    price = get_ticket_price()
    sales = query("""SELECT u.id, u.username, u.role, u.active, COUNT(t.id) AS tickets_sold, COALESCE(SUM(t.amount_paid - COALESCE(t.refund_amount,0)),0) AS total_cash, COALESCE(SUM(CASE WHEN t.used THEN 1 ELSE 0 END),0) AS tickets_used FROM users u LEFT JOIN tickets t ON u.id = t.sold_by GROUP BY u.id ORDER BY u.role, u.active DESC, u.username""").fetchall()
    users = query("SELECT id, username, role, active FROM users ORDER BY role, active DESC, username").fetchall()
    overall = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets""").fetchone()
    return render_template("report.html", sales=sales, users=users, overall=overall, ticket_price=price, event_name=EVENT_NAME, user=g.user)

@app.route("/")
@login_required()
def home():
    user = g.user
    overall = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets""").fetchone()
    mine = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE sold_by = %s""", (user["id"],)).fetchone()
    return render_template("home.html", overall=overall, mine=mine, event_name=EVENT_NAME, user=user, ticket_price=get_ticket_price())

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
        row = query("INSERT INTO tickets (ticket_code, name, whatsapp, seat, created_at, sold_by, amount_paid, qr_data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", (ticket_code, name, whatsapp, "General", created, user["id"], price, ticket_code)).fetchone()
        get_db().commit()
        return redirect(url_for("ticket_issued", ticket_id=row["id"]))
    overall = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets""").fetchone()
    mine = query("""SELECT COUNT(*) AS sold, COALESCE(SUM(CASE WHEN used THEN 1 ELSE 0 END),0) AS used, COALESCE(SUM(amount_paid - COALESCE(refund_amount,0)),0) AS cash FROM tickets WHERE sold_by = %s""", (user["id"],)).fetchone()
    recent = query("""SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC LIMIT 2""").fetchall()
    return render_template("sell.html", overall=overall, mine=mine, recent=recent, price=price, event_name=EVENT_NAME, user=user)

@app.route("/tickets")
@login_required()
def all_tickets():
    q = request.args.get("q", "").strip()
    sql = """SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id WHERE t.name ILIKE %s OR t.whatsapp ILIKE %s OR COALESCE(t.ticket_code,'') ILIKE %s OR t.id::text ILIKE %s ORDER BY t.id DESC"""
    like = f"%{q}%"
    tickets = query(sql, (like, like, like, like)).fetchall()
    return render_template("tickets.html", tickets=tickets, q=q, event_name=EVENT_NAME, user=g.user)

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
    message = f"Hi {ticket['name']}! 🎉\n\nYour ticket for \"{EVENT_NAME}\" is ready.\nTicket code: {ticket['ticket_code']}\nDownload your ticket & QR here: {link}\n\nPlease present the QR code at the gate. See you there!"
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
    return render_template("ticket_issued.html", ticket=ticket, event_name=EVENT_NAME, user=g.user)

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

@app.route("/check_ticket", methods=["POST"])
@login_required()
@limiter.limit("60 per minute")
def check_ticket():
    raw = request.form.get("data", "").strip()
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
            matches = [t for t in all_rows if normalize_phone(t["whatsapp"]) == target]
            if len(matches) == 1:
                ticket = matches[0]
            elif len(matches) > 1:
                return {"status": "MULTIPLE", "msg": f"{len(matches)} tickets are registered to this number — pick the guest.", "matches": [{"id": t["id"], "ticket_code": t["ticket_code"], "name": t["name"], "used": bool(t["used"])} for t in matches]}
    if not ticket:
        return {"status": "INVALID", "msg": f"No ticket found for \"{raw}\""}
    if ticket["cancelled"]:
        reason = f" ({ticket['cancel_reason']})" if ticket["cancel_reason"] else ""
        return {"status": "CANCELLED", "msg": f"This ticket was cancelled{reason}. Entry denied.", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]}
    if ticket["used"]:
        return {"status": "ALREADY USED", "msg": f"Already scanned at {ticket['used_at']} (Lagos time)", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]}
    used_time = lagos_timestamp()
    updated = query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s AND used = FALSE RETURNING id", (used_time, ticket["id"])).fetchone()
    if not updated:
        get_db().rollback()
        return {"status": "ALREADY USED", "msg": "This ticket has already been used."}
    get_db().commit()
    return {"status": "VALID", "msg": "Entry Approved", "name": ticket["name"], "whatsapp": ticket["whatsapp"], "ticket_code": ticket["ticket_code"]}

@app.route("/api/verify/<path:ticket_code>")
@login_required()
def verify(ticket_code):
    db = get_db()
    ticket = query("SELECT * FROM tickets WHERE UPPER(ticket_code) = UPPER(%s)", (ticket_code,)).fetchone()
    if not ticket:
        return {"status": "invalid"}
    if ticket["cancelled"]:
        return {"status": "cancelled", "name": ticket["name"]}
    if ticket["used"]:
        return {"status": "already_used", "name": ticket["name"], "time": ticket["used_at"]}
    used_time = lagos_timestamp()
    updated = query("UPDATE tickets SET used = TRUE, used_at = %s WHERE id = %s AND used = FALSE RETURNING id", (used_time, ticket["id"])).fetchone()
    if not updated:
        db.rollback()
        return {"status": "already_used", "name": ticket["name"], "time": ticket["used_at"]}
    db.commit()
    return {"status": "ok", "name": ticket["name"], "ticket_code": ticket["ticket_code"]}

@app.route("/export")
@login_required(role="admin")
def export_csv():
    tickets = query("SELECT t.*, u.username FROM tickets t LEFT JOIN users u ON t.sold_by = u.id ORDER BY t.id DESC").fetchall()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["Ticket Code", "Name", "WhatsApp", "Seat", "Sold By", "Amount Paid", "Created At", "Used", "Used At", "Cancelled", "Cancel Reason", "Refunded", "Refund Amount"])
    for t in tickets:
        cw.writerow([t["ticket_code"], t["name"], t["whatsapp"], t.get("seat","General"), t["username"], t["amount_paid"], t["created_at"], t["used"], t["used_at"], t["cancelled"], t["cancel_reason"] or "", t["refunded"], t["refund_amount"] or ""])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=owambe_tickets.csv"})

if __name__ == "__main__":
    app.run(debug=True)
