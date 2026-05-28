import os
import json
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
ist = pytz.timezone("Asia/Kolkata")
now_ist = datetime.now(ist)
DATE_FROM = now_ist.strftime("%Y-%m-01")
DATE_TO   = now_ist.strftime("%Y-%m-%d")

# ─────────────────────────────────────────────
# QUERIES — question ID, tab name, Metabase slug
# ─────────────────────────────────────────────
QUESTIONS = [
    {
        "id":       10574,
        "slug":     "organic-funnel-bg-base-query",
        "tab":      "Organic",
    },
    {
        "id":       10508,
        "slug":     "perf-funnel-bg",
        "tab":      "Perf",
    },
    {
        "id":       10433,
        "slug":     "referral-base-logic",
        "tab":      "Referral",
    },
    {
        "id":       10744,
        "slug":     "reactivation-funnel-logic-bg-main",
        "tab":      "Reactivation",
    },
    {
        "id":       10750,
        "slug":     "masterclass-funnel-logic-bg",
        "tab":      "MasterClass",
    },
    {
        "id":       10745,
        "slug":     "manual-assigned-funnel-logic-bg",
        "tab":      "Manual Assignment",
    },
]

# ─────────────────────────────────────────────
# STEP 1 — Get Metabase session token
# ─────────────────────────────────────────────
def get_metabase_token():
    print("Authenticating with Metabase...")
    resp = requests.post(
        f"{METABASE_URL}/api/session",
        json={"username": METABASE_USERNAME, "password": METABASE_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["id"]
    print("  Auth successful.")
    return token


# ─────────────────────────────────────────────
# STEP 2 — Run a Metabase question and get rows
# ─────────────────────────────────────────────
def run_question(token, question_id, date_from, date_to):
    """
    Calls the Metabase /api/card/:id/query endpoint with date parameters.
    Returns (headers_list, rows_list).
    """
    url     = f"{METABASE_URL}/api/card/{question_id}/query"
    headers = {"X-Metabase-Session": token, "Content-Type": "application/json"}

    # Pass date filters as template tag parameters
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

    print(f"  Running question {question_id} ({date_from} → {date_to})...")
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()

    data = resp.json()
    cols = [col["display_name"] for col in data["data"]["cols"]]
    rows = data["data"]["rows"]
    print(f"  Got {len(rows)} rows, {len(cols)} columns.")
    return cols, rows


# ─────────────────────────────────────────────
# STEP 3 — Write results to a Google Sheet tab
# ─────────────────────────────────────────────
def write_to_sheet(gc, sheet_id, tab_name, cols, rows, timestamp_str):
    sh = gc.open_by_key(sheet_id)

    # Get or create the worksheet
    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=5000, cols=26)
        print(f"  Created new tab: '{tab_name}'")

    # ── Clear columns A:T (columns 1-20) ──────────────────────
    # We clear the full sheet first, then write fresh data
    ws.batch_clear(["A:T"])
    print(f"  Cleared columns A:T on tab '{tab_name}'.")

    # ── Build the full payload to write at once ────────────────
    all_rows = []

    # Row 1 — timestamp label + value
    all_rows.append([f"Last updated: {timestamp_str}"] + [""] * (len(cols) - 1))

    # Row 2 — blank separator
    all_rows.append([""] * len(cols))

    # Row 3 — column headers
    all_rows.append(cols)

    # Rows 4+ — data
    for row in rows:
        # Convert any non-serialisable types to string
        cleaned = [str(cell) if cell is not None else "" for cell in row]
        all_rows.append(cleaned)

    # Write everything in a single API call starting at A1
    ws.update("A1", all_rows, value_input_option="USER_ENTERED")
    print(f"  Written {len(rows)} data rows to tab '{tab_name}'.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    # Current IST timestamp for the "Last updated" label
    ist       = pytz.timezone("Asia/Kolkata")
    now_ist   = datetime.now(ist)
    timestamp = now_ist.strftime("%d %b %Y, %I:%M %p IST")   # e.g. 28 May 2026, 10:45 AM IST

    print(f"\n=== Charter-wise MECE Funnel Sync ===")
    print(f"Date range : {DATE_FROM} → {DATE_TO}")
    print(f"Timestamp  : {timestamp}\n")

    # ── Metabase auth ──────────────────────────────────────────
    token = get_metabase_token()

    # ── Google Sheets auth ─────────────────────────────────────
    print("Authenticating with Google Sheets...")
    creds_dict = json.loads(GCP_JSON)
    scopes     = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc    = gspread.authorize(creds)
    print("  Google auth successful.\n")

    # ── Process each question ──────────────────────────────────
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
