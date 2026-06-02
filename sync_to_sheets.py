import os, json, re, time, requests, gspread, pytz
from datetime import datetime, date
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
METABASE_BASE_URL = os.environ["METABASE_BASE_URL"]
METABASE_USERNAME = os.environ["METABASE_USERNAME"]
METABASE_PASSWORD = os.environ["METABASE_PASSWORD"]
GCP_JSON          = os.environ["GCP_JSON"]
SHEET_ID          = os.environ["SHEET_ID"]

# Dynamic MTD dates
_today     = date.today()
DATE_FROM  = _today.replace(day=1).strftime("%Y-%m-%d")
DATE_TO    = _today.strftime("%Y-%m-%d")

# MasterClass split logic
_mid = _today.replace(day=16)
MC_PART1_FROM = DATE_FROM
MC_PART1_TO   = _today.replace(day=16).strftime("%Y-%m-%d")
RUN_PART2     = _today.day >= 16
MC_PART2_FROM = _mid.strftime("%Y-%m-%d") if RUN_PART2 else None
MC_PART2_TO   = DATE_TO

QUESTIONS = [
    {"id": 10742, "tab": "Overall Funnel"},
    {"id": 10508, "tab": "Perf Funnel"},
    {"id": 10574, "tab": "Organic Funnel"},
    {"id": 10433, "tab": "Referral Funnel"},
    {"id": 10744, "tab": "Reactivation Funnel"},
    {"id": 10745, "tab": "Manual Assignment"},
]

MASTERCLASS = {"tab": "MasterClass Funnel"}

# Tabs that have final_source in col O (index 14, 0-based)
# These need protected write — col O must NOT be overwritten
PROTECTED_TABS = {"Overall Funnel", "Overall Funnel - May 2026"}
FINAL_SOURCE_COL_INDEX = 14   # col O = index 14 (0-based), after prospect_id

