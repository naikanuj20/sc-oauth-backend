"""Work order routes — list, get, create, update status, add notes."""
import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from tokens import bearer, get_valid_token, require_auth

SC_API = os.getenv("SC_API_BASE", "https://api.servicechannel.com")

router = APIRouter(prefix="/workorders", tags=["Work Orders"])


# ── Request models ────────────────────────────────────────────────────────────

class StatusUpdate(BaseModel):
    primary: str                        # e.g. COMPLETED, IN PROGRESS, ON HOLD
    extended: Optional[str] = None      # e.g. PENDING CONFIRMATION, ON SITE
    note: Optional[str] = None          # optional message logged with the change

class NoteCreate(BaseModel):
    text: str

class WorkOrderCreate(BaseModel):
    store_id: str                       # ServiceChannel StoreId for the location
    trade: str                          # e.g. HVAC, PLUMBING, ELECTRICAL, DOORS
    description: str
    priority: str = "P3 - 24 Hours"    # P1-P4 tiers
    category: str = "MAINTENANCE"       # MAINTENANCE, CAPEX, etc.
    provider_id: Optional[str] = None
    nte: Optional[float] = None         # not-to-exceed budget in dollars
    scheduled_date: Optional[str] = None  # ISO 8601 UTC, e.g. 2026-05-15T09:00:00Z


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _sc_get(path: str) -> dict:
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{SC_API}{path}", headers=bearer(token))
    if resp.status_code == 404:
        raise HTTPException(404, f"Not found: {path}")
    if not resp.is_success:
        raise HTTPException(resp.status_code, f"SC API error: {resp.text}")
    return resp.json()


async def _sc_post(path: str, payload: dict, expected: int = 201) -> dict:
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{SC_API}{path}", headers=bearer(token), json=payload)
    if resp.status_code not in (200, expected):
        raise HTTPException(resp.status_code, f"SC API error: {resp.text}")
    return resp.json() if resp.content else {}


async def _sc_put(path: str, payload: dict) -> None:
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(f"{SC_API}{path}", headers=bearer(token), json=payload)
    if resp.status_code not in (200, 204):
        raise HTTPException(resp.status_code, f"SC API error: {resp.text}")


def _fmt_wo(wo: dict) -> dict:
    """Flatten a raw WO object into a readable summary dict."""
    status = wo.get("Status") or {}
    location = wo.get("Location") or wo.get("Store") or {}
    provider = wo.get("Provider") or {}
    return {
        "id":              wo.get("Id"),
        "number":          wo.get("Number"),
        "store":           location.get("Name") or location.get("StoreId"),
        "store_id":        location.get("StoreId"),
        "trade":           wo.get("Trade"),
        "description":     (wo.get("Description") or "")[:200],
        "priority":        wo.get("Priority"),
        "category":        wo.get("Category"),
        "status":          status.get("Primary"),
        "status_extended": status.get("Extended"),
        "provider":        provider.get("Name"),
        "provider_id":     provider.get("Id"),
        "scheduled_date":  wo.get("ScheduledDate"),
        "completed_date":  wo.get("CompletedDate"),
        "created_date":    wo.get("CallDate") or wo.get("CreatedDate"),
        "nte":             wo.get("Nte"),
    }


# ── Daily dashboard ───────────────────────────────────────────────────────────

@router.get("/dashboard", summary="Daily digest — all open & in-progress WOs grouped by status")
async def daily_dashboard(x_api_secret: str | None = Header(default=None)):
    require_auth(x_api_secret)

    filter_q = "(Status/Primary eq 'OPEN' or Status/Primary eq 'IN PROGRESS' or Status/Primary eq 'ON HOLD')"
    data = await _sc_get(
        f"/v3/odata/workorders?$filter={filter_q}&$orderby=ScheduledDate asc&$top=200"
    )
    wos = data.get("value", [])

    grouped: dict[str, list] = {}
    for wo in wos:
        key = (wo.get("Status") or {}).get("Primary", "UNKNOWN")
        grouped.setdefault(key, []).append(_fmt_wo(wo))

    # overdue = scheduled in the past and still open
    now = datetime.utcnow().isoformat()
    overdue = [
        w for w in grouped.get("OPEN", [])
        if w["scheduled_date"] and w["scheduled_date"] < now
    ]

    return {
        "as_of":   datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "total":   len(wos),
        "overdue": len(overdue),
        "by_status": {k: {"count": len(v), "work_orders": v} for k, v in grouped.items()},
        "overdue_list": overdue,
    }


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", summary="List work orders — default status=OPEN")
async def list_work_orders(
    status: str = "OPEN",
    limit: int = 50,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)
    data = await _sc_get(
        f"/v3/odata/workorders"
        f"?$filter=Status/Primary eq '{status.upper()}'"
        f"&$orderby=ScheduledDate asc&$top={limit}"
    )
    wos = [_fmt_wo(w) for w in data.get("value", [])]
    return {"status_filter": status.upper(), "count": len(wos), "work_orders": wos}


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{wo_id}", summary="Full details of a single work order")
async def get_work_order(wo_id: int, x_api_secret: str | None = Header(default=None)):
    require_auth(x_api_secret)
    raw = await _sc_get(f"/v3/workorders/{wo_id}")
    return _fmt_wo(raw)


