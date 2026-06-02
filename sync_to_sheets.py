import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date
import calendar
import pytz

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
METABASE_URL      = os.environ["METABASE_URL"].rstrip("/")
METABASE_USERNAME = os.environ["METABASE_USERNAME"]
METABASE_PASSWORD = os.environ["METABASE_PASSWORD"]
SHEET_ID          = os.environ["GOOGLE_SHEET_ID"]
GCP_JSON          = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

# ─────────────────────────────────────────────
# MTD DATE RANGE — always June 1 → today
# ─────────────────────────────────────────────
_today     = date.today()
DATE_FROM  = _today.replace(day=1).strftime("%Y-%m-%d")   # e.g. 2026-06-01
DATE_TO    = _today.strftime("%Y-%m-%d")                   # e.g. 2026-06-14  (today, exclusive upper bound in query)

# MasterClass split — day 1-15 and day 16-end
SPLIT_DAY  = 16
_last_day  = calendar.monthrange(_today.year, _today.month)[1]

MC_PART1_FROM = DATE_FROM                                              # June 1
MC_PART1_TO   = _today.replace(day=SPLIT_DAY).strftime("%Y-%m-%d")    # June 16

# Part 2 only runs if today >= split day
MC_PART2_FROM = MC_PART1_TO                                            # June 16
MC_PART2_TO   = DATE_TO                                                # today

RUN_PART2 = _today.day >= SPLIT_DAY   # False on June 1-15, True from June 16 onwards

# ─────────────────────────────────────────────
# QUERIES — MasterClass handled separately
# ─────────────────────────────────────────────
QUESTIONS = [
    {"id": 10742, "tab": "Overall Funnel"},
    {"id": 10574, "tab": "Organic"},
    {"id": 10508, "tab": "Perf"},
    {"id": 10433, "tab": "Referral"},
    {"id": 10744, "tab": "Reactivation"},
    {"id": 10745, "tab": "Manual Assignment"},
]

MASTERCLASS = {"id": 10750, "tab": "MasterClass"}

