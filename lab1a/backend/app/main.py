"""FastAPI application: auth + user CRUD endpoints.

Rule 1: password_hash is never returned (response schemas exclude it).
Rule 2: missing/bad/expired token -> 401 (see security.get_current_user).
Rule 3: touching another user's :id -> 404, applied consistently to GET,
        PATCH, and DELETE. We chose 404 over 403 so the API never reveals
        that another account with that id exists (avoids user enumeration).
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from .models import User
from .schemas import (
    HealthOut,
    LoginRequest,
    RegisterRequest,
    TokenOut,
    UpdateUserRequest,
    UserOut,
)
from .security import create_token, get_current_user, hash_password, verify_password


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables if they don't exist yet (simple, no migration tool).
    Base.metadata.create_all(bind=engine)
    yield
    # Shutdown: nothing to tear down for now.


app = FastAPI(title="FNMS Lab1a Auth API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", response_model=HealthOut)
def healthz() -> HealthOut:
    return HealthOut(status="ok")


@app.post("/api/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> User:
    user = User(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Generic message: don't confirm which field collided (anti-enumeration).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not create account",
        ) from None
    db.refresh(user)
    return user


@app.post("/api/auth/login", response_model=TokenOut)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenOut:
    user = db.query(User).filter(User.username == body.username).first()
    # Same generic error whether the user is missing or the password is wrong.
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    return TokenOut(token=create_token(user.id))


@app.get("/api/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


def _get_own_user_or_404(user_id: int, current_user: User) -> User:
    """Rule 3: only your own :id is visible; anything else -> 404."""
    if user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return current_user


@app.get("/api/users/{user_id}", response_model=UserOut)
def read_user(user_id: int, current_user: User = Depends(get_current_user)) -> User:
    return _get_own_user_or_404(user_id, current_user)


@app.patch("/api/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UpdateUserRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    user = _get_own_user_or_404(user_id, current_user)
    if body.email is not None:
        user.email = body.email
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    db.commit()
    db.refresh(user)
    return user


@app.delete("/api/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    user = _get_own_user_or_404(user_id, current_user)
    db.delete(user)
    db.commit()
