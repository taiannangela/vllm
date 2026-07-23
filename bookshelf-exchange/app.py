"""Bookshelf Exchange - a small book sharing site for a friends group.

Members photograph their bookshelves, Claude reads the book spines from the
photo to build the catalog, and friends search each other's collections and
borrow/return books through the site.
"""

import base64
import json
import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

import anthropic
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
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load settings (e.g. ANTHROPIC_API_KEY) saved by run.sh/run.bat into a .env
# file next to this script. Real environment variables take precedence.
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# All persistent data (database, photos, session secret) lives in DATA_DIR.
# Cloud hosts point this at their persistent disk; locally it's this folder.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DATABASE = os.path.join(DATA_DIR, "bookshelf.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")


def _secret_key():
    """Session-signing key that survives restarts (else logins drop)."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, "secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        key = os.urandom(24).hex()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(key)
        return key

SCAN_MODEL = os.environ.get("BOOK_SCAN_MODEL", "claude-opus-4-8")
MAX_PHOTO_EDGE = 2000  # px; plenty for Claude to read spines, keeps files small

app = Flask(__name__)
app.config["SECRET_KEY"] = _secret_key()
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
    photo      TEXT,  -- legacy single photo; migrated into shelf_photos
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelf_photos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shelf_id   INTEGER NOT NULL REFERENCES shelves(id),
    filename   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    shelf_id   INTEGER REFERENCES shelves(id),
    title      TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    publisher  TEXT NOT NULL DEFAULT '',
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
        cols = [row[1] for row in db.execute("PRAGMA table_info(books)")]
        if "publisher" not in cols:  # upgrade databases from older versions
            db.execute(
                "ALTER TABLE books ADD COLUMN publisher TEXT NOT NULL"
                " DEFAULT ''"
            )
        # Migrate legacy one-photo-per-shelf data into shelf_photos.
        for sid, photo in db.execute(
            "SELECT id, photo FROM shelves WHERE photo IS NOT NULL"
        ).fetchall():
            db.execute(
                "INSERT INTO shelf_photos (shelf_id, filename, created_at)"
                " VALUES (?, ?, ?)",
                (sid, photo, now()),
            )
        db.execute("UPDATE shelves SET photo = NULL WHERE photo IS NOT NULL")


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
    join_code = os.environ.get("JOIN_CODE", "").strip()
    if request.method == "POST":
        username = request.form["username"].strip()
        display_name = request.form["display_name"].strip() or username
        password = request.form["password"]
        if join_code and request.form.get("join_code", "").strip() != join_code:
            flash("That group code isn't right — ask whoever runs the site.")
        elif not username or not username.isalnum():
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
    return render_template("register.html", need_code=bool(join_code))


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


def save_photo(file_storage):
    """Validate, normalize, and save an uploaded shelf photo as JPEG.

    Returns the stored filename, or None if the upload isn't an image.
    """
    try:
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    img.thumbnail((MAX_PHOTO_EDGE, MAX_PHOTO_EDGE))
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85)
    return name


BOOKS_SCHEMA = {
    "type": "object",
    "properties": {
        "books": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                    "publisher": {"type": "string"},
                },
                "required": ["title", "author", "publisher"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["books"],
    "additionalProperties": False,
}

SCAN_PROMPT = (
    "This is a photo of a bookshelf. Identify every book whose spine or"
    " cover is legible. For each, give the title and author exactly as"
    " printed (use your knowledge of the book to fill in an author that is"
    " printed too small to read; leave author empty only if truly unknown)."
    " Also give the publisher when you can identify it — from a printed"
    " imprint on the spine, or from a distinctive edition design you"
    " recognize (for example Lamplighter Publishing's lamp emblem and"
    " ornate gilt spines, Penguin Classics' black spines, Easton Press"
    " leather bindings). Leave publisher empty rather than guessing."
    " List each physical book once, in shelf order. Skip objects that are"
    " not books and spines too blurry or obscured to identify."
)


class ScanError(Exception):
    pass


def scan_shelf_photo(photo_name):
    """Read book titles/authors from a shelf photo via the Claude vision API.

    Returns a list of {"title": ..., "author": ...} dicts.
    Raises ScanError with a user-friendly message on failure.
    """
    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        raise ScanError(
            "AI scanning isn't configured (set ANTHROPIC_API_KEY on the"
            " server). You can still add books by hand below."
        ) from exc
    with open(os.path.join(UPLOAD_DIR, photo_name), "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")
    try:
        response = client.messages.create(
            model=SCAN_MODEL,
            max_tokens=16000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_data,
                            },
                        },
                        {"type": "text", "text": SCAN_PROMPT},
                    ],
                }
            ],
            output_config={
                "format": {"type": "json_schema", "schema": BOOKS_SCHEMA}
            },
        )
    except (TypeError, anthropic.AuthenticationError) as exc:
        # The SDK raises TypeError at request time when no credentials
        # resolve; AuthenticationError when the key is invalid.
        raise ScanError(
            "AI scanning isn't configured — set a valid ANTHROPIC_API_KEY on"
            " the server. You can still add books by hand below."
        ) from exc
    except anthropic.APIError as exc:
        raise ScanError(
            "The AI scan didn't go through — try 'Scan photo' again in a"
            " minute, or add books by hand below."
        ) from exc
    if response.stop_reason == "refusal":
        raise ScanError("The AI couldn't process this photo — add books by hand below.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["books"]


def add_scanned_books(db, shelf_id, owner_id, found):
    """Insert scanned books, skipping titles already on the shelf."""
    existing = {
        row["title"].strip().lower()
        for row in db.execute(
            "SELECT title FROM books WHERE shelf_id = ?", (shelf_id,)
        )
    }
    added = 0
    for book in found:
        title = book["title"].strip()
        if not title or title.lower() in existing:
            continue
        existing.add(title.lower())
        db.execute(
            "INSERT INTO books (owner_id, shelf_id, title, author, publisher,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                shelf_id,
                title,
                book["author"].strip(),
                book.get("publisher", "").strip(),
                now(),
            ),
        )
        added += 1
    db.commit()
    return added


@app.route("/")
def index():
    db = get_db()
    shelves = db.execute(
        """SELECT shelves.*, users.username, users.display_name,
                  (SELECT COUNT(*) FROM books WHERE books.shelf_id = shelves.id)
                  AS book_count,
                  (SELECT filename FROM shelf_photos
                   WHERE shelf_photos.shelf_id = shelves.id
                   ORDER BY shelf_photos.id LIMIT 1) AS cover
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
        files = [f for f in request.files.getlist("photo") if f and f.filename]
        saved = _store_photos(files)
        if files and not saved:
            return render_template("new_shelf.html")
        db = get_db()
        cur = db.execute(
            "INSERT INTO shelves (user_id, name, created_at) VALUES (?, ?, ?)",
            (session["user_id"], name, now()),
        )
        db.commit()
        shelf_id = cur.lastrowid
        _attach_photos(db, shelf_id, saved)
        if saved:
            added, ok = _scan_photos(db, shelf_id, saved)
            if ok:
                flash(
                    f"Shelf saved! The AI spotted {added} book"
                    f"{'' if added == 1 else 's'} in your"
                    f" photo{'' if len(saved) == 1 else 's'} — check the"
                    " list and fix anything it misread."
                )
            else:
                flash("Shelf saved!")
        else:
            flash("Shelf saved! Add books below.")
        return redirect(url_for("shelf", shelf_id=shelf_id))
    return render_template("new_shelf.html")


