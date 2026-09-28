"""Seed the grader account required by the assignment.

Creates:  username=NYUgrader  password=Courant2026!
Idempotent: running it again resets the grader's password instead of erroring.

Usage:
    python seed.py
"""

from app.db import Base, SessionLocal, engine
from app.models import User
from app.security import hash_password

GRADER_USERNAME = "NYUgrader"
GRADER_PASSWORD = "Courant2026!"


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == GRADER_USERNAME).first()
        if user is None:
            user = User(
                username=GRADER_USERNAME,
                email="nyugrader@example.com",
                password_hash=hash_password(GRADER_PASSWORD),
            )
            db.add(user)
            print(f"Created grader account: {GRADER_USERNAME}")
        else:
            user.password_hash = hash_password(GRADER_PASSWORD)
            print(f"Grader account already existed; password reset: {GRADER_USERNAME}")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
