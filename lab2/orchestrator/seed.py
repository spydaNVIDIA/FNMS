"""Create the local grader account without resetting existing users."""

from .db import Base, SessionLocal, engine
from .models import User
from .security import hash_password


def seed():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.query(User).filter_by(username="NYUgrader").first():
            db.add(User(username="NYUgrader", password_hash=hash_password("Courant2026!")))
            db.commit()


if __name__ == "__main__":
    seed()