# ─────────────────────────────────────────────
# STEP 1 — Metabase auth
# ─────────────────────────────────────────────
def get_metabase_token():
    print("Authenticating with Metabase...")
    resp = requests.post(
        f"{METABASE_URL}/api/session",
        json={"username": METABASE_USERNAME, "password": METABASE_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    print("  Auth successful.")
    return resp.json()["id"]


# ─────────────────────────────────────────────
# STEP 2 — Run one question with date params
# ─────────────────────────────────────────────
def run_question(token, question_id, date_from, date_to, max_retries=3):
    url     = f"{METABASE_URL}/api/card/{question_id}/query/json"
    headers = {
        "X-Metabase-Session": token,
        "Content-Type": "application/json",
    }
    payload = {
        "parameters": [
            {"type": "category", "target": ["variable", ["template-tag", "date_from"]], "value": date_from},
            {"type": "category", "target": ["variable", ["template-tag", "date_to"]],   "value": date_to},
        ]
    }

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  Running Q{question_id} [{date_from} → {date_to}] (attempt {attempt}/{max_retries})...")
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                if len(data) == 0:
                    print(f"  Got 0 rows.")
                    return [], []
                cols = list(data[0].keys())
                rows = [list(row.values()) for row in data]
                print(f"  Got {len(rows)} rows.")
                return cols, rows

            if "data" in data:
                cols = [col["display_name"] for col in data["data"]["cols"]]
                rows = data["data"]["rows"]
                print(f"  Got {len(rows)} rows.")
                return cols, rows

            raise ValueError(f"Unexpected response format: {str(data)[:200]}")

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait = 30 * attempt
                print(f"  Timed out. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"  All {max_retries} attempts timed out.")
                raise

        except requests.exceptions.HTTPError as e:
            print(f"  HTTP error: {e}")
            raise


# ─────────────────────────────────────────────
# STEP 3 — MasterClass split fetch + merge
# ─────────────────────────────────────────────
def run_masterclass_split(token, timestamp_str):
    print(f"\n[MasterClass] Split mode")
    print(f"  Part 1: {MC_PART1_FROM} → {MC_PART1_TO}")

    cols, rows1 = run_question(token, MASTERCLASS["id"], MC_PART1_FROM, MC_PART1_TO)

    rows2 = []
    if RUN_PART2:
        print(f"  Part 2: {MC_PART2_FROM} → {MC_PART2_TO}")
        print(f"  Pausing 10s before Part 2...")
        time.sleep(10)
        _, rows2 = run_question(token, MASTERCLASS["id"], MC_PART2_FROM, MC_PART2_TO)
    else:
        print(f"  Today is day {_today.day} — before split day {SPLIT_DAY}, skipping Part 2.")

    # Deduplicate on prospect_id (col index 0) — safety net for boundary overlap
    seen   = set()
    merged = []
    for row in rows1 + rows2:
        key = row[0]  # prospect_id is first column
        if key not in seen:
            seen.add(key)
            merged.append(row)

    print(f"  Part 1: {len(rows1)} rows | Part 2: {len(rows2)} rows | Merged: {len(merged)} rows")
    return cols, merged


# ─────────────────────────────────────────────
# STEP 4 — Write to Google Sheet tab
# ─────────────────────────────────────────────
def write_to_sheet(gc, sheet_id, tab_name, cols, rows, timestamp_str):
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=5000, cols=26)
        print(f"  Created new tab: '{tab_name}'")

    ws.batch_clear(["A:T"])

    all_rows = []
    if cols:
        all_rows.append([f"Last updated: {timestamp_str}"] + [""] * (len(cols) - 1))
        all_rows.append([""] * len(cols))
        all_rows.append(cols)
        for row in rows:
            all_rows.append([str(cell) if cell is not None else "" for cell in row])
    else:
        all_rows.append([f"Last updated: {timestamp_str} — No data returned"])

    ws.update(all_rows, "A1", value_input_option="USER_ENTERED")
    print(f"  Written {len(rows)} rows to '{tab_name}'.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    ist       = pytz.timezone("Asia/Kolkata")
    now_ist   = datetime.now(ist)
    timestamp = now_ist.strftime("%d %b %Y, %I:%M %p IST")

    print(f"\n=== Charter-wise MECE Funnel Sync — MTD ===")
    print(f"MTD range  : {DATE_FROM} → {DATE_TO}")
    print(f"MC Part 1  : {MC_PART1_FROM} → {MC_PART1_TO}")
    print(f"MC Part 2  : {MC_PART2_FROM} → {MC_PART2_TO} (runs: {RUN_PART2})")
    print(f"Timestamp  : {timestamp}\n")

    token = get_metabase_token()

    print("Authenticating with Google Sheets...")
    creds_dict = json.loads(GCP_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc    = gspread.authorize(creds)
    print("  Google auth successful.\n")

    # ── Normal queries ──────────────────────────────────────────
    for q in QUESTIONS:
        print(f"[{q['tab']}] Question {q['id']}")
        try:
            cols, rows = run_question(token, q["id"], DATE_FROM, DATE_TO)
            write_to_sheet(gc, SHEET_ID, q["tab"], cols, rows, timestamp)
            print(f"  Done.\n")
        except Exception as e:
            print(f"  ERROR: {e}\n")
            raise

    # ── MasterClass split ───────────────────────────────────────
    try:
        cols, rows = run_masterclass_split(token, timestamp)
        write_to_sheet(gc, SHEET_ID, MASTERCLASS["tab"], cols, rows, timestamp)
        print(f"  Done.\n")
    except Exception as e:
        print(f"  ERROR on MasterClass: {e}\n")
        raise

    print(f"=== All tabs updated successfully ===\n")


if __name__ == "__main__":
    main()
