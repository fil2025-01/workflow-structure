# vcp-workflow

Turns your Jira tickets into a weekly work plan in your Google Sheet.

You pick tickets from your current sprint. For each one, Claude reads the ticket and the related code, then splits the work into four phases (Investigation & Analysis, Implementation & Logic, Testing & Quality Assurance, Deployment & Documentation), with short tasks and a point estimate for each. You review each plan, and the script adds it to the `Weekly List` tab of your workflow sheet, numbered and linked to the Jira ticket.

## Usage

```sh
vcp-workflow                  # pick from your sprint tickets (falls back to To Do if all are released)
vcp-workflow GEEK-123 ...     # skip the picker
vcp-workflow --dry-run        # print the rows instead of writing them
vcp-workflow --yes            # write without the review step
vcp-workflow --new-week       # Monday: copy Weekly List to a tab named after B2, then set B2 to this week
```

Each ticket takes about 30–60 seconds to analyze (up to 3 run at once). Then each breakdown is shown with **Write / Edit / Skip**; **Edit** opens the JSON in `$EDITOR`. Numbering continues from the last `7.N` in Column A, after one blank row.

## How it works

- **Claude** (`claude -p`, read-only: `jira issue view`, Read, Grep, Glob) reads the ticket and codebase and returns the phases and tasks as structured JSON. The script rejects output with missing phases or points other than 1 or 2.
- **The script** handles everything that must be exact: numbering, bold, the `HYPERLINK` on the top row, and the title (the Jira summary without leading tags like `[VCP-12933] [AV]`).

## Setup (done once)

- `.venv` with `gspread` + `google-auth` (Python 3.13); `gum` via Homebrew; `vcp-workflow` symlinked into `~/.local/bin`.
- `~/.config/vcp/workflow.json` says which sheet to write to (set `VCP_WORKFLOW_CONFIG` to use another path):
  ```json
  {"spreadsheet_id": "<id from the sheet URL>", "tab": "Weekly List", "section": 7}
  ```
- Google auth uses Application Default Credentials with your own OAuth client, because gcloud's built-in client is blocked for the Sheets scope. If the token expires or is revoked:
  ```sh
  gcloud auth application-default login \
    --client-id-file="$HOME/.config/vcp/client_secret.json" \
    --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform
  ```

## Setting it up for someone else

Everything is per person: their own Jira login, Google login, sheet and config. Nothing they do touches yours.

1. **Tools:** `brew install gum jira-cli python@3.13`, plus `gcloud` and the `claude` CLI. Run `jira init` to log in to Jira.
2. **Copy this folder** (without `.venv`), then in it:
   ```sh
   python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ln -s "$PWD/vcp-workflow" ~/.local/bin/
   ```
3. **Their sheet:** make a copy of the workflow sheet with a `Weekly List` tab, and put its ID in `~/.config/vcp/workflow.json` (see above).
4. **Google auth:** send them `client_secret.json` privately (not in a repo or channel), save it to `~/.config/vcp/client_secret.json`, and run the `gcloud auth application-default login` command above. They log in with their own Google account, which must be able to edit their sheet.
   - If the OAuth consent screen in Google Cloud Console is **External / Testing**, add their email as a test user first, or the login is blocked. If it's **Internal**, anyone in the Workspace can log in.
5. Check it with `vcp-workflow --dry-run`.
