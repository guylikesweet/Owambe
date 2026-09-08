from flask import Flask, render_template, request, redirect, url_for, session, g, flash, Response
import sqlite3, os, io, csv, secrets, string # CHANGED: added secrets, string
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
    db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_code TEXT UNIQUE, # ADDED: this line only
            name TEXT NOT NULL,
            whatsapp TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT,
            used_at TEXT,
            sold_by INTEGER,
            FOREIGN KEY (sold_by) REFERENCES users (id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'seller'
        )
    """)
    user = db.execute("SELECT * FROM users").fetchone()
    if not user:
        db.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                   ("admin", generate_password_hash("admin123"), "admin"))
        print("Default admin created: admin / admin123")
    db.commit()

with app.app_context():
    init_db()

def generate_ticket_code(): # ADDED: this whole function
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    code2 = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"BE-{code}-{code2}"

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
