import logging
import os
import secrets
import time

import httpx
import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from tokens import (
    load_tokens, save_tokens, basic_header,
    require_auth, get_valid_token, _do_refresh
)
from workorders import router as wo_router
from notifier import run_stale_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

NOTIFY_TZ = os.getenv("NOTIFY_TZ", "America/New_York")

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID    = os.getenv("SC_CLIENT_ID", "")
REDIRECT_URI = os.getenv("SC_REDIRECT_URI", "")
SC_AUTH_BASE = os.getenv("SC_AUTH_BASE", "https://login.servicechannel.com")
SC_API_BASE  = os.getenv("SC_API_BASE",  "https://api.servicechannel.com")

app = FastAPI(
    title="ServiceChannel Automation Backend",
    description="OAuth 2.0 auth + work order automation for ServiceChannel",
    version="3.0.0",
)
app.include_router(wo_router)


@app.on_event("startup")
async def start_scheduler():
    try:
        tz = pytz.timezone(NOTIFY_TZ)
    except Exception:
        tz = pytz.utc
        logger.warning("Invalid NOTIFY_TZ '%s', falling back to UTC", NOTIFY_TZ)

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        run_stale_check,
        CronTrigger(hour=9, minute=0, timezone=tz),
        args=["Morning Check (9 AM)"],
        id="morning_check",
    )
    scheduler.add_job(
        run_stale_check,
        CronTrigger(hour=15, minute=0, timezone=tz),
        args=["Afternoon Check (3 PM)"],
        id="afternoon_check",
    )
    scheduler.start()
    logger.info("Scheduler started — checks at 9 AM and 3 PM %s", NOTIFY_TZ)

# ── OAuth: Authorization Code flow ────────────────────────────────────────────
_pending_states: dict[str, float] = {}


@app.get("/auth/login", summary="Open in browser to authenticate with ServiceChannel", tags=["Auth"])
async def login():
    state = secrets.token_urlsafe(16)
    _pending_states[state] = time.time()

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


@app.get("/auth/callback", summary="OAuth callback — set this URL as your Callback URI in ServiceChannel", tags=["Auth"])
async def callback(code: str, state: str):
    if state not in _pending_states:
        raise HTTPException(400, "Invalid or expired state — start fresh via /auth/login")
    del _pending_states[state]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SC_AUTH_BASE}/oauth/token",
            headers={**basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
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
    save_tokens(tokens)

    return HTMLResponse("""
    <html><body style="font-family:system-ui,sans-serif;text-align:center;margin-top:100px;color:#1a1a1a">
      <h2>&#10003; Authenticated successfully!</h2>
      <p>Your tokens are stored. Close this tab — your automation is ready.</p>
    </body></html>
    """)


# ── OAuth: Password Credentials (simpler alternative) ────────────────────────

class PasswordLoginBody(BaseModel):
    username: str
    password: str


@app.post("/auth/login-password", summary="Authenticate directly with SC username + password", tags=["Auth"])
async def login_password(
    body: PasswordLoginBody,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SC_AUTH_BASE}/oauth/token",
            headers={**basic_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "password", "username": body.username, "password": body.password},
        )

    if resp.status_code != 200:
        raise HTTPException(400, f"Authentication failed: {resp.text}")

    tokens = resp.json()
    tokens["_obtained_at"] = time.time()
    save_tokens(tokens)
    return {"status": "authenticated", "expires_in": tokens.get("expires_in")}


# ── Token access ──────────────────────────────────────────────────────────────

@app.get("/auth/token", summary="Get current access token (auto-refreshes)", tags=["Auth"])
async def get_token(x_api_secret: str | None = Header(default=None)):
    require_auth(x_api_secret)
    tokens = load_tokens()
    if not tokens:
        raise HTTPException(
            404, "No token stored. Authenticate first via GET /auth/login"
        )
    obtained_at = tokens.get("_obtained_at", 0)
    expires_in  = tokens.get("expires_in", 600)
    if time.time() - obtained_at > expires_in * 0.85:
        tokens = await _do_refresh(tokens)
    return {
        "access_token": tokens["access_token"],
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in"),
    }


@app.post("/auth/refresh", summary="Force-refresh the stored access token", tags=["Auth"])
async def force_refresh(x_api_secret: str | None = Header(default=None)):
    require_auth(x_api_secret)
    tokens = load_tokens()
    if not tokens:
        raise HTTPException(404, "No token stored. Authenticate first.")
    new = await _do_refresh(tokens)
    return {"access_token": new["access_token"], "expires_in": new.get("expires_in")}


# ── Generic SC API proxy ──────────────────────────────────────────────────────

@app.api_route(
    "/sc/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    summary="Proxy: forward any call to api.servicechannel.com with auto-auth",
    tags=["Proxy"],
)
async def proxy(
    path: str,
    request: Request,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)
    token = await get_valid_token()

    body = await request.body()
    forward_headers = {
        "Authorization": f"Bearer {token}",
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

    if "application/json" in resp.headers.get("content-type", ""):
        return resp.json()
    return {"status_code": resp.status_code, "body": resp.text}


# ── Manual notification trigger ──────────────────────────────────────────────

@app.post("/notify/check-now", summary="Run the stale WO check immediately and send Teams alert", tags=["Notifications"])
async def check_now(x_api_secret: str | None = Header(default=None)):
    require_auth(x_api_secret)
    count = await run_stale_check("Manual Check")
    return {
        "status": "sent" if count >= 0 else "error",
        "stale_work_orders_found": count,
    }


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
async def health():
    tokens = load_tokens()
    has_token = bool(tokens.get("access_token"))
    age = round(time.time() - tokens["_obtained_at"]) if has_token else None
    return {"status": "ok", "authenticated": has_token, "token_age_seconds": age}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
