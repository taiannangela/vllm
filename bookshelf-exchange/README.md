# 📚 Bookshelf Exchange

A small website for a friends group to share their bookshelves and borrow
books from each other.

## What it does

- **Accounts** — each friend creates their own account.
- **Shelf photos** — take a picture of a bookshelf, upload it, and it's
  saved to your account for everyone in the group to browse.
- **AI catalog** — when a photo is uploaded, Claude reads the book spines
  and fills in the title/author list automatically. A "Scan photo" button
  re-runs it, and a manual form covers any spines it couldn't read.
- **Search** — search by title or author across *everyone's* shelves and
  see whose library has the book and whether it's available.
- **Borrow / check out / return** — click **Borrow** on any available
  book. The owner approves the checkout from their "Lending desk" page and
  marks the book returned when it comes back, so everyone can always see
  where a book is.

## Running it

Requires Python 3.10+. The start script handles everything else:

```bash
cd bookshelf-exchange
./run.sh          # on Windows: double-click run.bat
```

The first run sets up the environment and asks for your Claude API key
(get one at <https://platform.claude.com> — it powers the AI book-spine
reading and is saved to a local `.env` file so you're only asked once).
Then open <http://localhost:5000> in a browser and click **Join** to
create an account.

Without a key everything still works except AI scanning — the site tells
you to add books by hand. Each shelf-photo scan costs roughly a cent or
two (model: `claude-opus-4-8`; override with the `BOOK_SCAN_MODEL` env
var).

<details><summary>Manual setup (if you prefer not to use the script)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```

</details>

Data is stored in a local SQLite file (`bookshelf.db`) and uploaded photos
go in `uploads/`, so backing up the site is just copying those two things.

To share it with friends on the same Wi-Fi network, run it with:

```bash
flask --app app run --host 0.0.0.0
```

and give them your computer's local IP address (e.g. `http://192.168.1.20:5000`).
For hosting on the internet, deploy it to any small host that runs Python
(PythonAnywhere, Render, Fly.io, a Raspberry Pi, ...) and set a `SECRET_KEY`
environment variable so logins survive server restarts.

## Ideas for later

- Auto-detect book titles from the shelf photo with an AI vision API.
- Due dates and reminder emails for borrowed books.
- Book cover images and ratings/reviews.
