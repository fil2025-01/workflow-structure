"""Pick Jira tickets, have Claude break them down, and write them to the Weekly List sheet.

Usage:
    vcp-workflow                  pick from your sprint tickets
    vcp-workflow GEEK-123 ...     skip the picker
    vcp-workflow --dry-run        print the rows instead of writing them
    vcp-workflow --yes            write without the review step
    vcp-workflow --new-week       snapshot Weekly List and roll B2 to this week
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import google.auth
import gspread

CONFIG_PATH = Path(os.environ.get("VCP_WORKFLOW_CONFIG", Path.home() / ".config/vcp/workflow.json"))
JIRA_URL = "https://geekee.atlassian.net/browse/"
PHASES = [
    "Investigation & Analysis",
    "Implementation & Logic",
    "Testing & Quality Assurance",
    "Deployment & Documentation",
]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SPRINT_JQL = "assignee=currentUser() and sprint in openSprints()"
FALLBACK_JQL = "assignee=currentUser()"

SCHEMA = {
    "type": "object",
    "required": ["phases"],
    "properties": {
        "phases": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {
                "type": "object",
                "required": ["name", "tasks"],
                "properties": {
                    "name": {"type": "string", "enum": PHASES},
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["text", "points"],
                            "properties": {
                                "text": {"type": "string"},
                                "points": {"type": "integer", "enum": [1, 2]},
                            },
                        },
                    },
                },
            },
        }
    },
}

PROMPT = """Break down Jira ticket {key} into a work plan for my weekly sheet.

1. Run `jira issue view {key} --raw` and read the ticket.
2. Look through the codebase in this directory for real context: the files, components, \
endpoints, models and tests the change touches. V2 work is in value-chain-v2 (Nuxt frontend) \
and value-chain-api-v2 (Laravel API). V1 work is in value-chain and value-chain-api.
3. Return the breakdown as structured output: exactly these four phases, in this order:
   {phases}

Rules:
- Each task is one short line in plain English, naming the real thing it touches \
(a component, endpoint, enum, test file). No generic filler like "Implement core logic".
- 2–5 tasks per phase. points is 1 (an hour or two) or 2 (half a day or more).
- Don't put the points in the text; they are added separately.
- Deployment & Documentation is usually: create PR(s) naming the repos, deploy to staging \
for end-to-end testing, deploy to production and monitor.
- Do not edit any files.

