import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz

# ─────────────────────────────────────────────
# CONFIG — all values come from GitHub Secrets
# ─────────────────────────────────────────────
METABASE_URL      = os.environ["METABASE_URL"].rstrip("/")
METABASE_USERNAME = os.environ["METABASE_USERNAME"]
METABASE_PASSWORD = os.environ["METABASE_PASSWORD"]
SHEET_ID          = os.environ["GOOGLE_SHEET_ID"]
GCP_JSON          = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

# ─────────────────────────────────────────────
# DATE RANGE — always current month
# ─────────────────────────────────────────────
ist       = pytz.timezone("Asia/Kolkata")
now_ist   = datetime.now(ist)
DATE_FROM = now_ist.strftime("%Y-%m-01")
DATE_TO   = now_ist.strftime("%Y-%m-%d")

# ─────────────────────────────────────────────
# QUERIES
# ─────────────────────────────────────────────
QUESTIONS = [
    {"id": 10742, "tab": "Overall Funnel BG"},
    {"id": 10574, "tab": "Organic"},
    {"id": 10508, "tab": "Perf"},
    {"id": 10433, "tab": "Referral"},
    {"id": 10744, "tab": "Reactivation"},
    {"id": 10750, "tab": "MasterClass"},
    {"id": 10745, "tab": "Manual Assignment"},
]

# ─────────────────────────────────────────────
# STEP 1 — Metabase session token
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
# STEP 2 — Run question with retry on timeout
# ─────────────────────────────────────────────
def run_question(token, question_id, date_from, date_to, max_retries=3):
    url     = f"{METABASE_URL}/api/card/{question_id}/query"
    headers = {"X-Metabase-Session": token, "Content-Type": "application/json"}
    payload = {
        "parameters": [
            {
                "type":   "date/single",
                "target": ["variable", ["template-tag", "date_from"]],
                "value":  date_from,
            },
            {
                "type":   "date/single",
                "target": ["variable", ["template-tag", "date_to"]],
                "value":  date_to,
            },
        ]
    }

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  Running question {question_id} (attempt {attempt}/{max_retries})...")
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=300,          # 5 minutes — handles heavy queries
            )
            resp.raise_for_status()
            data = resp.json()
            cols = [col["display_name"] for col in data["data"]["cols"]]
            rows = data["data"]["rows"]
            print(f"  Got {len(rows)} rows, {len(cols)} columns.")
            return cols, rows

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait = 30 * attempt   # 30s, then 60s
                print(f"  Timed out. Waiting {wait}s before retry...")
                time.sleep(wait)
            else:
                print(f"  All {max_retries} attempts timed out. Giving up.")
                raise

        except requests.exceptions.HTTPError as e:
            print(f"  HTTP error: {e}")
            raise


# ─────────────────────────────────────────────
# STEP 3 — Write to Google Sheet tab
# ─────────────────────────────────────────────
def write_to_sheet(gc, sheet_id, tab_name, cols, rows, timestamp_str):
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=5000, cols=26)
        print(f"  Created new tab: '{tab_name}'")

    # Clear columns A:T
    ws.batch_clear(["A:T"])
    print(f"  Cleared A:T on tab '{tab_name}'.")

    # Build rows: timestamp → blank → headers → data
    all_rows = []
    all_rows.append([f"Last updated: {timestamp_str}"] + [""] * (len(cols) - 1))
    all_rows.append([""] * len(cols))
    all_rows.append(cols)
    for row in rows:
        all_rows.append([str(cell) if cell is not None else "" for cell in row])

    # FIX: values first, range_name second (gspread v6+)
    ws.update(all_rows, "A1", value_input_option="USER_ENTERED")
    print(f"  Written {len(rows)} data rows to '{tab_name}'.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    ist       = pytz.timezone("Asia/Kolkata")
    now_ist   = datetime.now(ist)
    timestamp = now_ist.strftime("%d %b %Y, %I:%M %p IST")

    print(f"\n=== Charter-wise MECE Funnel Sync ===")
    print(f"Date range : {DATE_FROM} → {DATE_TO}")
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

    for q in QUESTIONS:
        print(f"[{q['tab']}] Question {q['id']}")
        try:
            cols, rows = run_question(token, q["id"], DATE_FROM, DATE_TO)
            write_to_sheet(gc, SHEET_ID, q["tab"], cols, rows, timestamp)
            print(f"  Done.\n")
        except Exception as e:
            print(f"  ERROR on question {q['id']}: {e}\n")
            raise

    print("=== All tabs updated successfully ===\n")


if __name__ == "__main__":
    main()