def _store_photos(files):
    """Save uploaded images to disk; returns filenames, flashing skips."""
    saved = []
    for f in files:
        photo_name = save_photo(f)
        if photo_name is None:
            flash(f"{f.filename} doesn't look like an image — skipped it.")
        else:
            saved.append(photo_name)
    return saved


def _attach_photos(db, shelf_id, filenames):
    for photo_name in filenames:
        db.execute(
            "INSERT INTO shelf_photos (shelf_id, filename, created_at)"
            " VALUES (?, ?, ?)",
            (shelf_id, photo_name, now()),
        )
    if filenames:
        db.commit()


def _scan_photos(db, shelf_id, filenames):
    """Scan photos into the shelf's book list; returns (added, all_ok)."""
    added = 0
    for photo_name in filenames:
        try:
            found = scan_shelf_photo(photo_name)
        except ScanError as exc:
            flash(str(exc))
            return added, False
        added += add_scanned_books(db, shelf_id, session["user_id"], found)
    return added, True


def _shelf_photos(db, shelf_id):
    return db.execute(
        "SELECT * FROM shelf_photos WHERE shelf_id = ? ORDER BY id",
        (shelf_id,),
    ).fetchall()


def _owned_shelf(shelf_id):
    row = (
        get_db()
        .execute("SELECT * FROM shelves WHERE id = ?", (shelf_id,))
        .fetchone()
    )
    if row is None:
        abort(404)
    if row["user_id"] != session["user_id"]:
        abort(403)
    return row