# ── Status update ─────────────────────────────────────────────────────────────

@router.put("/{wo_id}/status", summary="Change status — optionally attach a note")
async def update_status(
    wo_id: int,
    body: StatusUpdate,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)

    payload: dict = {"Status": {"Primary": body.primary.upper()}}
    if body.extended:
        payload["Status"]["Extended"] = body.extended.upper()
    if body.note:
        payload["Note"] = body.note

    await _sc_put(f"/v3/workorders/{wo_id}/status", payload)
    return {
        "success":    True,
        "wo_id":      wo_id,
        "new_status": body.primary.upper(),
        "note_added": bool(body.note),
    }


# ── Add note (no status change) ───────────────────────────────────────────────

@router.post("/{wo_id}/note", summary="Add a progress note without changing status")
async def add_note(
    wo_id: int,
    body: NoteCreate,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)
    token = await get_valid_token()

    async with httpx.AsyncClient(timeout=30) as client:
        # Try the dedicated notes endpoint
        resp = await client.post(
            f"{SC_API}/v3/workorders/{wo_id}/notes",
            headers=bearer(token),
            json={"Note": body.text},
        )
        if resp.status_code == 404:
            # Fallback: keep current status, attach note via status endpoint
            detail = await client.get(
                f"{SC_API}/v3/workorders/{wo_id}", headers=bearer(token)
            )
            if not detail.is_success:
                raise HTTPException(detail.status_code, "Could not fetch WO details")
            wo = detail.json()
            status = wo.get("Status") or {}
            payload: dict = {
                "Status": {"Primary": status.get("Primary", "OPEN")},
                "Note": body.text,
            }
            if status.get("Extended"):
                payload["Status"]["Extended"] = status["Extended"]
            resp = await client.put(
                f"{SC_API}/v3/workorders/{wo_id}/status",
                headers=bearer(token),
                json=payload,
            )

    if resp.status_code not in (200, 201, 204):
        raise HTTPException(resp.status_code, f"SC API error: {resp.text}")
    return {"success": True, "wo_id": wo_id, "note": body.text}


# ── Create work order ─────────────────────────────────────────────────────────

@router.post("", summary="Create a new service request / work order", status_code=201)
async def create_work_order(
    body: WorkOrderCreate,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)

    payload: dict = {
        "ContractInfo": {
            "StoreId":   body.store_id,
            "TradeName": body.trade,
        },
        "Category":    body.category,
        "Priority":    body.priority,
        "Description": body.description,
    }
    if body.provider_id:
        payload["ContractInfo"]["ProviderId"] = body.provider_id
    if body.nte is not None:
        payload["Nte"] = body.nte
    if body.scheduled_date:
        payload["ScheduledDate"] = body.scheduled_date

    result = await _sc_post("/v3/workorders", payload, expected=201)
    wo_id = result.get("id") or result
    return {"success": True, "work_order_id": wo_id}


# ── Mark complete (shortcut) ──────────────────────────────────────────────────

@router.post("/{wo_id}/complete", summary="Mark a work order as COMPLETED with an optional note")
async def complete_work_order(
    wo_id: int,
    note: Optional[str] = None,
    x_api_secret: str | None = Header(default=None),
):
    require_auth(x_api_secret)
    payload: dict = {
        "Status": {"Primary": "COMPLETED", "Extended": "PENDING CONFIRMATION"},
    }
    if note:
        payload["Note"] = note
    await _sc_put(f"/v3/workorders/{wo_id}/status", payload)
    return {"success": True, "wo_id": wo_id, "status": "COMPLETED"}
