#!/usr/bin/env python3
"""remove_user.py
Remove one or more users, found by uid, and the data that belongs only to them.

Deleting a user from User Management does nothing when other rows still point at them: the database
refuses, and the page says it worked anyway. This does it properly, and shows what it will do first.

  python scripts/remove_user.py MathMemer                 # dry run: shows what would be removed, changes nothing
  python scripts/remove_user.py MathMemer --apply         # really remove them
  python scripts/remove_user.py a b c --apply             # several; each is its own transaction
  python scripts/remove_user.py x --apply --also posts    # also delete their rows in the named table(s)

On the server, from the Flask folder:
  docker compose exec web python scripts/remove_user.py MathMemer
  docker compose exec web python scripts/remove_user.py MathMemer --apply

What is removed with the user, without asking: their microblog posts, and the rows that only link them to
something (their sections, personas, classrooms they are a student in, presence rooms they are in, events they
signed up for). An account that owns an event stops the removal, like any other table that holds their rows.

It refuses (STOP, nothing changed for that user) when:
  - no user has that uid,
  - the user is staff (an Admin or Teacher),
  - any other table holds rows for them, for example a classroom they own, their posts, leaderboard entries or
    study records. The list comes from the database itself, so a table the code does not know is found too.
    --also TABLE says you know what is in that table and want those rows deleted as well.

Each user is removed in one transaction: all of it happens, or none of it does. A Spring account, if they
made one, is separate: remove it in Spring.

Exit: 0 every user removed (or, as a dry run, nothing to stop), 1 any user not found, refused or failed.
"""
import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from sqlalchemy import inspect, text
from main import app, db  # registers every model
from model import account_types

# Rows that only link the user to something: removed with them
LINKS = {"user_sections", "user_personas", "classroom_student", "presence_room_members", "event_participants"}
# Content the user made that goes with them
CONTENT = {"microblogs"}


def _references(inspector):
    """{table: [columns]} for every table with a column that points at users, as the database enforces it."""
    refs = {}
    for table in inspector.get_table_names():
        if table == "users":
            continue
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] == "users":
                refs.setdefault(table, []).extend(c for c in fk["constrained_columns"] if c not in refs.get(table, []))
    return refs


def remove_users(uids, apply=False, also=(), out=print):
    """Returns the exit code."""
    with app.app_context():
        quote = db.engine.dialect.identifier_preparer.quote
        refs = _references(inspect(db.engine))
        unknown = [t for t in also if t not in refs]
        if unknown:
            out(f"STOP: --also {', '.join(unknown)}: no such table points at users. Nothing changed.")
            return 1
        failed = False
        for target in uids:
            db.session.rollback()
            row = db.session.execute(text("SELECT id, _uid, _name, _role FROM users WHERE _uid = :u"), {"u": target}).fetchone()
            if row is None:
                out(f"{target}: no user with this uid: nothing to do")
                failed = True
                continue
            user_id, uid, name, role = row
            out(f"user {user_id} = {uid}  (name: {name}, role: {role})")
            if role in account_types.staff_roles():
                out(f"  STOP: {uid} is staff ({role}). This script does not remove staff. Nothing changed.")
                failed = True
                continue
            held = {(t, c): db.session.execute(text(f"SELECT COUNT(*) FROM {quote(t)} WHERE {quote(c)} = :i"), {"i": user_id}).scalar()
                    for t, cols in refs.items() for c in cols}
            removed_with = LINKS | CONTENT | set(also)
            blocked = {f"{t}.{c}": n for (t, c), n in held.items() if n and t not in removed_with}
            if blocked:
                out(f"  STOP: other tables also hold rows for this user: {blocked}. Nothing changed."
                    f" (--also TABLE deletes the user's rows in that table too, if you know what is in it.)")
                failed = True
                continue
            for (t, c), n in sorted(held.items()):
                if n and t in removed_with:
                    out(f"  {t}: {n} row(s) {'link them and' if t in LINKS else 'belong to them and'} will be removed")
            if not apply:
                out("  DRY RUN: nothing changed. Add --apply to do it for real.")
                continue
            try:
                for (t, c), n in sorted(held.items()):
                    if n and t in removed_with:
                        db.session.execute(text(f"DELETE FROM {quote(t)} WHERE {quote(c)} = :i"), {"i": user_id})
                db.session.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                out(f"  FAILED and rolled back, nothing changed: {e}")
                failed = True
                continue
            gone = db.session.execute(text("SELECT COUNT(*) FROM users WHERE id = :i"), {"i": user_id}).scalar() == 0
            out(f"  DONE: {uid} and their own data removed. A Spring account, if they made one, is separate: remove it in Spring." if gone
                else f"  STILL THERE: check {uid} by hand")
            failed = failed or not gone
        return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="Remove users by uid, and the data that belongs only to them.")
    parser.add_argument("uids", nargs="+", metavar="UID")
    parser.add_argument("--apply", action="store_true", help="really remove them (without it, a dry run)")
    parser.add_argument("--also", nargs="+", default=[], metavar="TABLE", help="also delete the user's rows in these tables")
    args = parser.parse_args()
    sys.exit(remove_users(args.uids, apply=args.apply, also=tuple(args.also)))


if __name__ == "__main__":
    main()
