# HireLab Screener — Personal MCP Connector

Lets *your* Claude read from *your* HireLab Screener ATS. Read-only and safe: it
logs in as a service account and calls your existing owner-scoped API, so all
your tenant-isolation rules apply. Claude can search and read; it cannot edit or
delete.

## What Claude can do (15 tools)

**Read (always on)**
| Tool | What it answers |
|---|---|
| search_candidates | semantic search — "senior PLC engineer in Pune under 20 LPA" |
| search_database | filtered search by company/location/CTC |
| list_mandates | your open roles |
| get_pipeline | candidates in a mandate + stage |
| get_candidate | one candidate's full profile |
| get_today_tasks | your action items |
| get_overview | business snapshot |

**Write / Actions (on when `MCP_ALLOW_WRITES=true`, the default)**
| Tool | What it does | Confirmation |
|---|---|---|
| add_candidate | add a new candidate to a mandate | normal |
| move_candidate_stage | move a candidate's stage | normal |
| update_candidate | edit candidate fields (CTC, notice, feedback…) | Claude confirms (edits data) |
| schedule_interview | schedule an interview round | normal |
| add_tags | tag a candidate | normal |
| create_task | add a to-do to the Command Center | normal |
| create_reminder | create a follow-up reminder | normal |
| send_candidate_email | **send a real email** to a candidate | Claude confirms (sends email) |

You can now just tell Claude what to do — e.g. *"Add Ravi Kumar (ABB, 18 LPA, Pune) to the L&T PLC mandate and set a reminder to call him tomorrow."*

### Safety
- Every action goes through your ATS's owner-scoped API, so Claude only ever touches your company's data.
- Write tools carry MCP annotations, so Claude surfaces/asks before edits and email sends.
- No delete tools are exposed — deletions stay in the app.
- Flip `MCP_ALLOW_WRITES=false` in Render anytime to instantly go back to read-only.

## One-time setup

### 1. Create a service account in your ATS
Make a normal ATS user (e.g. username `claude-mcp`) with a strong password. Using
a dedicated account means you can revoke MCP access anytime without touching your
own login. It must belong to your company so it sees your data.

### 2. Deploy this folder as a NEW Render service
- Put this folder in a Git repo (or a subfolder) and create a **new** Render Web
  Service pointing at it (it is separate from your ATS service).
- Render will use `render.yaml` (build: install requirements, start: python mcp_server.py).
- In the new service's **Environment**, set:
  - `ATS_BASE_URL`  = your ATS URL, e.g. `https://hirelabscreener.onrender.com` (no trailing slash)
  - `ATS_USERNAME`  = `claude-mcp`
  - `ATS_PASSWORD`  = that account's password
  - `MCP_SECRET`    = a long random string (e.g. 30+ characters). Keep it private.
  - `MCP_ALLOW_WRITES` = `true` to allow actions (add/update/email), or `false` for read-only. Default `true`.
- Deploy. Check `https://<your-mcp-service>.onrender.com/health` returns `{"status":"ok"}`.

### 3. Add it to Claude
- Claude.ai → Settings → Connectors → **Add custom connector**.
- URL: `https://<your-mcp-service>.onrender.com/<MCP_SECRET>/mcp`
  (the same MCP_SECRET you set above, in the path).
- Save. Then in a chat, open the connectors menu and enable it.
- Ask Claude: *"List my open mandates"* — it should call `list_mandates`.

## Security notes
- The MCP endpoint is hidden behind your secret path; without it, requests get 404.
- Tools are read-only. To add write actions later (with confirmation), extend
  TOOLS in `mcp_server.py`.
- Rotate access by changing `MCP_SECRET` (update the connector URL) or by
  disabling the service account in your ATS.
- Free Render services sleep when idle; the first call after a nap may take ~30s
  to wake. Upgrade the plan if you want it always-on.