# ─────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────
def get_metabase_token():
    resp = requests.post(
        f"{METABASE_BASE_URL}/api/session",
        json={"username": METABASE_USERNAME, "password": METABASE_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]

# ─────────────────────────────────────────────────────────────────
# QUERY
# ─────────────────────────────────────────────────────────────────
def run_question(token, question_id, date_from, date_to, max_retries=3):
    headers = {"X-Metabase-Session": token, "Content-Type": "application/json"}
    params  = {
        "parameters": json.dumps([
            {"type": "date/single", "target": ["variable", ["template-tag", "start_date"]], "value": date_from},
            {"type": "date/single", "target": ["variable", ["template-tag", "end_date"]],   "value": date_to},
        ])
    }
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                f"{METABASE_BASE_URL}/api/card/{question_id}/query/json",
                headers=headers, params=params, timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return [], []
            cols = list(data[0].keys())
            rows = [[row.get(c) for c in cols] for row in data]
            return cols, rows
        except Exception as e:
            if attempt < max_retries:
                wait = 30 * attempt
                print(f"  Attempt {attempt} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise

# ─────────────────────────────────────────────────────────────────
# WRITE — STANDARD (clears and rewrites fully)
# ─────────────────────────────────────────────────────────────────
def write_to_sheet(gc, sheet_id, tab_name, cols, rows, timestamp):
    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=2000, cols=30)

    all_rows = []
    all_rows.append([f"Last updated: {timestamp}"] + [""] * (len(cols) - 1))
    all_rows.append([""] * len(cols))
    all_rows.append(cols)
    for row in rows:
        all_rows.append([str(c) if c is not None else "" for c in row])

    ws.clear()
    ws.update(all_rows, "A1", value_input_option="USER_ENTERED")
    print(f"  Written {len(rows)} data rows to '{tab_name}'.")

# ─────────────────────────────────────────────────────────────────
# WRITE — PROTECTED (preserves col O = final_source)
# ─────────────────────────────────────────────────────────────────
def write_to_sheet_protected(gc, sheet_id, tab_name, cols, rows, timestamp):
    """
    Same as write_to_sheet but:
      1. Reads existing col O (final_source) keyed by prospect_id (col A)
      2. Clears + rewrites cols A–N from Metabase
      3. Restores final_source values back into col O by matching prospect_id
    """
    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=2000, cols=30)

    # ── Step 1: snapshot existing final_source keyed by prospect_id ──
    print(f"  [{tab_name}] Reading existing final_source values...")
    existing = ws.get_all_values()   # list of lists
    final_source_map = {}            # {prospect_id: final_source_value}

    # Header row is row index 2 (0-based: row 0=timestamp, 1=blank, 2=headers)
    if len(existing) > 2:
        header_row = existing[2]
        # Find prospect_id col index
        try:
            pid_idx = header_row.index("prospect_id")
        except ValueError:
            pid_idx = 0   # fallback: assume col A
        # final_source is col O = index 14
        fs_idx = FINAL_SOURCE_COL_INDEX
        for data_row in existing[3:]:  # data starts row 3
            if len(data_row) > pid_idx:
                pid = data_row[pid_idx].strip()
                fs_val = data_row[fs_idx].strip() if len(data_row) > fs_idx else ""
                if pid:
                    final_source_map[pid] = fs_val

    print(f"  [{tab_name}] Saved {len(final_source_map)} final_source values.")

    # ── Step 2: build new rows from Metabase (cols A–N only) ──
    all_rows = []
    all_rows.append([f"Last updated: {timestamp}"] + [""] * (len(cols) - 1))
    all_rows.append([""] * len(cols))
    all_rows.append(cols)
    for row in rows:
        all_rows.append([str(c) if c is not None else "" for c in row])

    ws.clear()
    ws.update(all_rows, "A1", value_input_option="USER_ENTERED")
    print(f"  [{tab_name}] Written {len(rows)} Metabase rows.")

    # ── Step 3: restore final_source back into col O ──
    if not final_source_map:
        print(f"  [{tab_name}] No existing final_source to restore — skipping.")
        return

    # Re-read to get the new row layout
    new_data = ws.get_all_values()
    if len(new_data) <= 3:
        return

    new_headers = new_data[2]
    try:
        pid_idx = new_headers.index("prospect_id")
    except ValueError:
        pid_idx = 0

    # Write final_source header in O3
    ws.update_cell(3, FINAL_SOURCE_COL_INDEX + 1, "final_source")  # 1-indexed

    # Build list of (row_number_1indexed, value) to batch update
    updates = []
    for row_i, data_row in enumerate(new_data[3:], start=4):  # 1-indexed, data from row 4
        if len(data_row) > pid_idx:
            pid = data_row[pid_idx].strip()
            fs_val = final_source_map.get(pid, "")
            updates.append({
                "range": f"O{row_i}",
                "values": [[fs_val]]
            })

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        restored = sum(1 for u in updates if u["values"][0][0])
        print(f"  [{tab_name}] Restored final_source for {restored}/{len(updates)} rows.")

# ─────────────────────────────────────────────────────────────────
# MASTERCLASS SPLIT
# ─────────────────────────────────────────────────────────────────
def run_masterclass_split(token, timestamp):
    print(f"  MasterClass Part 1: {MC_PART1_FROM} → {MC_PART1_TO}")
    cols1, rows1 = run_question(token, 10750, MC_PART1_FROM, MC_PART1_TO)

    if not RUN_PART2:
        print(f"  MasterClass Part 2: skipped (before day 16)")
        return cols1, rows1

    print(f"  MasterClass Part 2: {MC_PART2_FROM} → {MC_PART2_TO}")
    cols2, rows2 = run_question(token, 10750, MC_PART2_FROM, MC_PART2_TO)

    # Dedupe by prospect_id (col index 0)
    seen = set()
    combined = []
    for row in rows1 + rows2:
        pid = row[0]
        if pid not in seen:
            seen.add(pid)
            combined.append(row)
    print(f"  MasterClass combined: {len(combined)} unique rows")
    return cols1, combined

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
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
            # Use protected write for Overall Funnel, standard for all others
            if q["tab"] in PROTECTED_TABS:
                write_to_sheet_protected(gc, SHEET_ID, q["tab"], cols, rows, timestamp)
            else:
                write_to_sheet(gc, SHEET_ID, q["tab"], cols, rows, timestamp)
            print(f"  Done.\n")
        except Exception as e:
            print(f"  ERROR: {e}\n")
            raise

    # ── MasterClass ─────────────────────────────────────────────
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
