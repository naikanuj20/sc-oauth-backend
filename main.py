import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
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
from notifier import run_stale_check, send_teams_notification, fetch_all_active_workorders
from notion_sync import sync_workorders, upsert_workorder, query_recently_edited_pages, clear_notes_to_sc

SC_API = os.getenv("SC_API_BASE", "https://api.servicechannel.com")

# Tracks the last time we pushed SC→Notion so the reverse-sync can ignore those
_last_sc_to_notion: datetime = datetime.min.replace(tzinfo=timezone.utc)

# Notion status → SC {primary, extended} mapping
_NOTION_TO_SC: dict[str, dict] = {
    "OPEN":                             {"primary": "OPEN"},
    "IN PROGRESS":                      {"primary": "IN PROGRESS"},
    "IN PROGRESS:PARTS ON ORDER":       {"primary": "IN PROGRESS", "extended": "PARTS ON ORDER"},
    "IN PROGRESS:INCOMPLETE":           {"primary": "IN PROGRESS", "extended": "INCOMPLETE"},
    "IN PROGRESS:DISPATCH CONFIRMED":   {"primary": "IN PROGRESS", "extended": "DISPATCH CONFIRMED"},
    "IN PROGRESS:WAITING FOR QUOTE":    {"primary": "IN PROGRESS", "extended": "WAITING FOR QUOTE"},
    "COMPLETED":                        {"primary": "COMPLETED"},
    "COMPLETED:CONFIRMED":              {"primary": "COMPLETED", "extended": "CONFIRMED"},
    "COMPLETED:NO CHARGE":              {"primary": "COMPLETED", "extended": "NO CHARGE"},
    "INVOICED":                         {"primary": "INVOICED"},
    "INVOICED:CONFIRMED":               {"primary": "INVOICED", "extended": "CONFIRMED"},
}

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

    async def _notion_daily_sync():
        """Full Notion sync — upserts every active WO, archives completed ones."""
        global _last_sc_to_notion
        logger.info("Starting daily Notion sync")
        try:
            wos = await fetch_all_active_workorders()
            result = await sync_workorders(wos)
            _last_sc_to_notion = datetime.now(timezone.utc)
            logger.info("Daily Notion sync complete: %s", result)
        except Exception:
            logger.exception("Daily Notion sync failed")

    async def _stale_check_and_sync(label: str):
        """Stale WO check → Teams alert, then refresh Notion."""
        global _last_sc_to_notion
        await run_stale_check(label)
        try:
            wos = await fetch_all_active_workorders()
            result = await sync_workorders(wos)
            _last_sc_to_notion = datetime.now(timezone.utc)
            logger.info("Notion sync after %s: %s", label, result)
        except Exception:
            logger.exception("Notion sync failed after %s", label)

    # Notion priority label → SC priority string
    _NOTION_PRIORITY_TO_SC = {
        "P1 (2-4 HOURS)": "P1 - Emergency",
        "P2 (24 HOURS)":  "P2 - Urgent",
        "P3 (48 HOURS)":  "P3 - 24 Hours",
        "PM":             "PM",
    }

    async def _notion_to_sc_sync():
        """
        Poll Notion every 10 min for user-edited pages.
        Pushes Status changes, Priority changes, and Notes to SC back to ServiceChannel.
        Skips pages that were last edited by our own SC→Notion sync (anti-loop).
        """
        # Use the later of (now-12min) or (last_sync+2min) as the query window start.
        # This prevents picking up changes our own sync just wrote.
        ignore_before = _last_sc_to_notion + timedelta(minutes=2)
        since_dt = datetime.now(timezone.utc) - timedelta(minutes=12)
        effective_since = max(since_dt, ignore_before)
        since_iso = effective_since.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            pages = await query_recently_edited_pages(since_iso)
        except Exception:
            logger.exception("Notion→SC: failed to query recent changes")
            return

        for entry in pages:
            wo_num          = entry["wo_num"]
            notion_status   = entry["status"].upper() if entry.get("status") else ""
            notion_priority = entry.get("priority", "").upper()
            note            = entry.get("note", "").strip()
            page_id         = entry.get("page_id", "")

            try:
                wo_id = int(wo_num.replace("WO-", "").strip())
            except ValueError:
                continue

            token = await get_valid_token()
            headers = {
                "Authorization":  f"Bearer {token}",
                "Accept":         "application/json",
                "Content-Type":   "application/json",
            }

            async with httpx.AsyncClient(timeout=15) as client:
                sc_resp = await client.get(f"{SC_API}/v3/workorders/{wo_id}", headers=headers)
                if not sc_resp.is_success:
                    logger.warning("Notion→SC: could not fetch WO #%s from SC (%s)", wo_num, sc_resp.status_code)
                    continue
                sc_wo = sc_resp.json()

                sc_primary  = (sc_wo.get("Status") or {}).get("Primary", "").upper()
                sc_extended = (sc_wo.get("Status") or {}).get("Extended", "").upper()
                sc_combined = f"{sc_primary}:{sc_extended}" if sc_extended else sc_primary
                sc_priority = str(sc_wo.get("Priority") or "").strip()

                # ── 1. Status change ─────────────────────────────────────────
                sc_map = _NOTION_TO_SC.get(notion_status) if notion_status else None
                if sc_map and sc_combined != notion_status:
                    payload: dict = {"Status": {"Primary": sc_map["primary"]}}
                    if sc_map.get("extended"):
                        payload["Status"]["Extended"] = sc_map["extended"]
                    if note:
                        payload["Note"] = note

                    upd = await client.put(
                        f"{SC_API}/v3/workorders/{wo_id}/status", headers=headers, json=payload
                    )
                    if upd.is_success:
                        logger.info("Notion→SC: WO #%s status %s → %s", wo_num, sc_combined, notion_status)
                    else:
                        logger.error("Notion→SC: status update failed WO #%s: %s", wo_num, upd.text[:200])

                    # Note was included in the status payload — don't double-send
                    note = ""

                # ── 2. Priority change ───────────────────────────────────────
                sc_priority_target = _NOTION_PRIORITY_TO_SC.get(notion_priority, "") if notion_priority else ""
                if sc_priority_target and not sc_priority.upper().startswith(notion_priority[:2]):
                    upd = await client.put(
                        f"{SC_API}/v3/workorders/{wo_id}/priority",
                        headers=headers,
                        json={"Priority": sc_priority_target},
                    )
                    if upd.is_success:
                        logger.info("Notion→SC: WO #%s priority → %s", wo_num, sc_priority_target)
                    else:
                        # Fallback: some SC versions embed priority in the general WO update
                        upd2 = await client.put(
                            f"{SC_API}/v3/workorders/{wo_id}",
                            headers=headers,
                            json={"Priority": sc_priority_target},
                        )
                        if upd2.is_success:
                            logger.info("Notion→SC: WO #%s priority → %s (via general update)", wo_num, sc_priority_target)
                        else:
                            logger.warning("Notion→SC: priority update not supported for WO #%s", wo_num)

                # ── 3. Notes to SC ───────────────────────────────────────────
                if note:
                    note_resp = await client.post(
                        f"{SC_API}/v3/workorders/{wo_id}/notes",
                        headers=headers,
                        json={"Note": note},
                    )
                    if note_resp.is_success:
                        logger.info("Notion→SC: note pushed for WO #%s", wo_num)
                        if page_id:
                            await clear_notes_to_sc(page_id)
                    else:
                        logger.error("Notion→SC: note push failed WO #%s: %s", wo_num, note_resp.text[:200])

    scheduler = AsyncIOScheduler(timezone=tz)

    # 8 AM — full Notion sync (upsert all active WOs, archive completed ones)
    scheduler.add_job(
        _notion_daily_sync,
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="notion_daily_sync",
    )
    # 9 AM — stale WO check → Teams alert + Notion refresh
    scheduler.add_job(
        _stale_check_and_sync,
        CronTrigger(hour=9, minute=0, timezone=tz),
        args=["Morning Check (9 AM)"],
        id="morning_check",
    )
    # 3 PM — stale WO check → Teams alert + Notion refresh
    scheduler.add_job(
        _stale_check_and_sync,
        CronTrigger(hour=15, minute=0, timezone=tz),
        args=["Afternoon Check (3 PM)"],
        id="afternoon_check",
    )
    # Every 10 min — pick up status changes made manually in Notion and push to SC
    scheduler.add_job(
        _notion_to_sc_sync,
        IntervalTrigger(minutes=10),
        id="notion_to_sc",
    )
    scheduler.start()
    logger.info(
        "Scheduler started — Notion sync 8 AM, Teams checks 9 AM & 3 PM, Notion→SC every 10 min (%s)",
        NOTIFY_TZ,
    )

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
    x_api_secret: Optional[str] = Header(default=None),
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
async def get_token(x_api_secret: Optional[str] = Header(default=None)):
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
async def force_refresh(x_api_secret: Optional[str] = Header(default=None)):
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
    x_api_secret: Optional[str] = Header(default=None),
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
async def check_now(x_api_secret: Optional[str] = Header(default=None)):
    require_auth(x_api_secret)
    count = await run_stale_check("Manual Check", raise_errors=True)
    return {
        "status": "sent" if count >= 0 else "error",
        "stale_work_orders_found": count,
    }