Example of the style (GEEK-11800):
Investigation & Analysis
- Review the 31 notification types and agree on the category grouping (1)
- Analyze the preferences API response and the settings screen component (1)
Implementation & Logic
- Add NotificationCategory enum and NotificationType::category() mapping (1)
- Group preference rows under category headers in the settings screen (2)
Testing & Quality Assurance
- Add API tests for category coverage and response shape (1)
- Happy path and regression testing of saving preferences on desktop and mobile (1)
Deployment & Documentation
- Create PR(s) for the changes (value-chain-api-v2, value-chain-v2) (1)
- Deploy to staging for end-to-end testing (1)
- Deploy to production and monitor (1)
"""


def fail(msg):
    sys.exit(f"error: {msg}")


def load_config():
    """Per-user settings, so each person writes to their own sheet."""
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        fail(f'no config at {CONFIG_PATH}; create it with '
            '{"spreadsheet_id": "...", "tab": "Weekly List", "section": 7}')
    except json.JSONDecodeError as e:
        fail(f"invalid JSON in {CONFIG_PATH}: {e}")
    if not config.get("spreadsheet_id"):
        fail(f"spreadsheet_id is missing in {CONFIG_PATH}")
    return config["spreadsheet_id"], config.get("tab", "Weekly List"), int(config.get("section", 7))


SPREADSHEET_ID, TAB, SECTION = load_config()


def jira_list(jql):
    out = subprocess.run(
        ["jira", "issue", "list", f"-q={jql}", "--plain", "--no-headers",
        "--columns", "key,status,summary"],
        capture_output=True, text=True,
    )
    # jira exits non-zero when the query has no results
    if out.returncode and "No result found" not in out.stderr + out.stdout:
        fail(f"jira issue list failed: {out.stderr.strip()}")
    issues = []
    for line in out.stdout.splitlines():
        parts = [p for p in line.split("\t") if p]
        if len(parts) >= 3:
            issues.append({"key": parts[0], "status": parts[1], "summary": parts[2]})
    return issues


def fetch_issues():
    issues = jira_list(SPRINT_JQL)
    if all(i["status"].lower() in ("release", "released") for i in issues):
        print("All sprint issues are released; showing your To Do issues instead.")
        issues = [i for i in jira_list(FALLBACK_JQL) if i["status"].lower() == "to do"]
    return issues


def pick(issues):
    if not issues:
        fail("no issues to pick from")

    width = max(len(i["status"]) for i in issues)
    lines = [f'{i["key"]}  {i["status"]:<{width}}  {i["summary"]}' for i in issues]
    out = subprocess.run(
        ["gum", "choose", "--no-limit", "--show-help",
        "--unselected-prefix", "[ ] ", "--selected-prefix", "[✓] ", "--cursor-prefix", "[ ] ",
        "--header", "Pick tickets (x to check, enter to confirm)"],
        input="\n".join(lines), stdout=subprocess.PIPE, text=True,
    )
    if out.returncode:
        sys.exit(out.returncode)
    keys = [line.split()[0] for line in out.stdout.splitlines() if line.strip()]
    if not keys:
        fail("no tickets selected; check one with x (or ctrl+a for all) before pressing enter")
    print(f"Selected: {', '.join(keys)}")
    return [i for i in issues if i["key"] in keys]


def clean_title(summary):
    """Drop leading tags like "[VCP-12933] [AV]", as the sheet titles do."""
    return re.sub(r"^(\s*\[[^\]]*\]\s*)+", "", summary).strip()


def validate(plan):
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(phases, list) or [p.get("name") for p in phases] != PHASES:
        return "phases must be exactly: " + ", ".join(PHASES)
    for p in phases:
        tasks = p.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return f'{p["name"]} has no tasks'
        for t in tasks:
            if not isinstance(t.get("text"), str) or not t["text"].strip():
                return f'{p["name"]} has a task with no text'
            if t.get("points") not in (1, 2):
                return f'{p["name"]}: points must be 1 or 2, got {t.get("points")!r}'
    return None


def analyze(issue):
    key = issue["key"]
    out = subprocess.run(
        ["claude", "-p", PROMPT.format(key=key, phases=", ".join(PHASES)),
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        "--allowedTools", f"Bash(jira issue view {key}:*)", "Read", "Grep", "Glob",
        "--disallowedTools", "Edit", "Write", "NotebookEdit"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=1800,
    )
    if out.returncode:
        return None, f"claude exited {out.returncode}: {(out.stderr or out.stdout).strip()[:500]}"
    try:
        result = json.loads(out.stdout)
        plan = result.get("structured_output")
        if plan is None:
            plan = json.loads(result.get("result", ""))
    except (json.JSONDecodeError, AttributeError) as e:
        return None, f"could not parse Claude's output: {e}"
    error = validate(plan)
    if error:
        return None, error
    return {"key": key, "title": clean_title(issue["summary"]), "phases": plan["phases"]}, None


def build_rows(entry, n):
    """Rows of (column A, column B, bold, link) for one ticket numbered 7.n."""
    top = f"{SECTION}.{n}"
    rows = [(top, f'{entry["key"]} {entry["title"]}', True, JIRA_URL + entry["key"])]
    for pi, phase in enumerate(entry["phases"], 1):
        rows.append((f"{top}.{pi}", phase["name"], True, None))
        for ti, task in enumerate(phase["tasks"], 1):
            rows.append((f"{top}.{pi}.{ti}", f'{task["text"].strip()} ({task["points"]})', False, None))
    return rows


def print_rows(rows):
    for a, b, bold, link in rows:
        text = f"\033[1m{b}\033[0m" if bold else b
        print(f"  {a:<12}{text}" + (f"  → {link}" if link else ""))


def review(entry):
    """Show the breakdown and ask what to do. Returns the entry to write, or None to skip."""
    while True:
        print(f'\n{entry["key"]} {entry["title"]}')
        print_rows(build_rows(entry, "N")[1:])
        choice = subprocess.run(
            ["gum", "choose", "Write", "Edit", "Skip", "--header", f'{entry["key"]}:'],
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        if choice == "Write":
            return entry
        if choice != "Edit":
            return None
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(entry, f, indent=2, ensure_ascii=False)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.run(f'{editor} "{f.name}"', shell=True)
        try:
            edited = json.loads(Path(f.name).read_text())
            error = validate(edited)
        except json.JSONDecodeError as e:
            error = f"invalid JSON: {e}"
        if error:
            print(f"Edit rejected ({error}); keeping the previous version.")
        else:
            entry = edited
        os.unlink(f.name)


def open_sheet():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def sheet_state(ws):
    """Return (last used row, last 7.N number) from columns A and B."""
    values = ws.get_values("A:B")
    last = 0
    for (a, *_) in (r for r in values if r):
        # top-level cells are stored as numbers (7.108 → "7.108"); sub-rows as text
        m = re.fullmatch(rf"{SECTION}\.(\d+)(\..*)?", a.strip())
        if m:
            last = max(last, int(m.group(1)))
    return len(values), last


def write_rows(sh, ws, rows, start_row):
    needed = start_row + len(rows) - 1
    if needed > ws.row_count:
        ws.add_rows(needed - ws.row_count)

    def cell(value, bold):
        fmt = {"textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": bold}}
        return {"userEnteredValue": value, "userEnteredFormat": fmt}

    data = []
    for a, b, bold, link in rows:
        if link:
            b_value = {"formulaValue": f'=HYPERLINK("{link}", "{b.replace(chr(34), chr(34) * 2)}")'}
        else:
            b_value = {"stringValue": b}
        # column A is text so 7.110 isn't shown as 7.11
        data.append({"values": [cell({"stringValue": a}, bold), cell(b_value, bold)]})
    sh.batch_update({"requests": [{"updateCells": {
        "start": {"sheetId": ws.id, "rowIndex": start_row - 1, "columnIndex": 0},
        "rows": data,
        "fields": "userEnteredValue,userEnteredFormat.textFormat",
    }}]})


def week_label(today):
    monday = today - dt.timedelta(days=today.weekday())
    if today.weekday() >= 5:
        monday += dt.timedelta(days=7)
    friday = monday + dt.timedelta(days=4)
    return f"Week ({monday:%m/%d/%y}-{friday:%m/%d/%y})"


def new_week(dry_run):
    sh = open_sheet()
    ws = sh.worksheet(TAB)
    current = ws.acell("B2").value
    label = week_label(dt.date.today())
    if current == label:
        print(f"B2 is already {label}; nothing to do.")
        return
    if current in [w.title for w in sh.worksheets()]:
        fail(f'a tab named "{current}" already exists; fix B2 or that tab first')
    if dry_run:
        print(f'Would copy {TAB} to "{current}" and set B2 to "{label}".')
        return
    ws.duplicate(insert_sheet_index=ws.index + 1, new_sheet_name=current)
    ws.update_acell("B2", label)
    print(f'Copied {TAB} to "{current}" and set B2 to "{label}".')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("keys", nargs="*", help="Jira keys; skips the picker")
    parser.add_argument("--dry-run", action="store_true", help="print rows instead of writing")
    parser.add_argument("--yes", action="store_true", help="skip the review step")
    parser.add_argument("--new-week", action="store_true", help="snapshot Weekly List and roll B2 forward")
    args = parser.parse_args()

    if args.new_week:
        new_week(args.dry_run)
        return

    if args.keys:
        keys = [k.upper() for k in args.keys]
        issues = jira_list(f"key in ({', '.join(keys)})")
        missing = set(keys) - {i["key"] for i in issues}
        if missing:
            fail(f"not found in Jira: {', '.join(sorted(missing))}")
        issues.sort(key=lambda i: keys.index(i["key"]))
    else:
        issues = pick(fetch_issues())
    if not issues:
        return

    print("debug: came in this line")

    sh = open_sheet()
    ws = sh.worksheet(TAB)

    print(f"Analyzing {', '.join(i['key'] for i in issues)} with Claude (this takes a few minutes)…")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(analyze, issues))

    entries = []
    for issue, (entry, error) in zip(issues, results):
        if error:
            print(f'{issue["key"]}: skipped, {error}')
        else:
            entries.append(entry)
    if not args.yes and not args.dry_run:
        entries = [e for e in (review(e) for e in entries) if e]

    last_row, last_n = sheet_state(ws)
    for entry in entries:
        last_n += 1
        rows = build_rows(entry, last_n)
        start_row = last_row + 2
        if args.dry_run:
            print(f"\nWould write rows {start_row}–{start_row + len(rows) - 1}:")
            print_rows(rows)
        else:
            write_rows(sh, ws, rows, start_row)
            print(f'Wrote {entry["key"]} as {SECTION}.{last_n} (rows {start_row}–{start_row + len(rows) - 1}).')
        last_row = start_row + len(rows) - 1


if __name__ == "__main__":
    main()