@app.route("/shelves/<int:shelf_id>/photo", methods=["POST"])
@login_required
def upload_shelf_photo(shelf_id):
    _owned_shelf(shelf_id)
    db = get_db()
    files = [f for f in request.files.getlist("photo") if f and f.filename]
    saved = _store_photos(files)
    _attach_photos(db, shelf_id, saved)
    if not saved:
        if not files:
            flash("Choose a photo first.")
    else:
        added, ok = _scan_photos(db, shelf_id, saved)
        if ok:
            flash(
                f"The AI spotted {added} new book{'' if added == 1 else 's'}"
                " in the photo — check the list and fix anything it misread."
            )
    return redirect(url_for("shelf", shelf_id=shelf_id))


@app.route("/photos/<int:photo_id>/delete", methods=["POST"])
@login_required
def delete_photo(photo_id):
    db = get_db()
    row = db.execute(
        """SELECT shelf_photos.*, shelves.user_id FROM shelf_photos
           JOIN shelves ON shelves.id = shelf_photos.shelf_id
           WHERE shelf_photos.id = ?""",
        (photo_id,),
    ).fetchone()
    if row is None:
        abort(404)
    if row["user_id"] != session["user_id"]:
        abort(403)
    db.execute("DELETE FROM shelf_photos WHERE id = ?", (photo_id,))
    db.commit()
    try:
        os.remove(os.path.join(UPLOAD_DIR, row["filename"]))
    except OSError:
        pass
    flash("Photo removed. The books stay listed.")
    return redirect(url_for("shelf", shelf_id=row["shelf_id"]))


@app.route("/shelves/<int:shelf_id>/delete", methods=["POST"])
@login_required
def delete_shelf(shelf_id):
    row = _owned_shelf(shelf_id)
    db = get_db()
    checked_out = db.execute(
        """SELECT COUNT(*) FROM books
           WHERE shelf_id = ? AND status = 'borrowed'""",
        (shelf_id,),
    ).fetchone()[0]
    if checked_out:
        flash("You can't delete a shelf while one of its books is checked out.")
        return redirect(url_for("shelf", shelf_id=shelf_id))
    filenames = [p["filename"] for p in _shelf_photos(db, shelf_id)]
    db.execute(
        "DELETE FROM loans WHERE book_id IN"
        " (SELECT id FROM books WHERE shelf_id = ?)",
        (shelf_id,),
    )
    db.execute("DELETE FROM books WHERE shelf_id = ?", (shelf_id,))
    db.execute("DELETE FROM shelf_photos WHERE shelf_id = ?", (shelf_id,))
    db.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))
    db.commit()
    for filename in filenames:
        try:
            os.remove(os.path.join(UPLOAD_DIR, filename))
        except OSError:
            pass
    flash("Shelf deleted.")
    return redirect(url_for("user_page", username=current_user()["username"]))


