import os
import json
import time
import base64
import secrets
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("SC_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SC_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("SC_REDIRECT_URI", "")
SC_AUTH_BASE  = os.getenv("SC_AUTH_BASE", "https://login.servicechannel.com")
SC_API_BASE   = os.getenv("SC_API_BASE",  "https://api.servicechannel.com")
API_SECRET    = os.getenv("API_SECRET", "")     # protects all automation endpoints
TOKEN_FILE    = Path(os.getenv("TOKEN_FILE", "tokens.json"))

app = FastAPI(
    title="ServiceChannel OAuth Backend",
    description="Handles OAuth 2.0 auth + token refresh for ServiceChannel API automation",
    version="1.0.0",
)

# ── Helpers ───────────────────────────────────────────────────────────────────
_pending_states: dict[str, float] = {}


def _basic_header() -> dict:
    encoded = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _load_tokens() -> dict:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return {}


def _save_tokens(data: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(data, indent=2))


def _require_auth(secret: str | None) -> None:
    if not API_SECRET:
        raise HTTPException(500, "API_SECRET env var is not set on the server")
    if secret != API_SECRET:
        raise HTTPException(401, "Unauthorized — wrong or missing X-Api-Secret header")


# ── OAuth: Authorization Code flow ────────────────────────────────────────────

@app.get("/auth/login", summary="Step 1 — open this URL in a browser to log in to ServiceChannel")
async def login():
    state = secrets.token_urlsafe(16)
    _pending_states[state] = time.time()

    # clean up states older than 10 min
    stale = [k for k, v in _pending_states.items() if time.time() - v > 600]
    for k in stale:
        del _pending_states[k]

    url = (
        f"{SC_AUTH_BASE}/oauth/authorize"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
    )
    return RedirectResponse(url)


@app.get("/auth/callback", summary="Step 2 — ServiceChannel redirects here after login (set this as your Callback URI)")
async def callback(code: str, state: str):
    if state not in _pending_states:
        raise HTTPException(400, "Invalid or expired state — start fresh via /auth/login")
    del _pending_states[state]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SC_AUTH_BASE}/oauth/token",
            headers={**_basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(400, f"Token exchange failed: {resp.text}")

    tokens = resp.json()
    tokens["_obtained_at"] = time.time()
    _save_tokens(tokens)

    return HTMLResponse("""
    <html><body style="font-family:system-ui,sans-serif;text-align:center;margin-top:100px;color:#1a1a1a">
      <h2>&#10003; Authenticated successfully!</h2>
      <p>Your tokens are stored. Close this tab and return to your automation.</p>
    </body></html>
    """)


# ── OAuth: Password Credentials flow (simpler alternative for automation) ─────

class PasswordLoginBody(BaseModel):
    username: str
    password: str


@app.post("/auth/login-password", summary="Alternative — exchange SC username+password for a token directly")
async def login_password(
    body: PasswordLoginBody,
    x_api_secret: str | None = Header(default=None),
):
    _require_auth(x_api_secret)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SC_AUTH_BASE}/oauth/token",
            headers={**_basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "password",
                "username": body.username,
                "password": body.password,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(400, f"Authentication failed: {resp.text}")

    tokens = resp.json()
    tokens["_obtained_at"] = time.time()
    _save_tokens(tokens)
    return {"status": "authenticated", "expires_in": tokens.get("expires_in")}


# ── Token access ──────────────────────────────────────────────────────────────

@app.get("/auth/token", summary="Get the current valid access token (auto-refreshes if near expiry)")
async def get_token(x_api_secret: str | None = Header(default=None)):
    _require_auth(x_api_secret)

    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(
            404,
            "No token stored. Authenticate first via GET /auth/login "
            "or POST /auth/login-password",
        )

    obtained_at = tokens.get("_obtained_at", 0)
    expires_in  = tokens.get("expires_in", 600)

    # refresh when 85 % of lifetime has elapsed
    if time.time() - obtained_at > expires_in * 0.85:
        tokens = await _do_refresh(tokens)

    return {
        "access_token": tokens["access_token"],
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in"),
    }


@app.post("/auth/refresh", summary="Force-refresh the stored access token")
async def force_refresh(x_api_secret: str | None = Header(default=None)):
    _require_auth(x_api_secret)
    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(404, "No token stored. Authenticate first.")
    new = await _do_refresh(tokens)
    return {"access_token": new["access_token"], "expires_in": new.get("expires_in")}


async def _do_refresh(tokens: dict) -> dict:
    refresh_tok = tokens.get("refresh_token")
    if not refresh_tok:
        raise HTTPException(
            401,
            "Token expired and no refresh_token available. Re-authenticate via /auth/login",
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SC_AUTH_BASE}/oauth/token",
            headers={**_basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok},
        )

    if resp.status_code != 200:
        raise HTTPException(400, f"Token refresh failed: {resp.text}")

    new_tokens = resp.json()
    new_tokens["_obtained_at"] = time.time()
    _save_tokens(new_tokens)
    return new_tokens


# ── API Proxy — forward any SC API call with auto-auth ────────────────────────

@app.api_route(
    "/sc/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    summary="Proxy: forwards calls to https://api.servicechannel.com/<path> with your Bearer token",
)
async def proxy(
    path: str,
    request: Request,
    x_api_secret: str | None = Header(default=None),
):
    _require_auth(x_api_secret)

    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(404, "No token stored. Authenticate first.")

    obtained_at = tokens.get("_obtained_at", 0)
    expires_in  = tokens.get("expires_in", 600)
    if time.time() - obtained_at > expires_in * 0.85:
        tokens = await _do_refresh(tokens)

    body = await request.body()
    forward_headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "Accept": "application/json",
    }
    target = f"{SC_API_BASE}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method=request.method,
            url=target,
            headers=forward_headers,
            content=body,
        )

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        return resp.json()
    return {"status_code": resp.status_code, "body": resp.text}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    tokens = _load_tokens()
    has_token = bool(tokens.get("access_token"))
    age = round(time.time() - tokens["_obtained_at"]) if has_token else None
    return {"status": "ok", "authenticated": has_token, "token_age_seconds": age}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
