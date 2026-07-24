"""Reset a member's password from the command line.

Usage (from the bookshelf-exchange folder, with the server stopped or running):
    .venv/bin/python reset_password.py USERNAME          # Mac/Linux
    .venv\\Scripts\\python reset_password.py USERNAME     # Windows
"""

import getpass
import sqlite3
import sys

from werkzeug.security import generate_password_hash

from app import DATABASE

if len(sys.argv) != 2:
    sys.exit("Usage: python reset_password.py USERNAME")

username = sys.argv[1]
db = sqlite3.connect(DATABASE)
row = db.execute(
    "SELECT id FROM users WHERE username = ?", (username,)
).fetchone()
if row is None:
    sys.exit(f"No user named {username!r}.")

password = getpass.getpass(f"New password for {username}: ")
if len(password) < 6:
    sys.exit("Password must be at least 6 characters.")
db.execute(
    "UPDATE users SET password_hash = ? WHERE id = ?",
    (generate_password_hash(password), row[0]),
)
db.commit()
print(f"Password updated for {username}.")
