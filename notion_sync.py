"""
ServiceChannel → Notion sync.
Upserts work orders into the Ascend Grocery tracker database.
Secure: NOTION_API_KEY is read from env only — never logged or returned in responses.
"""
import asyncio
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


async def _bulk_fetch_existing_pages() -> dict[str, str]:
    """Return {wo_number: page_id} for every non-archived page in the DB (one sweep)."""
    existing: dict[str, str] = {}
    has_more = True
    cursor = None
    async with httpx.AsyncClient(timeout=60) as client:
        while has_more:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            resp = await client.post(
                f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
                headers=_headers(),
                json=payload,
            )
            if not resp.is_success:
                logger.error("Notion bulk page fetch failed: %s %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            for page in data.get("results", []):
                if page.get("archived"):
                    continue
                title_arr = page.get("properties", {}).get("Work Order #", {}).get("title", [])
                wo_num = title_arr[0].get("plain_text", "") if title_arr else ""
                if wo_num:
                    existing[wo_num] = page["id"]
            has_more = data.get("has_more", False)
            cursor = data.get("next_cursor")
    logger.info("Notion bulk fetch: found %d existing pages", len(existing))
    return existing


async def upsert_workorder(wo: dict, existing: Optional[dict] = None) -> bool:
    """Create or update a single Notion page for this WO. Returns True on success."""
    if not NOTION_API_KEY:
        logger.warning("NOTION_API_KEY not configured — skipping Notion sync")
        return False

    wo_number = str(wo.get("number") or wo.get("id", ""))
    props = _build_properties(wo)

    async with httpx.AsyncClient(timeout=15) as client:
        page_id = (existing or {}).get(wo_number)
        if page_id is None:
            # Fall back to individual query only when no bulk map provided
            resp = await client.post(
                f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
                headers=_headers(),
                json={"filter": {"property": "Work Order #", "title": {"equals": wo_number}}, "page_size": 1},
            )
            if resp.is_success:
                results = resp.json().get("results", [])
                page_id = results[0]["id"] if results else None

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
    """Upsert active WOs to Notion then archive any that are no longer active."""
    if not NOTION_API_KEY:
        return {"skipped": True, "reason": "NOTION_API_KEY not set in Railway environment variables"}

    # One bulk query to find all existing pages — avoids N individual queries
    existing = await _bulk_fetch_existing_pages()

    ok = fail = 0
    for i, wo in enumerate(wos):
        if await upsert_workorder(wo, existing):
            ok += 1
        else:
            fail += 1
        # Notion rate limit: ~3 req/sec — pause briefly every 10 WOs
        if i > 0 and i % 10 == 0:
            await asyncio.sleep(0.5)

    logger.info("Notion batch sync complete: %d synced, %d failed", ok, fail)

    active_numbers = {str(wo.get("number") or wo.get("id", "")) for wo in wos}
    archived = await archive_completed_pages(active_numbers)
    return {"synced": ok, "failed": fail, "archived": archived}


async def archive_completed_pages(active_wo_numbers: set) -> int:
    """Archive Notion pages for WOs that are no longer open/in-progress in SC."""
    if not NOTION_API_KEY:
        return 0
    archived = 0
    has_more = True
    cursor = None
    async with httpx.AsyncClient(timeout=30) as client:
        while has_more:
            payload: dict = {"page_size": 100, "filter": {"property": "Status", "select": {"does_not_equal": "ARCHIVED"}}}
            if cursor:
                payload["start_cursor"] = cursor
            resp = await client.post(
                f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
                headers=_headers(),
                json=payload,
            )
            if not resp.is_success:
                logger.error("Notion query failed during archive sweep: %s", resp.text[:200])
                break
            data = resp.json()
            for page in data.get("results", []):
                if page.get("archived"):
                    continue
                props = page.get("properties", {})
                title_arr = props.get("Work Order #", {}).get("title", [])
                wo_num = title_arr[0].get("plain_text", "") if title_arr else ""
                if wo_num and wo_num not in active_wo_numbers:
                    patch = await client.patch(
                        f"{NOTION_API}/pages/{page['id']}",
                        headers=_headers(),
                        json={"archived": True},
                    )
                    if patch.is_success:
                        archived += 1
                        logger.info("Archived Notion page for completed WO #%s", wo_num)
            has_more = data.get("has_more", False)
            cursor = data.get("next_cursor")
    return archived


async def query_recently_edited_pages(since_iso: str) -> list[dict]:
    """Return pages in the tracker DB edited after `since_iso` (ISO timestamp)."""
    if not NOTION_API_KEY:
        return []
    results = []
    has_more = True
    cursor = None
    async with httpx.AsyncClient(timeout=30) as client:
        while has_more:
            payload: dict = {
                "page_size": 50,
                "filter": {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"after": since_iso},
                },
            }
            if cursor:
                payload["start_cursor"] = cursor
            resp = await client.post(
                f"{NOTION_API}/databases/{NOTION_DB_ID}/query",
                headers=_headers(),
                json=payload,
            )
            if not resp.is_success:
                break
            data = resp.json()
            for page in data.get("results", []):
                if page.get("archived"):
                    continue
                props = page.get("properties", {})
                title_arr = props.get("Work Order #", {}).get("title", [])
                wo_num = title_arr[0].get("plain_text", "") if title_arr else ""
                status_sel = props.get("Status", {}).get("select") or {}
                notion_status = status_sel.get("name", "")
                # Notes field (if user typed a note in Notion to push to SC)
                note_arr = props.get("Notes to SC", {}).get("rich_text", [])
                note = note_arr[0].get("plain_text", "") if note_arr else ""
                if wo_num and notion_status:
                    results.append({"wo_num": wo_num, "status": notion_status, "note": note})
            has_more = data.get("has_more", False)
            cursor = data.get("next_cursor")
    return results