@app.route("/shelves/<int:shelf_id>/scan", methods=["POST"])
@login_required
def rescan_shelf(shelf_id):
    db = get_db()
    _owned_shelf(shelf_id)
    filenames = [p["filename"] for p in _shelf_photos(db, shelf_id)]
    if not filenames:
        flash("This shelf has no photo to scan.")
    else:
        added, ok = _scan_photos(db, shelf_id, filenames)
        if ok:
            if added:
                flash(f"Scan found {added} new book{'' if added == 1 else 's'}.")
            else:
                flash("Scan finished — no new books beyond what's listed.")
    return redirect(url_for("shelf", shelf_id=shelf_id))


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
    return render_template(
        "shelf.html", shelf=row, books=books, photos=_shelf_photos(db, shelf_id)
    )


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
        parts = [p.strip() for p in line.split("|")]
        title = parts[0]
        author = parts[1] if len(parts) > 1 else ""
        publisher = parts[2] if len(parts) > 2 else ""
        db.execute(
            "INSERT INTO books (owner_id, shelf_id, title, author, publisher,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session["user_id"], shelf_id, title, author, publisher, now()),
        )
        added += 1
    db.commit()
    flash(f"Added {added} book{'s' if added != 1 else ''}.")
    return redirect(url_for("shelf", shelf_id=shelf_id))


@app.route("/books/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def edit_book(book_id):
    db = get_db()
    book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        abort(404)
    if book["owner_id"] != session["user_id"]:
        abort(403)
    if request.method == "POST":
        title = request.form["title"].strip()
        author = request.form["author"].strip()
        publisher = request.form.get("publisher", "").strip()
        if not title:
            flash("The title can't be empty.")
        else:
            db.execute(
                "UPDATE books SET title = ?, author = ?, publisher = ?"
                " WHERE id = ?",
                (title, author, publisher, book_id),
            )
            db.commit()
            flash("Book updated.")
            if book["shelf_id"]:
                return redirect(url_for("shelf", shelf_id=book["shelf_id"]))
            return redirect(
                url_for("user_page", username=current_user()["username"])
            )
    return render_template("edit_book.html", book=book)


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
                  AS book_count,
                  (SELECT filename FROM shelf_photos
                   WHERE shelf_photos.shelf_id = shelves.id
                   ORDER BY shelf_photos.id LIMIT 1) AS cover
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
                      OR books.publisher LIKE ?
                   ORDER BY books.status, books.title""",
                (like, like, like),
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
    elif db.execute(
        """SELECT 1 FROM loans WHERE book_id = ? AND borrower_id = ?
           AND status IN ('requested', 'checked_out')""",
        (book_id, session["user_id"]),
    ).fetchone():
        flash("You already have a request or hold on that book.")
    else:
        db.execute(
            "INSERT INTO loans (book_id, borrower_id, status, requested_at)"
            " VALUES (?, ?, 'requested', ?)",
            (book_id, session["user_id"], now()),
        )
        db.commit()
        if book["status"] == "available":
            flash("Request sent! The owner will approve the checkout.")
        else:
            queue = db.execute(
                "SELECT COUNT(*) FROM loans WHERE book_id = ?"
                " AND status = 'requested'",
                (book_id,),
            ).fetchone()[0]
            flash(
                f"Hold placed — you're #{queue} in line. The owner will"
                " approve it once the book comes back."
            )
    return redirect(request.referrer or url_for("index"))


@app.route("/requests")
@login_required
def requests_page():
    db = get_db()
    incoming = db.execute(
        """SELECT loans.*, books.title, books.author,
                  books.status AS book_status,
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
            """SELECT loans.*, books.owner_id, books.status AS book_status
               FROM loans
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
    elif loan["book_status"] != "available":
        flash(
            "That book is still checked out — mark it returned first, then"
            " approve the hold."
        )
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
        next_hold = db.execute(
            """SELECT users.display_name FROM loans
               JOIN users ON users.id = loans.borrower_id
               WHERE loans.book_id = ? AND loans.status = 'requested'
               ORDER BY loans.requested_at LIMIT 1""",
            (loan["book_id"],),
        ).fetchone()
        if next_hold:
            flash(
                f"Welcome back, book! {next_hold['display_name']} has a hold"
                " on it — approve their request below to pass it along."
            )
        else:
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
