"""Shared dependencies — auth, Supabase client."""

import os

from fastapi import Header, HTTPException
from supabase import Client, create_client

_API_KEY = os.environ.get("STENO_API_KEY", "")


def get_supabase() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def check_auth(authorization: str | None = Header(default=None)):
    if not _API_KEY:
        return
    if not authorization or authorization != f"Bearer {_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")
