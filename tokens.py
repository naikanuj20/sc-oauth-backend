"""Shared token storage, refresh, and auth guard — used by main.py and workorders.py."""
import os
import json
import time
import base64
from pathlib import Path

import httpx
from fastapi import HTTPException

TOKEN_FILE   = Path(os.getenv("TOKEN_FILE", "tokens.json"))
CLIENT_ID    = os.getenv("SC_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SC_CLIENT_SECRET", "")
SC_AUTH_BASE = os.getenv("SC_AUTH_BASE", "https://login.servicechannel.com")
API_SECRET   = os.getenv("API_SECRET", "")


def load_tokens() -> dict:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return {}


def save_tokens(data: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(data, indent=2))


def basic_header() -> dict:
    encoded = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def require_auth(secret: str | None) -> None:
    if not API_SECRET:
        raise HTTPException(500, "API_SECRET env var is not configured on the server")
    if secret != API_SECRET:
        raise HTTPException(401, "Unauthorized — wrong or missing X-Api-Secret header")


async def get_valid_token() -> str:
    """Return a non-expired access token, refreshing if needed."""
    tokens = load_tokens()
    if not tokens:
        raise HTTPException(
            404, "No token stored — authenticate first via GET /auth/login"
        )
    obtained_at = tokens.get("_obtained_at", 0)
    expires_in  = tokens.get("expires_in", 600)
    if time.time() - obtained_at > expires_in * 0.85:
        tokens = await _do_refresh(tokens)
    return tokens["access_token"]


async def _do_refresh(tokens: dict) -> dict:
    refresh_tok = tokens.get("refresh_token")
    if not refresh_tok:
        raise HTTPException(
            401, "Token expired and no refresh_token. Re-authenticate via /auth/login"
        )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SC_AUTH_BASE}/oauth/token",
            headers={**basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
        )
    if resp.status_code != 200:
        raise HTTPException(400, f"Token refresh failed: {resp.text}")
    new = resp.json()
    new["_obtained_at"] = time.time()
    save_tokens(new)
    return new


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