@app.post("/notify/test-webhook", summary="Send a sample Teams card to verify webhook connectivity", tags=["Notifications"])
async def test_webhook(x_api_secret: Optional[str] = Header(default=None)):
    require_auth(x_api_secret)
    sample = [
        {
            "id":                "12345678",
            "number":            "WO-12345678",
            "store":             "Store #042 — Downtown",
            "address":           "123 Main St, Chicago, IL 60601",
            "trade":             "HVAC",
            "priority":          "P2 - Urgent",
            "status":            "IN PROGRESS",
            "status_ext":        "PENDING PARTS",
            "provider":          "ACME HVAC Services",
            "description":       "A/C unit not cooling — reported by store manager. Vendor dispatched but parts on order.",
            "scheduled_date":    "2026-05-20",
            "days_old":          72,
            "days_since_update": 18,
        }
    ]
    ok, http_status, body = await send_teams_notification(sample, "Webhook Test")
    return {
        "status":              "ok" if ok else "failed",
        "webhook_configured":  bool(os.getenv("TEAMS_WEBHOOK_URL")),
        "teams_http_status":   http_status,
        "teams_response_body": body,
    }


# ── Notion sync endpoints ────────────────────────────────────────────────────

@app.post("/notion/sync", summary="Sync all active WOs from ServiceChannel into Notion, grouped by Trade", tags=["Notion"])
async def notion_sync(x_api_secret: Optional[str] = Header(default=None)):
    require_auth(x_api_secret)
    wos = await fetch_all_active_workorders()
    result = await sync_workorders(wos)
    return {"total_fetched": len(wos), **result}


