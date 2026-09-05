"""Manage staff accounts from the shell (there is no self-signup).

    python -m server.users add "KyJah" kyjah@example.com      # prints a temp password
    python -m server.users set-password kyjah@example.com     # prompts; clears must-change
    python -m server.users list
    python -m server.users remove kyjah@example.com

Run from your laptop with DATABASE_URL set to the same Postgres the Vercel app
uses (deploy/README.md), so the CLI and the app share one database. Without
DATABASE_URL it talks to the local SQLite file instead.
"""
import getpass
import secrets
import sys

from . import auth


def main(argv):
    if len(argv) < 2 or argv[1] not in ("add", "set-password", "list", "remove"):
        print(__doc__)
        return 2
    con = auth.connect()
    cmd = argv[1]
    if cmd == "list":
        for u in auth.list_users(con):
            print(f"{u.id:>3}  {u.name:<10} {u.email:<32} {'temp password' if u.must_change else 'ok'}")
        return 0
    if cmd == "add":
        if len(argv) != 4:
            print("usage: add <Name> <email>")
            return 2
        name, email = argv[2], argv[3]
        if auth.get_user_by_email(con, email):
            print(f"{email} already exists")
            return 1
        temp = secrets.token_urlsafe(9)
        auth.add_user(con, name, email, temp, must_change=True)
        print(f"Added {name} <{email}>.\nTemporary password: {temp}\n"
              "They must change it on first sign-in.")
        return 0
    if cmd == "set-password":
        if len(argv) != 3:
            print("usage: set-password <email>")
            return 2
        u = auth.get_user_by_email(con, argv[2])
        if not u:
            print("no such user")
            return 1
        pw = getpass.getpass("New password (12+ chars): ")
        if len(pw) < 12:
            print("too short")
            return 1
        auth.set_password(con, u.id, pw, must_change=False)
        print(f"Password set for {u.name}.")
        return 0
    if cmd == "remove":
        if len(argv) != 3:
            print("usage: remove <email>")
            return 2
        print("removed" if auth.remove_user(con, argv[2]) else "no such user")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
