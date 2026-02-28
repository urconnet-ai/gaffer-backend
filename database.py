"""
Gaffer — database.py
User storage using Supabase (supabase.com).
Free tier handles Phase 1 easily.

Schema (run in Supabase SQL editor):

  create table users (
    id          bigserial primary key,
    team_id     integer unique not null,
    team_name   text,
    email       text,
    mode        text default 'assisted',
    active      boolean default true,
    created_at  timestamptz default now(),
    updated_at  timestamptz default now()
  );

  create index on users(team_id);
  create index on users(email);
"""

import os
import httpx
from typing import Optional

SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY", "")

HEADERS = lambda: {
    "apikey":        SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}


async def save_user(user: dict) -> Optional[dict]:
    """Insert or update a user by team_id (upsert)."""
    if not SUPABASE_URL:
        print("[db] SUPABASE_URL not set — skipping database write")
        return user

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/users",
            headers={**HEADERS(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json={
                "team_id":   user["team_id"],
                "team_name": user.get("team_name"),
                "email":     user.get("email"),
                "mode":      user.get("mode", "assisted"),
                "active":    True,
            }
        )
        if response.status_code in (200, 201):
            data = response.json()
            return data[0] if isinstance(data, list) else data
        print(f"[db] save_user error {response.status_code}: {response.text}")
        return None


async def get_user(team_id: int) -> Optional[dict]:
    """Fetch a user by team_id."""
    if not SUPABASE_URL:
        return None

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/users",
            headers=HEADERS(),
            params={"team_id": f"eq.{team_id}", "limit": "1"}
        )
        data = response.json()
        return data[0] if isinstance(data, list) and data else None


async def get_all_active_users() -> list:
    """Returns all active users — used by the scheduler for bulk briefings."""
    if not SUPABASE_URL:
        return []

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/users",
            headers=HEADERS(),
            params={"active": "eq.true", "limit": "1000"}
        )
        return response.json() if response.status_code == 200 else []