@app.post("/notion/sync-wo/{wo_id}", summary="Sync a single WO into Notion by its ServiceChannel ID", tags=["Notion"])
async def notion_sync_one(wo_id: int, x_api_secret: Optional[str] = Header(default=None)):
    require_auth(x_api_secret)
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{os.getenv('SC_API_BASE', 'https://api.servicechannel.com')}/v3/odata/workorders({wo_id})",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if not resp.is_success:
        raise HTTPException(resp.status_code, f"SC API error: {resp.text}")
    raw = resp.json()
    status   = raw.get("Status")   or {}
    location = raw.get("Location") or raw.get("Store") or {}
    provider = raw.get("Provider") or {}
    priority_raw = raw.get("Priority") or ""
    priority = priority_raw.get("Name") if isinstance(priority_raw, dict) else str(priority_raw).strip()
    address_parts = [str(location.get(f, "")).strip() for f in ["Address", "City", "State", "ZipCode"] if location.get(f)]
    wo = {
        "id":           raw.get("Id"),
        "number":       raw.get("Number") or raw.get("Id"),
        "store":        location.get("Name") or str(location.get("StoreId", "")),
        "address":      ", ".join(address_parts),
        "trade":        raw.get("Trade", ""),
        "priority":     priority,
        "status":       status.get("Primary", ""),
        "status_ext":   status.get("Extended", ""),
        "provider":     provider.get("Name") or "Unassigned",
        "description":  (raw.get("Description") or "")[:200],
        "call_date":    (raw.get("CallDate") or "")[:10],
        "scheduled_date": (raw.get("ScheduledDate") or "")[:10],
        "nte":          raw.get("Nte") or raw.get("NTE"),
    }
    ok = await upsert_workorder(wo)
    return {"status": "synced" if ok else "failed", "wo_number": wo["number"]}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
async def health():
    tokens = load_tokens()
    has_token = bool(tokens.get("access_token"))
    age = round(time.time() - tokens["_obtained_at"]) if has_token else None
    return {"status": "ok", "authenticated": has_token, "token_age_seconds": age}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
