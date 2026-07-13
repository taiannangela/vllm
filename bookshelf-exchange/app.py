"""Bookshelf Exchange - a small book sharing site for a friends group.

Members photograph their bookshelves, catalog the books on them, search
each other's collections, and borrow/return books through the site.
"""

import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "bookshelf.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(24).hex())
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB photo uploads

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    name       TEXT NOT NULL,
    photo      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    shelf_id   INTEGER REFERENCES shelves(id),
    title      TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'available',  -- available | borrowed
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id        INTEGER NOT NULL REFERENCES books(id),
    borrower_id    INTEGER NOT NULL REFERENCES users(id),
    status         TEXT NOT NULL DEFAULT 'requested',
    -- requested | checked_out | returned | declined | cancelled
    requested_at   TEXT NOT NULL,
    checked_out_at TEXT,
    returned_at    TEXT
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with sqlite3.connect(DATABASE) as db:
        db.executescript(SCHEMA)


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user_id") is None:
            flash("Please log in first.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_globals():
    user = current_user()
    pending = 0
    if user:
        pending = (
            get_db()
            .execute(
                """SELECT COUNT(*) FROM loans
                   JOIN books ON books.id = loans.book_id
                   WHERE books.owner_id = ? AND loans.status = 'requested'""",
                (user["id"],),
            )
            .fetchone()[0]
        )
    return {"me": user, "pending_requests": pending}


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        display_name = request.form["display_name"].strip() or username
        password = request.form["password"]
        if not username or not username.isalnum():
            flash("Username must be letters and numbers only.")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.")
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, display_name, password_hash,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (
                        username,
                        display_name,
                        generate_password_hash(password),
                        now(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("That username is taken.")
            else:
                user = db.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                session["user_id"] = user["id"]
                flash("Welcome! Add your first shelf to get started.")
                return redirect(url_for("new_shelf"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = (
            get_db()
            .execute(
                "SELECT * FROM users WHERE username = ?",
                (request.form["username"].strip(),),
            )
            .fetchone()
        )
        if user and check_password_hash(
            user["password_hash"], request.form["password"]
        ):
            session["user_id"] = user["id"]
            target = request.args.get("next")
            if target and target.startswith("/") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("index"))
        flash("Wrong username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Shelves and books
# --------------------------------------------------------------------------


def allowed_photo(filename):
    return "." in filename and (
        filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


@app.route("/")
def index():
    db = get_db()
    shelves = db.execute(
        """SELECT shelves.*, users.username, users.display_name,
                  (SELECT COUNT(*) FROM books WHERE books.shelf_id = shelves.id)
                  AS book_count
           FROM shelves JOIN users ON users.id = shelves.user_id
           ORDER BY shelves.created_at DESC LIMIT 12"""
    ).fetchall()
    books = db.execute(
        """SELECT books.*, users.username, users.display_name
           FROM books JOIN users ON users.id = books.owner_id
           ORDER BY books.created_at DESC LIMIT 10"""
    ).fetchall()
    members = db.execute(
        "SELECT username, display_name FROM users ORDER BY created_at"
    ).fetchall()
    return render_template(
        "index.html", shelves=shelves, books=books, members=members
    )


@app.route("/shelves/new", methods=["GET", "POST"])
@login_required
def new_shelf():
    if request.method == "POST":
        name = request.form["name"].strip() or "My shelf"
        photo_name = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            if not allowed_photo(photo.filename):
                flash("Photo must be a png, jpg, gif, or webp image.")
                return render_template("new_shelf.html")
            ext = photo.filename.rsplit(".", 1)[1].lower()
            photo_name = f"{uuid.uuid4().hex}.{secure_filename(ext)}"
            photo.save(os.path.join(UPLOAD_DIR, photo_name))
        db = get_db()
        cur = db.execute(
            "INSERT INTO shelves (user_id, name, photo, created_at)"
            " VALUES (?, ?, ?, ?)",
            (session["user_id"], name, photo_name, now()),
        )
        db.commit()
        flash("Shelf saved! Now list the books you can spot in the photo.")
        return redirect(url_for("shelf", shelf_id=cur.lastrowid))
    return render_template("new_shelf.html")


@app.route("/shelves/<int:shelf_id>")
def shelf(shelf_id):
    db = get_db()
    row = db.execute(
        """SELECT shelves.*, users.username, users.display_name
           FROM shelves JOIN users ON users.id = shelves.user_id
           WHERE shelves.id = ?""",
        (shelf_id,),
    ).fetchone()
    if row is None:
        abort(404)
    books = db.execute(
        "SELECT * FROM books WHERE shelf_id = ? ORDER BY title", (shelf_id,)
    ).fetchall()
    return render_template("shelf.html", shelf=row, books=books)


@app.route("/shelves/<int:shelf_id>/books", methods=["POST"])
@login_required
def add_books(shelf_id):
    db = get_db()
    row = db.execute("SELECT * FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
    if row is None:
        abort(404)
    if row["user_id"] != session["user_id"]:
        abort(403)
    added = 0
    for line in request.form["books"].splitlines():
        line = line.strip()
        if not line:
            continue
        title, _, author = line.partition("|")
        db.execute(
            "INSERT INTO books (owner_id, shelf_id, title, author, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], shelf_id, title.strip(), author.strip(), now()),
        )
        added += 1
    db.commit()
    flash(f"Added {added} book{'s' if added != 1 else ''}.")
    return redirect(url_for("shelf", shelf_id=shelf_id))


@app.route("/books/<int:book_id>/delete", methods=["POST"])
@login_required
def delete_book(book_id):
    db = get_db()
    book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        abort(404)
    if book["owner_id"] != session["user_id"]:
        abort(403)
    if book["status"] == "borrowed":
        flash("You can't remove a book while it is checked out.")
    else:
        db.execute(
            "DELETE FROM loans WHERE book_id = ? AND status = 'requested'",
            (book_id,),
        )
        db.execute("DELETE FROM books WHERE id = ?", (book_id,))
        db.commit()
        flash("Book removed.")
    return redirect(request.referrer or url_for("index"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/u/<username>")
def user_page(username):
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if user is None:
        abort(404)
    shelves = db.execute(
        """SELECT shelves.*,
                  (SELECT COUNT(*) FROM books WHERE books.shelf_id = shelves.id)
                  AS book_count
           FROM shelves WHERE user_id = ? ORDER BY created_at DESC""",
        (user["id"],),
    ).fetchall()
    books = db.execute(
        "SELECT * FROM books WHERE owner_id = ? ORDER BY title", (user["id"],)
    ).fetchall()
    return render_template(
        "user.html", user=user, shelves=shelves, books=books
    )


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    books = []
    if q:
        like = f"%{q}%"
        books = (
            get_db()
            .execute(
                """SELECT books.*, users.username, users.display_name
                   FROM books JOIN users ON users.id = books.owner_id
                   WHERE books.title LIKE ? OR books.author LIKE ?
                   ORDER BY books.status, books.title""",
                (like, like),
            )
            .fetchall()
        )
    return render_template("search.html", q=q, books=books)


# --------------------------------------------------------------------------
# Borrowing: request -> check out -> return
# --------------------------------------------------------------------------


@app.route("/books/<int:book_id>/request", methods=["POST"])
@login_required
def request_book(book_id):
    db = get_db()
    book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        abort(404)
    if book["owner_id"] == session["user_id"]:
        flash("That's your own book!")
    elif book["status"] != "available":
        flash("That book is already checked out.")
    elif db.execute(
        """SELECT 1 FROM loans WHERE book_id = ? AND borrower_id = ?
           AND status IN ('requested', 'checked_out')""",
        (book_id, session["user_id"]),
    ).fetchone():
        flash("You already have a request or loan for that book.")
    else:
        db.execute(
            "INSERT INTO loans (book_id, borrower_id, status, requested_at)"
            " VALUES (?, ?, 'requested', ?)",
            (book_id, session["user_id"], now()),
        )
        db.commit()
        flash("Request sent! The owner will approve the checkout.")
    return redirect(request.referrer or url_for("index"))


@app.route("/requests")
@login_required
def requests_page():
    db = get_db()
    incoming = db.execute(
        """SELECT loans.*, books.title, books.author,
                  users.username, users.display_name
           FROM loans
           JOIN books ON books.id = loans.book_id
           JOIN users ON users.id = loans.borrower_id
           WHERE books.owner_id = ? AND loans.status = 'requested'
           ORDER BY loans.requested_at""",
        (session["user_id"],),
    ).fetchall()
    lent_out = db.execute(
        """SELECT loans.*, books.title, books.author,
                  users.username, users.display_name
           FROM loans
           JOIN books ON books.id = loans.book_id
           JOIN users ON users.id = loans.borrower_id
           WHERE books.owner_id = ? AND loans.status = 'checked_out'
           ORDER BY loans.checked_out_at""",
        (session["user_id"],),
    ).fetchall()
    return render_template("requests.html", incoming=incoming, lent_out=lent_out)


def _loan_for_owner(loan_id):
    """Fetch a loan, verifying the current user owns the book."""
    loan = (
        get_db()
        .execute(
            """SELECT loans.*, books.owner_id FROM loans
               JOIN books ON books.id = loans.book_id
               WHERE loans.id = ?""",
            (loan_id,),
        )
        .fetchone()
    )
    if loan is None:
        abort(404)
    if loan["owner_id"] != session["user_id"]:
        abort(403)
    return loan


@app.route("/loans/<int:loan_id>/approve", methods=["POST"])
@login_required
def approve_loan(loan_id):
    db = get_db()
    loan = _loan_for_owner(loan_id)
    if loan["status"] != "requested":
        flash("That request is no longer pending.")
    else:
        db.execute(
            "UPDATE loans SET status = 'checked_out', checked_out_at = ?"
            " WHERE id = ?",
            (now(), loan_id),
        )
        db.execute(
            "UPDATE books SET status = 'borrowed' WHERE id = ?",
            (loan["book_id"],),
        )
        db.execute(
            """UPDATE loans SET status = 'declined'
               WHERE book_id = ? AND status = 'requested' AND id != ?""",
            (loan["book_id"], loan_id),
        )
        db.commit()
        flash("Checked out. Mark it returned when you get it back.")
    return redirect(url_for("requests_page"))


@app.route("/loans/<int:loan_id>/decline", methods=["POST"])
@login_required
def decline_loan(loan_id):
    db = get_db()
    loan = _loan_for_owner(loan_id)
    if loan["status"] == "requested":
        db.execute(
            "UPDATE loans SET status = 'declined' WHERE id = ?", (loan_id,)
        )
        db.commit()
        flash("Request declined.")
    return redirect(url_for("requests_page"))


@app.route("/loans/<int:loan_id>/return", methods=["POST"])
@login_required
def return_loan(loan_id):
    db = get_db()
    loan = _loan_for_owner(loan_id)
    if loan["status"] != "checked_out":
        flash("That loan isn't checked out.")
    else:
        db.execute(
            "UPDATE loans SET status = 'returned', returned_at = ? WHERE id = ?",
            (now(), loan_id),
        )
        db.execute(
            "UPDATE books SET status = 'available' WHERE id = ?",
            (loan["book_id"],),
        )
        db.commit()
        flash("Welcome back, book! It's available again.")
    return redirect(url_for("requests_page"))


@app.route("/loans/<int:loan_id>/cancel", methods=["POST"])
@login_required
def cancel_request(loan_id):
    db = get_db()
    loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if loan is None:
        abort(404)
    if loan["borrower_id"] != session["user_id"]:
        abort(403)
    if loan["status"] == "requested":
        db.execute(
            "UPDATE loans SET status = 'cancelled' WHERE id = ?", (loan_id,)
        )
        db.commit()
        flash("Request cancelled.")
    return redirect(url_for("my_loans"))


@app.route("/loans")
@login_required
def my_loans():
    db = get_db()
    loans = db.execute(
        """SELECT loans.*, books.title, books.author,
                  users.username, users.display_name
           FROM loans
           JOIN books ON books.id = loans.book_id
           JOIN users ON users.id = books.owner_id
           WHERE loans.borrower_id = ?
           ORDER BY loans.requested_at DESC""",
        (session["user_id"],),
    ).fetchall()
    return render_template("loans.html", loans=loans)


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
