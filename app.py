from flask import Flask, render_template, request, redirect, url_for, session, g, flash, Response
import sqlite3, os, io, csv
from datetime import datetime
import qrcode
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-render")

DATABASE = "tickets.db"
EVENT_NAME = "BE Owambe"
TICKET_PRICE = 3500

# ------------------ DB HELPERS ------------------
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    # Tickets table with sold_by
    db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            whatsapp TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT,
            used_at TEXT,
            sold_by INTEGER,
            FOREIGN KEY (sold_by) REFERENCES users (id)
        )
    """)
    # Users table
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'seller'
        )
    """)
    # Create default admin if no users exist
    user = db.execute("SELECT * FROM users").fetchone()
    if not user:
        db.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                   ("admin", generate_password_hash("admin123"), "admin"))
        print("Default admin created: admin / admin123")
    db.commit()

# Run DB init on startup for Flask 3.x
with app.app_context():
    init_db()

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id =?", (session["user_id"],)).fetchone()

def login_required(role=None):
    def decorator(f):
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if role and user["role"]!= role:
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
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username =?", (username,)).fetchone()

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
            db.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                       (username, generate_password_hash(password), role))
            db.commit()
            flash(f"User {username} created successfully", "success")
            return redirect(url_for("report"))
        except sqlite3.IntegrityError:
            flash("Username already exists", "error")
    return render_template("register.html", event_name=EVENT_NAME, user=g.user)

@app.route("/report")
@login_required(role="admin")
def report():
    db = get_db()
    sales = db.execute("""
        SELECT u.username, u.role, COUNT(t.id) as tickets_sold,
               COALESCE(SUM(?), 0) as total_cash
        FROM users u
        LEFT JOIN tickets t ON u.id = t.sold_by
        GROUP BY u.id
        ORDER BY total_cash DESC
    """, (TICKET_PRICE,)).fetchall()

    users = db.execute("SELECT * FROM users ORDER BY role, username").fetchall()
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
        db.execute("INSERT INTO tickets (name, whatsapp, created_at, sold_by) VALUES (?,?,?,?)",
                   (name, whatsapp, created, user["id"])) # FIXED: 4 question marks
        db.commit()
        flash(f"Ticket issued for {name}", "success")
        return redirect(url_for("sell"))

    # Stats: different for admin vs seller
    if user["role"] == "admin":
        stats = db.execute("SELECT COUNT(*), COALESCE(SUM(?),0), SUM(used) FROM tickets", (TICKET_PRICE,)).fetchone()
        recent = db.execute("""
            SELECT t.*, u.username FROM tickets t
            LEFT JOIN users u ON t.sold_by = u.id
            ORDER BY t.id DESC LIMIT 10
        """).fetchall()
    else:
        stats = db.execute("SELECT COUNT(*), COALESCE(SUM(?),0), SUM(used) FROM tickets WHERE sold_by =?",
                           (TICKET_PRICE, user["id"])).fetchone()
        recent = db.execute("""
            SELECT t.*, u.username FROM tickets t
            LEFT JOIN users u ON t.sold_by = u.id
            WHERE t.sold_by =?
            ORDER BY t.id DESC LIMIT 10
        """, (user["id"],)).fetchall()

    return render_template("sell.html",
        total_sold=stats[0] or 0, total_cash=stats[1] or 0, total_used=stats[2] or 0,
        recent=recent, price=TICKET_PRICE, event_name=EVENT_NAME, user=user)

@app.route("/tickets")
@login_required()
def all_tickets():
    db = get_db()
    user = g.user
    q = request.args.get("q", "")

    if user["role"] == "admin":
        sql = """
            SELECT t.*, u.username FROM tickets t
            LEFT JOIN users u ON t.sold_by = u.id
            WHERE t.name LIKE? OR t.whatsapp LIKE? OR t.id LIKE?
            ORDER BY t.id DESC
        """
        tickets = db.execute(sql, (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    else:
        sql = """
            SELECT t.*, u.username FROM tickets t
            LEFT JOIN users u ON t.sold_by = u.id
            WHERE t.sold_by =? AND (t.name LIKE? OR t.whatsapp LIKE? OR t.id LIKE?)
            ORDER BY t.id DESC
        """
        tickets = db.execute(sql, (user["id"], f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()

    return render_template("tickets.html", tickets=tickets, q=q, event_name=EVENT_NAME, user=user)

@app.route("/scan")
@login_required()
def scan():
    return render_template("scan.html", event_name=EVENT_NAME, user=g.user)

@app.route("/api/verify/<int:ticket_id>")
@login_required()
def verify(ticket_id):
    db = get_db()
    ticket = db.execute("SELECT * FROM tickets WHERE id =?", (ticket_id,)).fetchone()
    if not ticket:
        return {"status": "invalid"}
    if ticket["used"]:
        return {"status": "already_used", "name": ticket["name"], "time": ticket["used_at"]}
    used_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE tickets SET used = 1, used_at =? WHERE id =?", (used_time, ticket_id))
    db.commit()
    return {"status": "ok", "name": ticket["name"]}

@app.route("/ticket/<int:ticket_id>")
@login_required()
def ticket(ticket_id):
    db = get_db()
    ticket = db.execute("SELECT * FROM tickets WHERE id =?", (ticket_id,)).fetchone()
    if not ticket:
        return "Ticket not found", 404
    qr = qrcode.make(f"{request.host_url}api/verify/{ticket_id}") # FIXED: uses your domain automatically
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")

@app.route("/export")
@login_required(role="admin")
def export_csv():
    db = get_db()
    tickets = db.execute("""
        SELECT t.*, u.username FROM tickets t
        LEFT JOIN users u ON t.sold_by = u.id
        ORDER BY t.id DESC
    """).fetchall()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["ID", "Name", "WhatsApp", "Sold By", "Created At", "Used", "Used At"])
    for t in tickets:
        cw.writerow([t["id"], t["name"], t["whatsapp"], t["username"], t["created_at"], t["used"], t["used_at"]])
    output = si.getvalue()
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=tickets.csv"})

if __name__ == "__main__":
    app.run(debug=True)
