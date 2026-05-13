"""
Stale work order detector + Microsoft Teams notifier.

A WO is considered "stale" when ALL of these are true:
  - Status is OPEN or IN PROGRESS
  - CallDate (creation) is older than STALE_DAYS  (default 60)
  - UpdatedDate (last activity) is older than FOLLOWUP_DAYS  (default 14)
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from tokens import get_valid_token

logger = logging.getLogger(__name__)

SC_API         = os.getenv("SC_API_BASE", "https://api.servicechannel.com")
TEAMS_WEBHOOK  = os.getenv("TEAMS_WEBHOOK_URL", "")
STALE_DAYS     = int(os.getenv("STALE_DAYS",    "60"))
FOLLOWUP_DAYS  = int(os.getenv("FOLLOWUP_DAYS", "14"))


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
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)

    if not resp.is_success:
        logger.error("SC API error fetching stale WOs: %s %s", resp.status_code, resp.text)
        return []

    stale = []
    for wo in resp.json().get("value", []):
        status   = wo.get("Status")   or {}
        location = wo.get("Location") or wo.get("Store") or {}
        provider = wo.get("Provider") or {}

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
            "number":            wo.get("Number"),
            "store":             location.get("Name") or str(location.get("StoreId", "Unknown")),
            "trade":             wo.get("Trade", ""),
            "status":            status.get("Primary", ""),
            "status_ext":        status.get("Extended", ""),
            "provider":          provider.get("Name") or "Unassigned",
            "description":       (wo.get("Description") or "")[:150],
            "scheduled_date":    (wo.get("ScheduledDate") or "")[:10],
            "days_old":          days_old,
            "days_since_update": days_since_update,
        })

    return stale


# ── Teams message card ────────────────────────────────────────────────────────

def _build_teams_card(wos: list[dict], run_label: str) -> dict:
    if not wos:
        return {
            "@type":    "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": "00B050",
            "summary": "All WOs are up to date",
            "title":   f"✅ {run_label} — All Work Orders Have Recent Follow-Ups",
            "text": (
                f"No open work orders older than **{STALE_DAYS} days** "
                f"are missing an update in the last **{FOLLOWUP_DAYS} days**."
            ),
        }

    sections = []
    for wo in wos:
        status_display = wo["status"]
        if wo["status_ext"]:
            status_display += f" / {wo['status_ext']}"

        # Flag severity: red if no update > 30 days, orange otherwise
        age_note = (
            "🔴 URGENT — no update in over 30 days"
            if wo["days_since_update"] > 30
            else f"🟠 {wo['days_since_update']} days since last update"
        )

        sections.append({
            "activityTitle":    f"**WO #{wo['id']}** — {wo['store']}",
            "activitySubtitle": (
                f"{wo['trade']}  ·  "
                f"Open **{wo['days_old']} days**  ·  "
                f"{age_note}"
            ),
            "facts": [
                {"name": "Status",      "value": status_display},
                {"name": "Provider",    "value": wo["provider"]},
                {"name": "Scheduled",   "value": wo["scheduled_date"] or "Not set"},
                {"name": "Description", "value": wo["description"] or "(none)"},
            ],
        })

    return {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "FF6B35",
        "summary": f"{len(wos)} work orders need follow-up",
        "title":   f"⚠️ {run_label} — {len(wos)} Work Orders Need Follow-Up",
        "text": (
            f"These work orders are **older than {STALE_DAYS} days** "
            f"and have had **no activity in the last {FOLLOWUP_DAYS} days**. "
            f"Please call the Store Manager, Landlord, or Vendor to get an update."
        ),
        "sections": sections,
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

    if resp.is_success:
        logger.info("Teams notification sent — %d stale WOs", len(wos))
        return True

    logger.error("Teams notification failed: %s %s", resp.status_code, resp.text)
    return False


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_stale_check(run_label: str = "Scheduled Check") -> int:
    """Find stale WOs, send Teams alert. Returns count found (-1 on error)."""
    logger.info("Starting stale WO check: %s", run_label)
    try:
        wos = await find_stale_workorders()
        await send_teams_notification(wos, run_label)
        logger.info("Stale WO check done: %d found", len(wos))
        return len(wos)
    except Exception:
        logger.exception("Stale WO check failed")
        return -1
