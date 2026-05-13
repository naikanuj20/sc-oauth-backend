"""
ServiceChannel → Notion sync.
Upserts work orders into the Ascend Grocery tracker database.
Secure: NOTION_API_KEY is read from env only — never logged or returned in responses.
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DB_ID   = os.getenv("NOTION_DATABASE_ID", "98790a991cfb442d8a92dfb6d2004e04")
NOTION_API     = "https://api.notion.com/v1"
NOTION_VER     = "2022-06-28"

# Valid select option values in the Notion database schema
_NOTION_TRADES = {
    "REFRIGERATION", "HVAC", "MEAT SAW", "LIGHTING", "FIRE PROTECTION",
    "AUTOMATIC DOORS", "SCALE", "PEST CONTROL", "PLUMBING", "SECURITY EQUIPMENT",
    "DOCK LIFT", "FLOOR SCRUBBER", "GENERAL MAINTENANCE - BUILDING",
    "EQUIPMENT REPAIR", "ROOF", "WRAPPER", "OTHER",
}

_NOTION_STATUSES = {
    "INVOICED:CONFIRMED", "COMPLETED:CONFIRMED", "COMPLETED", "COMPLETED:NO CHARGE",
    "IN PROGRESS:PARTS ON ORDER", "IN PROGRESS:INCOMPLETE",
    "IN PROGRESS:DISPATCH CONFIRMED", "IN PROGRESS:WAITING FOR QUOTE",
    "INVOICED", "OPEN",
}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def _map_trade(sc_trade: str) -> str:
    t = (sc_trade or "").upper().strip()
    if t in _NOTION_TRADES:
        return t
    if "DOOR" in t:
        return "AUTOMATIC DOORS"
    if "FIRE" in t or "SUPPRESSION" in t:
        return "FIRE PROTECTION"
    if "PEST" in t or "EXTERMINATOR" in t:
        return "PEST CONTROL"
    if "SECURITY" in t or "CAMERA" in t or "ALARM" in t:
        return "SECURITY EQUIPMENT"
    if "FLOOR" in t or "SCRUBBER" in t:
        return "FLOOR SCRUBBER"
    if "DOCK" in t:
        return "DOCK LIFT"
    if "WRAP" in t:
        return "WRAPPER"
    if "EQUIP" in t:
        return "EQUIPMENT REPAIR"
    if "ELECTRICAL" in t or "GENERAL" in t or "BUILDING" in t:
        return "GENERAL MAINTENANCE - BUILDING"
    return "OTHER"


def _map_priority(sc_priority: str) -> Optional[str]:
    p = (sc_priority or "").upper().strip()
    if p.startswith("P1"):
        return "P1 (2-4 HOURS)"
    if p.startswith("P2"):
        return "P2 (24 HOURS)"
    if p.startswith("P3") or p.startswith("P4"):
        return "P3 (48 HOURS)"
    if "PM" in p:
        return "PM"
    return None


def _map_status(primary: str, extended: str) -> str:
    p = (primary or "").upper().strip()
    e = (extended or "").upper().strip()
    candidate = f"{p}:{e}" if e else p
    if candidate in _NOTION_STATUSES:
        return candidate
    if p == "OPEN":
        return "OPEN"
    if p in ("COMPLETED", "COMPLETE"):
        if "CONFIRM" in e:
            return "COMPLETED:CONFIRMED"
        if "NO CHARGE" in e:
            return "COMPLETED:NO CHARGE"
        return "COMPLETED"
    if p == "IN PROGRESS":
        if "PARTS" in e or "ORDER" in e:
            return "IN PROGRESS:PARTS ON ORDER"
        if "INCOMPLETE" in e:
            return "IN PROGRESS:INCOMPLETE"
        if "DISPATCH" in e or "CONFIRMED" in e:
            return "IN PROGRESS:DISPATCH CONFIRMED"
        if "QUOTE" in e or "WAITING" in e:
            return "IN PROGRESS:WAITING FOR QUOTE"
        return "OPEN"
    if p == "INVOICED":
        if "CONFIRM" in e:
            return "INVOICED:CONFIRMED"
        return "INVOICED"
    return "OPEN"


def _text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": (value or "")[:2000]}}]}


def _build_properties(wo: dict) -> dict:
    wo_num = str(wo.get("number") or wo.get("id", ""))
    props: dict = {
        "Work Order #":      {"title": [{"text": {"content": wo_num}}]},
        "Location":          _text(wo.get("store", "")),
        "Problem Description": _text(wo.get("description", "")),
        "Provider":          _text(wo.get("provider", "")),
        "Category":          _text(wo.get("trade", "")),
        "Trade":             {"select": {"name": _map_trade(wo.get("trade", ""))}},
        "Status":            {"select": {"name": _map_status(wo.get("status", ""), wo.get("status_ext", ""))}},
    }

    priority = _map_priority(wo.get("priority", ""))
    if priority:
        props["Priority"] = {"select": {"name": priority}}

    # Parse "Street, City, ST ZIP" address string into City and State fields
    address = wo.get("address", "")
    if address:
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            props["City"] = _text(parts[-2])
        if len(parts) >= 1 and parts[-1].strip():
            state_token = parts[-1].strip().split()[0]
            props["State"] = _text(state_token)

    # Dates
    call_date = (wo.get("call_date") or "")[:10]
    if call_date and len(call_date) == 10:
        props["Call Date"] = {"date": {"start": call_date}}

    sched = (wo.get("scheduled_date") or "")[:10]
    if sched and len(sched) == 10:
        props["Scheduled Date"] = {"date": {"start": sched}}

    nte = wo.get("nte")
    if nte is not None:
        try:
            props["NTE"] = {"number": float(nte)}
        except (TypeError, ValueError):
            pass

    return props


async def _find_page_id(client: httpx.AsyncClient, wo_number: str) -> Optional[str]:
    resp = await client.post(
        f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
        headers=_headers(),
        json={
            "filter": {"property": "Work Order #", "title": {"equals": wo_number}},
            "page_size": 1,
        },
    )
    if resp.is_success:
        results = resp.json().get("results", [])
        if results:
            return results[0]["id"]
    return None


async def upsert_workorder(wo: dict) -> bool:
    """Create or update a single Notion page for this WO. Returns True on success."""
    if not NOTION_API_KEY:
        logger.warning("NOTION_API_KEY not configured — skipping Notion sync")
        return False

    wo_number = str(wo.get("number") or wo.get("id", ""))
    props = _build_properties(wo)

    async with httpx.AsyncClient(timeout=15) as client:
        page_id = await _find_page_id(client, wo_number)
        if page_id:
            resp = await client.patch(
                f"{NOTION_API}/pages/{page_id}",
                headers=_headers(),
                json={"properties": props},
            )
            action = "updated"
        else:
            resp = await client.post(
                f"{NOTION_API}/pages",
                headers=_headers(),
                json={"parent": {"database_id": NOTION_DB_ID}, "properties": props},
            )
            action = "created"

    if resp.is_success:
        logger.info("Notion %s WO #%s (trade: %s)", action, wo_number, wo.get("trade", "?"))
        return True

    logger.error("Notion upsert failed WO #%s: %s %s", wo_number, resp.status_code, resp.text[:300])
    return False


async def sync_workorders(wos: list[dict]) -> dict:
    """Upsert a batch of WO dicts to Notion. Returns {synced, failed}."""
    if not NOTION_API_KEY:
        return {"skipped": True, "reason": "NOTION_API_KEY not configured"}
    ok = fail = 0
    for wo in wos:
        if await upsert_workorder(wo):
            ok += 1
        else:
            fail += 1
    logger.info("Notion batch sync complete: %d synced, %d failed", ok, fail)
    return {"synced": ok, "failed": fail}
