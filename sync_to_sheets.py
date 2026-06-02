import os, json, time, requests, gspread, pytz
from datetime import datetime, date
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────
# CONFIG — matches your GitHub secrets exactly
# ─────────────────────────────────────────────────────────────────
METABASE_URL      = os.environ["METABASE_URL"].rstrip("/")
METABASE_USERNAME = os.environ["METABASE_USERNAME"]
METABASE_PASSWORD = os.environ["METABASE_PASSWORD"]
SHEET_ID          = os.environ["GOOGLE_SHEET_ID"]
GCP_JSON          = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

# ─────────────────────────────────────────────────────────────────
# DYNAMIC MTD DATES
# ─────────────────────────────────────────────────────────────────
_today    = date.today()
DATE_FROM = _today.replace(day=1).strftime("%Y-%m-%d")
DATE_TO   = _today.strftime("%Y-%m-%d")

# MasterClass split
_mid      = _today.replace(day=16)
MC_PART1_FROM = DATE_FROM
MC_PART1_TO   = _mid.strftime("%Y-%m-%d")
RUN_PART2     = _today.day >= 16
MC_PART2_FROM = _mid.strftime("%Y-%m-%d") if RUN_PART2 else None
MC_PART2_TO   = DATE_TO

# ─────────────────────────────────────────────────────────────────
# QUESTIONS
# ─────────────────────────────────────────────────────────────────
QUESTIONS = [
    {"id": 10742, "tab": "Overall Funnel"},
    {"id": 10508, "tab": "Perf Funnel"},
    {"id": 10574, "tab": "Organic Funnel"},
    {"id": 10433, "tab": "Referral Funnel"},
    {"id": 10744, "tab": "Reactivation Funnel"},
    {"id": 10745, "tab": "Manual Assignment"},
]
MASTERCLASS = {"tab": "MasterClass Funnel"}

# Tabs where col O (final_source) must be preserved across syncs
PROTECTED_TABS        = {"Overall Funnel", "Overall Funnel - May 2026"}
FINAL_SOURCE_COL_IDX  = 14   # col O, 0-based

# ─────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────
# QUERY — async job polling (same method that worked before)
# ─────────────────────────────────────────────────────────────────
def run_question(token, question_id, date_from, date_to):
    headers = {
        "X-Metabase-Session": token,
        "Content-Type": "application/json",
    }
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

    print(f"  Triggering question {question_id}...")
    resp = requests.post(
        f"{METABASE_URL}/api/card/{question_id}/query",
        headers=headers,
        json=payload,
        timeout=600,
    )
    resp.raise_for_status()
    data = resp.json()

    # Fast/cached queries return data immediately
    if "data" in data:
        print(f"  Query returned immediately.")
        cols = [col["display_name"] for col in data["data"]["cols"]]
        rows = data["data"]["rows"]
        print(f"  Got {len(rows)} rows.")
        return cols, rows

    # Async path — poll until complete
    job_id = data.get("id") or data.get("job_id")
    if not job_id:
        raise ValueError(f"Unexpected Metabase response for question {question_id}: {data}")

    print(f"  Job started (id={job_id}). Polling...")
    poll_url   = f"{METABASE_URL}/api/async/job/{job_id}"
    result_url = f"{METABASE_URL}/api/async/job/{job_id}/results"
    max_wait   = 600
    waited     = 0
    interval   = 10

    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        status_resp = requests.get(poll_url, headers=headers, timeout=30)
        status_resp.raise_for_status()
        status = status_resp.json().get("status", "unknown")
        print(f"  [{waited}s] Status: {status}")
        if status == "complete":
            break
        elif status in ("failed", "error"):
            raise RuntimeError(f"Metabase job {job_id} failed")
    else:
        raise TimeoutError(f"Query {question_id} did not finish within {max_wait}s")

    result_resp = requests.get(result_url, headers=headers, timeout=60)
    result_resp.raise_for_status()
    result_data = result_resp.json()
    cols = [col["display_name"] for col in result_data["data"]["cols"]]
    rows = result_data["data"]["rows"]
    print(f"  Got {len(rows)} rows.")
    return cols, rows

