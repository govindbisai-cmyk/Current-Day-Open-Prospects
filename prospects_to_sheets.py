"""
prospects_to_sheets.py
Fetches current-day prospect queries from Metabase and writes each to a
separate tab in a Google Sheet. Clears only columns A:T before writing.
Timestamp is shown in IST.

Environment variables (set as GitHub Actions secrets):
  METABASE_URL                 e.g. https://metabase.yourcompany.co
  METABASE_USERNAME            your login email
  METABASE_PASSWORD            your login password
  GOOGLE_SHEET_ID              the long ID from your sheet URL
  GCP_SERVICE_ACCOUNT_JSON     full contents of your service account JSON
"""

import os
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

METABASE_URL      = os.environ["METABASE_URL"].rstrip("/")
METABASE_USERNAME = os.environ["METABASE_USERNAME"]
METABASE_PASSWORD = os.environ["METABASE_PASSWORD"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GCP_SA_JSON       = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

IST = timezone(timedelta(hours=5, minutes=30))

# ── Queries ───────────────────────────────────────────────────────────────────

QUERIES = [
    ("Current_day_open_prospects", "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10594-current-day-open-prospects"),
    ("Overall Perf",               "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10617-overall-perf-open-prospects"),
    ("Overall Inbound",            "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10616-overall-inbound-open-prospects"),
    ("Perf Inbound",               "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10618-perf-inbound-open-prospects"),
    ("Organic Inbound",            "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10619-organic-inbound-open-prospects"),
    ("Masterclass",                "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10620-masterclass-open-prospects"),
    ("Referral",                   "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10621-referral-open-prospects"),
    ("Open Funnel",                "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10622-open-funnel-open-prospects"),
    ("Reapplied",                  "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10625-reapplied-open-prospects"),
    ("Reactivation",               "https://metabase-lierhfgoeiwhr.newtonschool.co/question/10624-reactivation-open-prospects"),
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def metabase_session_token() -> str:
    resp = requests.post(
        f"{METABASE_URL}/api/session",
        json={"username": METABASE_USERNAME, "password": METABASE_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def extract_card_id(url: str) -> int:
    match = re.search(r"/(?:question|card)/(\d+)", url)
    if not match:
        raise ValueError(f"Cannot extract card ID from URL: {url}")
    return int(match.group(1))


def fetch_card_data(card_id: int, session_token: str) -> tuple[list[str], list[list]]:
    headers = {"X-Metabase-Session": session_token}

    # Try JSON first
    resp = requests.post(
        f"{METABASE_URL}/api/card/{card_id}/query/json",
        headers=headers,
        timeout=300,
    )

    if resp.status_code == 200:
        try:
            data = resp.json()
            if isinstance(data, list) and data:
                cols = list(data[0].keys())
                rows = [[str(row.get(c, "")) for c in cols] for row in data]
                return cols, rows
            return [], []
        except Exception:
            pass  # fall through to CSV

    # Fallback: CSV export
    resp = requests.post(
        f"{METABASE_URL}/api/card/{card_id}/query/csv",
        headers=headers,
        timeout=300,
    )
    resp.raise_for_status()

    import csv, io
    reader = csv.reader(io.StringIO(resp.text))
    rows_raw = list(reader)
    if not rows_raw:
        return [], []
    return rows_raw[0], rows_raw[1:]


def sheets_service():
    creds_info = json.loads(GCP_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def ensure_sheet_tab(service, spreadsheet_id: str, tab_name: str):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == tab_name:
            return
    # Create tab if it doesn't exist
    body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body
    ).execute()


def write_tab(service, spreadsheet_id: str, tab_name: str, cols: list, rows: list):
    # Clear only columns A:T (preserves any custom columns beyond T)
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A:T",
    ).execute()

    if not cols:
        print(f"  ⚠️  No data returned — tab '{tab_name}' cleared.")
        return

    # Timestamp in IST
    ist_now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    timestamp_row = [f"Last updated: {ist_now}"]
    values = [timestamp_row, cols] + rows

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()

    print(f"  ✅  '{tab_name}' → {len(rows)} rows written.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("🔐 Authenticating with Metabase...")
    token = metabase_session_token()
    print("✅ Metabase session OK")

    print("🔐 Building Google Sheets client...")
    svc = sheets_service()
    print("✅ Sheets client OK")

    errors = []
    for tab_name, url in QUERIES:
        try:
            card_id = extract_card_id(url)
            print(f"\n📊 Fetching card {card_id} → '{tab_name}'")
            cols, rows = fetch_card_data(card_id, token)
            ensure_sheet_tab(svc, GOOGLE_SHEET_ID, tab_name)
            write_tab(svc, GOOGLE_SHEET_ID, tab_name, cols, rows)
            time.sleep(0.5)
        except Exception as e:
            msg = f"❌ '{tab_name}' (card {url}): {e}"
            print(msg)
            errors.append(msg)

    if errors:
        print("\n\n⚠️  Completed with errors:")
        for e in errors:
            print(" ", e)
        sys.exit(1)
    else:
        print("\n\n🎉 All queries written successfully.")


if __name__ == "__main__":
    main()
