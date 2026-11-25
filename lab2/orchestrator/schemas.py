"""Pydantic request/response schemas.

Response models intentionally omit `password_hash`, so a hash can never be
serialized out of any endpoint (Rule 1).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    email: EmailStr | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateUserRequest(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr | None = None
    created_at: datetime


class TokenOut(BaseModel):
    token: str
    token_type: str = "bearer"


class HealthOut(BaseModel):
    status: str