# ─────────────────────────────────────────────────────────────────
# WRITE — STANDARD
# ─────────────────────────────────────────────────────────────────
def write_to_sheet(gc, sheet_id, tab_name, cols, rows, timestamp):
    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=5000, cols=30)

    ws.batch_clear(["A:T"])

    all_rows = []
    if cols:
        all_rows.append([f"Last updated: {timestamp}"] + [""] * (len(cols) - 1))
        all_rows.append([""] * len(cols))
        all_rows.append(cols)
        for row in rows:
            all_rows.append([str(cell) if cell is not None else "" for cell in row])
    else:
        all_rows.append([f"Last updated: {timestamp} — No data returned"])

    ws.update(all_rows, "A1", value_input_option="USER_ENTERED")
    print(f"  Written {len(rows)} rows to '{tab_name}'.")

# ─────────────────────────────────────────────────────────────────
# WRITE — PROTECTED (preserves col O = final_source)
# ─────────────────────────────────────────────────────────────────
def write_to_sheet_protected(gc, sheet_id, tab_name, cols, rows, timestamp):
    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=5000, cols=30)

    # Step 1 — snapshot existing final_source keyed by prospect_id
    print(f"  [{tab_name}] Reading existing final_source values...")
    existing = ws.get_all_values()
    final_source_map = {}

    if len(existing) > 2:
        header_row = existing[2]
        try:
            pid_idx = header_row.index("prospect_id")
        except ValueError:
            pid_idx = 0
        fs_idx = FINAL_SOURCE_COL_IDX
        for data_row in existing[3:]:
            if len(data_row) > pid_idx:
                pid    = data_row[pid_idx].strip()
                fs_val = data_row[fs_idx].strip() if len(data_row) > fs_idx else ""
                if pid:
                    final_source_map[pid] = fs_val

    print(f"  [{tab_name}] Saved {len(final_source_map)} final_source values.")

    # Step 2 — rewrite Metabase cols A–N
    ws.batch_clear(["A:T"])

    all_rows = []
    if cols:
        all_rows.append([f"Last updated: {timestamp}"] + [""] * (len(cols) - 1))
        all_rows.append([""] * len(cols))
        all_rows.append(cols)
        for row in rows:
            all_rows.append([str(cell) if cell is not None else "" for cell in row])
    else:
        all_rows.append([f"Last updated: {timestamp} — No data returned"])

    ws.update(all_rows, "A1", value_input_option="USER_ENTERED")
    print(f"  [{tab_name}] Written {len(rows)} Metabase rows.")

    # Step 3 — restore final_source back into col O
    if not final_source_map:
        print(f"  [{tab_name}] No existing final_source to restore.")
        return

    new_data = ws.get_all_values()
    if len(new_data) <= 3:
        return

    new_headers = new_data[2]
    try:
        pid_idx = new_headers.index("prospect_id")
    except ValueError:
        pid_idx = 0

    # Write header
    ws.update_cell(3, FINAL_SOURCE_COL_IDX + 1, "final_source")

    # Batch restore values
    updates = []
    for row_i, data_row in enumerate(new_data[3:], start=4):
        if len(data_row) > pid_idx:
            pid    = data_row[pid_idx].strip()
            fs_val = final_source_map.get(pid, "")
            updates.append({"range": f"O{row_i}", "values": [[fs_val]]})

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

    seen, combined = set(), []
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
    creds = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    gc = gspread.authorize(creds)
    print("  Google auth successful.\n")

    for q in QUESTIONS:
        print(f"[{q['tab']}] Question {q['id']}")
        try:
            cols, rows = run_question(token, q["id"], DATE_FROM, DATE_TO)
            if q["tab"] in PROTECTED_TABS:
                write_to_sheet_protected(gc, SHEET_ID, q["tab"], cols, rows, timestamp)
            else:
                write_to_sheet(gc, SHEET_ID, q["tab"], cols, rows, timestamp)
            print(f"  Done.\n")
        except Exception as e:
            print(f"  ERROR: {e}\n")
            raise

    try:
        print(f"[MasterClass Funnel]")
        cols, rows = run_masterclass_split(token, timestamp)
        write_to_sheet(gc, SHEET_ID, MASTERCLASS["tab"], cols, rows, timestamp)
        print(f"  Done.\n")
    except Exception as e:
        print(f"  ERROR on MasterClass: {e}\n")
        raise

    print(f"=== All tabs updated successfully ===\n")


if __name__ == "__main__":
    main()
