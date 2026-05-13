#!/usr/bin/env python3
"""
Daily Work Order CLI — run this every morning to review and act on open WOs.

Usage:
    python daily_check.py                    # show dashboard
    python daily_check.py --wo 12345678      # inspect one WO
    python daily_check.py --complete 12345678 --note "Vendor confirmed fix"
    python daily_check.py --status 12345678 "IN PROGRESS" --note "Called vendor"
    python daily_check.py --add-note 12345678 "Left VM for store manager"
    python daily_check.py --create           # interactive new WO wizard
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL   = os.getenv("SC_BACKEND_URL", "https://sc-oauth-backend-production.up.railway.app")
API_SECRET = os.getenv("API_SECRET", "")

HEADERS = {"X-Api-Secret": API_SECRET, "Content-Type": "application/json"}


def _req(method: str, path: str, **kwargs):
    url = f"{BASE_URL}{path}"
    resp = httpx.request(method, url, headers=HEADERS, timeout=30, **kwargs)
    if not resp.is_success:
        print(f"[ERROR] {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json() if resp.content else {}


def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _status_color(status: str) -> str:
    colors = {
        "OPEN":        "93",   # yellow
        "IN PROGRESS": "94",   # blue
        "ON HOLD":     "91",   # red
        "COMPLETED":   "92",   # green
    }
    return _color(status, colors.get(status, "97"))


def show_dashboard():
    data = _req("GET", "/workorders/dashboard")
    print()
    print(_color(f"  ServiceChannel — Daily Work Order Dashboard", "1;97"))
    print(_color(f"  {data['as_of']}  |  Total active: {data['total']}  |  Overdue: {data['overdue']}", "90"))
    print()

    for status, bucket in data["by_status"].items():
        wos = bucket["work_orders"]
        print(_color(f"  {_status_color(status)}  ({bucket['count']} WOs)", "1"))
        print(_color("  " + "─" * 80, "90"))
        for wo in wos:
            sched = wo["scheduled_date"][:10] if wo["scheduled_date"] else "no date"
            ext   = f" / {wo['status_extended']}" if wo.get("status_extended") else ""
            print(
                f"  [{_color(str(wo['id']), '1;97')}]  "
                f"{_color(wo['store'] or '(no store)', '96')}  "
                f"{_color(wo['trade'] or '', '95')}  "
                f"sched:{sched}  "
                f"provider:{wo['provider'] or '(unassigned)'}"
            )
            if wo["description"]:
                print(f"    {_color(wo['description'][:100], '90')}")
            print()
        print()

    if data["overdue_list"]:
        print(_color("  ⚠  OVERDUE WORK ORDERS", "1;91"))
        for wo in data["overdue_list"]:
            print(f"    [{wo['id']}] {wo['store']} — {wo['description'][:80]}")
        print()


def show_wo(wo_id: int):
    wo = _req("GET", f"/workorders/{wo_id}")
    print()
    print(_color(f"  WO #{wo_id}", "1;97"))
    for k, v in wo.items():
        if v is not None:
            print(f"  {_color(k, '90'):30} {v}")
    print()


def update_status(wo_id: int, primary: str, extended: Optional[str], note: Optional[str]):
    payload = {"primary": primary}
    if extended:
        payload["extended"] = extended
    if note:
        payload["note"] = note
    _req("PUT", f"/workorders/{wo_id}/status", json=payload)
    print(_color(f"  WO {wo_id} → status set to {primary.upper()}", "92"))


def add_note(wo_id: int, text: str):
    _req("POST", f"/workorders/{wo_id}/note", json={"text": text})
    print(_color(f"  Note added to WO {wo_id}", "92"))


def complete_wo(wo_id: int, note: Optional[str]):
    params = f"?note={note}" if note else ""
    _req("POST", f"/workorders/{wo_id}/complete{params}")
    print(_color(f"  WO {wo_id} marked COMPLETED", "92"))


def create_wizard():
    print(_color("\n  Create New Work Order\n", "1;97"))
    store_id    = input("  Store ID (from ServiceChannel): ").strip()
    trade       = input("  Trade (e.g. HVAC, PLUMBING, ELECTRICAL): ").strip().upper()
    description = input("  Description of the issue: ").strip()
    priority    = input("  Priority [P3 - 24 Hours]: ").strip() or "P3 - 24 Hours"
    category    = input("  Category [MAINTENANCE]: ").strip() or "MAINTENANCE"
    provider_id = input("  Provider ID (leave blank if unassigned): ").strip() or None
    nte_raw     = input("  NTE budget in $ (leave blank to skip): ").strip()
    nte         = float(nte_raw) if nte_raw else None

    payload = {
        "store_id": store_id,
        "trade": trade,
        "description": description,
        "priority": priority,
        "category": category,
    }
    if provider_id:
        payload["provider_id"] = provider_id
    if nte:
        payload["nte"] = nte

    result = _req("POST", "/workorders", json=payload)
    print(_color(f"\n  Work Order created! ID: {result.get('work_order_id')}\n", "1;92"))


def main():
    parser = argparse.ArgumentParser(description="ServiceChannel daily WO automation")
    parser.add_argument("--wo",       type=int,   help="Show details for a work order ID")
    parser.add_argument("--status",   type=int,   help="Work order ID to update status")
    parser.add_argument("new_status", nargs="?",  help="New primary status (with --status)")
    parser.add_argument("--extended", type=str,   help="Extended status (optional, with --status)")
    parser.add_argument("--note",     type=str,   help="Note to attach (with --status or --complete)")
    parser.add_argument("--add-note", type=int,   dest="add_note", help="Add a note to this WO ID")
    parser.add_argument("--complete", type=int,   help="Mark this WO ID as COMPLETED")
    parser.add_argument("--create",   action="store_true", help="Interactive new WO wizard")
    args = parser.parse_args()

    if not API_SECRET:
        print("[ERROR] API_SECRET not set. Check your .env file.")
        sys.exit(1)

    if args.wo:
        show_wo(args.wo)
    elif args.status:
        if not args.new_status:
            print("[ERROR] Provide new status after --status <WO_ID>, e.g. --status 123 'IN PROGRESS'")
            sys.exit(1)
        update_status(args.status, args.new_status, args.extended, args.note)
    elif args.add_note:
        text = args.note or input("  Note text: ").strip()
        add_note(args.add_note, text)
    elif args.complete:
        complete_wo(args.complete, args.note)
    elif args.create:
        create_wizard()
    else:
        show_dashboard()


if __name__ == "__main__":
    main()
