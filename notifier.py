"""
Stale work order detector + Microsoft Teams notifier.

A WO is considered "stale" when ALL of these are true:
  - Status is OPEN or IN PROGRESS
  - CallDate (creation) is older than STALE_DAYS  (default 60)
  - UpdatedDate (last activity) is older than FOLLOWUP_DAYS  (default 14)

Teams notifications use Adaptive Cards format, which is required for
Workflow-based webhooks (the current Teams standard).
"""
import logging
import os
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from tokens import get_valid_token

logger = logging.getLogger(__name__)

SC_API         = os.getenv("SC_API_BASE", "https://api.servicechannel.com")
TEAMS_WEBHOOK  = os.getenv("TEAMS_WEBHOOK_URL", "")
STALE_DAYS     = int(os.getenv("STALE_DAYS",    "60"))
FOLLOWUP_DAYS  = int(os.getenv("FOLLOWUP_DAYS", "14"))
SC_TIMEOUT     = float(os.getenv("SC_TIMEOUT_SECONDS", "90"))


# ── Stale WO detection ────────────────────────────────────────────────────────

async def find_stale_workorders() -> list[dict]:
    token = await get_valid_token()
    now   = datetime.now(timezone.utc)

    stale_cutoff    = (now - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    followup_cutoff = (now - timedelta(days=FOLLOWUP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_filter = (
        f"CallDate lt {stale_cutoff}"
        f" and UpdatedDate lt {followup_cutoff}"
        f" and (Status/Primary eq 'OPEN' or Status/Primary eq 'IN PROGRESS')"
    )
    url = (
        f"{SC_API}/v3/odata/workorders"
        f"?$filter={quote(raw_filter)}"
        f"&$orderby=CallDate asc"
        f"&$top=50"
    )

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    timeout = httpx.Timeout(SC_TIMEOUT, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(2):
            try:
                resp = await client.get(url, headers=headers)
                break
            except httpx.ReadTimeout:
                if attempt == 1:
                    logger.exception("SC API timed out fetching stale WOs after %.0f seconds", SC_TIMEOUT)
                    raise HTTPException(
                        504,
                        f"SC API timed out fetching stale WOs after {SC_TIMEOUT:.0f} seconds",
                    )
                logger.warning("SC API timed out fetching stale WOs; retrying once")
                await asyncio.sleep(2)

    if not resp.is_success:
        logger.error("SC API error fetching stale WOs: %s %s", resp.status_code, resp.text)
        raise HTTPException(
            resp.status_code,
            f"SC API error fetching stale WOs: {resp.text}",
        )

    stale = []
    for wo in resp.json().get("value", []):
        status   = wo.get("Status")   or {}
        location = wo.get("Location") or wo.get("Store") or {}
        provider = wo.get("Provider") or {}

        # Build a readable address from the location object
        address_parts = []
        for field in ["Address", "City", "State", "ZipCode", "Zip"]:
            val = location.get(field)
            if val:
                address_parts.append(str(val).strip())
        address = ", ".join(address_parts)

        # Priority can be a string or a nested object depending on SC API version
        priority_raw = wo.get("Priority") or ""
        if isinstance(priority_raw, dict):
            priority = priority_raw.get("Name") or priority_raw.get("Primary") or ""
        else:
            priority = str(priority_raw).strip()

        days_old = days_since_update = 0
        for field, attr in [("CallDate", "days_old"), ("UpdatedDate", "days_since_update")]:
            raw = wo.get(field, "")
            if raw:
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    val = (now - dt).days
                    if attr == "days_old":
                        days_old = val
                    else:
                        days_since_update = val
                except Exception:
                    pass

        stale.append({
            "id":                wo.get("Id"),
            "number":            wo.get("Number") or wo.get("Id"),
            "store":             location.get("Name") or str(location.get("StoreId", "Unknown")),
            "address":           address,
            "trade":             wo.get("Trade", ""),
            "priority":          priority,
            "status":            status.get("Primary", ""),
            "status_ext":        status.get("Extended", ""),
            "provider":          provider.get("Name") or "Unassigned",
            "description":       (wo.get("Description") or "")[:200],
            "scheduled_date":    (wo.get("ScheduledDate") or "")[:10],
            "days_old":          days_old,
            "days_since_update": days_since_update,
        })

    return stale


# ── Teams Adaptive Card builder ───────────────────────────────────────────────

def _build_teams_card(wos: list[dict], run_label: str) -> dict:
    """
    Builds a Teams message payload using Adaptive Cards format.
    This works with both new Workflow webhooks and legacy connectors.
    """
    if not wos:
        body = [
            {
                "type": "TextBlock",
                "size": "Large",
                "weight": "Bolder",
                "color": "Good",
                "text": f"✅ {run_label} — All Work Orders Up To Date",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"No open work orders older than **{STALE_DAYS} days** "
                    f"are missing an update in the last **{FOLLOWUP_DAYS} days**."
                ),
                "wrap": True,
                "isSubtle": True,
            },
        ]
    else:
        body = [
            {
                "type": "TextBlock",
                "size": "Large",
                "weight": "Bolder",
                "color": "Attention",
                "text": f"⚠️ {run_label} — {len(wos)} Work Order{'s' if len(wos) != 1 else ''} Need Follow-Up",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"Work orders open **{STALE_DAYS}+ days** with no activity "
                    f"in the last **{FOLLOWUP_DAYS} days**. "
                    f"Please contact the Store Manager, Landlord, or Vendor."
                ),
                "wrap": True,
                "isSubtle": True,
                "spacing": "None",
            },
        ]

        for wo in wos:
            status_display = wo["status"]
            if wo["status_ext"]:
                status_display += f" / {wo['status_ext']}"

            if wo["days_since_update"] > 30:
                urgency_icon = "🔴"
                urgency_text = f"No update in **{wo['days_since_update']} days** — URGENT"
            else:
                urgency_icon = "🟠"
                urgency_text = f"No update in **{wo['days_since_update']} days**"

            facts = [
                {"title": "WO #",      "value": str(wo["number"])},
                {"title": "Store",     "value": wo["store"]},
            ]
            if wo["address"]:
                facts.append({"title": "Address",  "value": wo["address"]})
            facts += [
                {"title": "Problem",   "value": wo["description"] or "(no description)"},
                {"title": "Priority",  "value": wo["priority"] or "Not set"},
                {"title": "Status",    "value": status_display},
                {"title": "Open",      "value": f"{wo['days_old']} days"},
                {"title": "Last Activity", "value": urgency_text},
                {"title": "Provider",  "value": wo["provider"]},
                {"title": "Scheduled", "value": wo["scheduled_date"] or "Not scheduled"},
            ]

            body += [
                {"type": "Separator"},
                {
                    "type": "TextBlock",
                    "weight": "Bolder",
                    "text": f"{urgency_icon} WO #{wo['number']} — {wo['store']}",
                    "wrap": True,
                    "spacing": "Medium",
                },
                {
                    "type": "FactSet",
                    "facts": facts,
                    "spacing": "Small",
                },
            ]

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": body,
                },
            }
        ],
    }


# ── Send to Teams ─────────────────────────────────────────────────────────────

async def send_teams_notification(wos: list[dict], run_label: str) -> bool:
    if not TEAMS_WEBHOOK:
        logger.warning("TEAMS_WEBHOOK_URL not set — skipping Teams notification")
        return False

    card = _build_teams_card(wos, run_label)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TEAMS_WEBHOOK,
            json=card,
            headers={"Content-Type": "application/json"},
        )

    logger.info(
        "Teams webhook response: status=%s body=%s",
        resp.status_code,
        resp.text[:300],
    )

    if resp.is_success:
        logger.info("Teams notification sent — %d stale WOs", len(wos))
        return True

    logger.error("Teams notification failed: %s %s", resp.status_code, resp.text)
    return False


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_stale_check(run_label: str = "Scheduled Check", raise_errors: bool = False) -> int:
    """Find stale WOs, send Teams alert. Returns count found (-1 on error)."""
    logger.info("Starting stale WO check: %s", run_label)
    try:
        wos = await find_stale_workorders()
        await send_teams_notification(wos, run_label)
        logger.info("Stale WO check done: %d found", len(wos))
        return len(wos)
    except Exception:
        logger.exception("Stale WO check failed")
        if raise_errors:
            raise
        return -1
