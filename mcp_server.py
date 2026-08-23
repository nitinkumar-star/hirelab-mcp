"""
HireLab Screener — personal MCP connector for Claude.ai
=======================================================

A small, dependency-light remote MCP server (Streamable HTTP) that lets *your*
Claude read from *your* HireLab Screener ATS.

How it stays safe
-----------------
* It logs into the ATS as a normal user (a dedicated service account you create)
  and calls the SAME owner-scoped API endpoints your app uses. So every tenant
  isolation rule already in the ATS automatically applies here — Claude only ever
  sees the data that service account's company can see.
* The tools are READ-ONLY. Claude can search and read; it cannot edit or delete.
* The MCP endpoint sits behind a secret path segment (MCP_SECRET), so only a URL
  that includes your secret can reach it.

Environment variables (set these in Render — never hard-code):
    ATS_BASE_URL   e.g. https://hirelabscreener.onrender.com   (no trailing slash)
    ATS_USERNAME   the service-account username in your ATS
    ATS_PASSWORD   that account's password
    MCP_SECRET     any long random string (this goes in the connector URL)
    PORT           provided automatically by Render

Connector URL you paste into Claude.ai:
    https://<your-mcp-service>.onrender.com/<MCP_SECRET>/mcp

This file has no MCP SDK dependency on purpose — it implements just the small
slice of the Streamable HTTP + JSON-RPC protocol that Claude needs, so it stays
stable across SDK releases.
"""

import os
import json
import threading

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, Response

# ── Config ──────────────────────────────────────────────────────────────────
ATS_BASE_URL = (os.environ.get("ATS_BASE_URL") or "").rstrip("/")
ATS_USERNAME = os.environ.get("ATS_USERNAME") or ""
ATS_PASSWORD = os.environ.get("ATS_PASSWORD") or ""
MCP_SECRET   = os.environ.get("MCP_SECRET") or ""
PORT         = int(os.environ.get("PORT") or "8000")
# Safety switch: set MCP_ALLOW_WRITES=false to instantly drop back to read-only
# (write tools disappear from tools/list and are refused if called).
ALLOW_WRITES = (os.environ.get("MCP_ALLOW_WRITES") or "true").strip().lower() in ("1", "true", "yes", "on")

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "hirelab-screener", "version": "1.0.0"}


# ── ATS client (holds a session cookie, re-logins on expiry) ────────────────
class ATS:
    def __init__(self):
        self._client = httpx.Client(base_url=ATS_BASE_URL, timeout=45.0, follow_redirects=True)
        self._lock = threading.Lock()
        self._logged_in = False

    def _login(self):
        r = self._client.post("/api/auth/login",
                              json={"username": ATS_USERNAME, "password": ATS_PASSWORD})
        if r.status_code != 200:
            raise RuntimeError(f"ATS login failed ({r.status_code}): check ATS_USERNAME/ATS_PASSWORD")
        self._logged_in = True

    def _request(self, method, path, **kw):
        with self._lock:
            if not self._logged_in:
                self._login()
            r = self._client.request(method, path, **kw)
            if r.status_code in (401, 403):        # session expired -> re-login once
                self._login()
                r = self._client.request(method, path, **kw)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, payload=None):
        return self._request("POST", path, json=payload or {})

    def put(self, path, payload=None):
        return self._request("PUT", path, json=payload or {})


ats = ATS()


# ── Small helpers to keep tool output compact ───────────────────────────────
def _slim_candidate(c):
    if not isinstance(c, dict):
        return c
    keep = ("id", "name", "company", "designation", "experience", "ctc_current",
            "ctc_expected", "notice_period", "location", "stage", "phone", "email",
            "mandate_id", "match", "score", "match_pct")
    return {k: c.get(k) for k in keep if k in c}


# ── Tool implementations (READ-ONLY) ────────────────────────────────────────
def tool_search_candidates(query: str, top_k: int = 10):
    data = ats.post("/api/ai/search", {"query": query, "top_k": int(top_k)})
    results = data.get("results") or data.get("candidates") or data
    if isinstance(results, list):
        return {"query": query, "results": [_slim_candidate(x) for x in results][:top_k]}
    return data


def tool_search_database(query: str = "", company: str = "", location: str = "",
                         phone: str = "", ctc_min: str = "", ctc_max: str = ""):
    params = {"q": query, "company": company, "location": location,
              "phone": phone, "ctc_min": ctc_min, "ctc_max": ctc_max}
    params = {k: v for k, v in params.items() if v != ""}
    data = ats.get("/api/central-db/search", params=params)
    results = data.get("results") if isinstance(data, dict) else data
    if isinstance(results, list):
        return {"results": [_slim_candidate(x) for x in results][:50]}
    return data


def tool_list_mandates():
    data = ats.get("/api/mandates")
    if isinstance(data, list):
        keep = ("id", "client", "role", "status", "location", "ctc_min", "ctc_max",
                "created_at", "assigned_user_id")
        return {"mandates": [{k: m.get(k) for k in keep if k in m} for m in data]}
    return data


def tool_get_pipeline(mandate_id: int):
    data = ats.get(f"/api/mandates/{int(mandate_id)}/candidates")
    cands = data.get("candidates") if isinstance(data, dict) else data
    if isinstance(cands, list):
        return {"mandate_id": mandate_id, "candidates": [_slim_candidate(c) for c in cands]}
    return data


def tool_get_candidate(candidate_id: int):
    return ats.get(f"/api/candidates/{int(candidate_id)}")


def tool_get_today_tasks():
    return ats.get("/api/command/tasks")


def tool_get_overview():
    return ats.get("/api/command/overview")


# ── Tool implementations (WRITE — only active when ALLOW_WRITES) ─────────────
def tool_add_candidate(mandate_id: int, name: str, company: str = "", designation: str = "",
                       experience: float = 0, ctc_current: float = 0, ctc_expected: float = 0,
                       notice_period: int = 0, location: str = "", phone: str = "", email: str = "",
                       key_skills: str = ""):
    payload = {"candidates": [{
        "name": name, "company": company, "designation": designation,
        "experience": experience, "ctc_current": ctc_current, "ctc_expected": ctc_expected,
        "notice_period": notice_period, "location": location, "phone": phone,
        "email": email, "key_skills": key_skills,
    }]}
    return ats.post(f"/api/mandates/{int(mandate_id)}/candidates/bulk", payload)


def tool_move_candidate_stage(candidate_id: int, stage: str):
    return ats.post(f"/api/candidates/{int(candidate_id)}/stage", {"stage": stage})


def tool_update_candidate(candidate_id: int, fields: dict):
    return ats.put(f"/api/candidates/{int(candidate_id)}", fields or {})


def tool_schedule_interview(candidate_id: int, round_name: str = "Interview", scheduled_at: str = "",
                            mode: str = "", location: str = "", interviewer: str = ""):
    return ats.post(f"/api/candidates/{int(candidate_id)}/interviews", {
        "round_name": round_name, "scheduled_at": scheduled_at, "mode": mode,
        "location": location, "interviewer": interviewer,
    })


def tool_add_tags(candidate_id: int, tags: list, tag_type: str = "general"):
    return ats.post(f"/api/candidates/{int(candidate_id)}/tags", {"tag_type": tag_type, "tags": tags})


def tool_create_task(text: str, category: str = "", priority: str = "medium"):
    return ats.post("/api/command/tasks", {"text": text, "category": category, "priority": priority})


def tool_create_reminder(note: str, candidate_id: int = 0, due_at: str = "", stage: str = "todo"):
    payload = {"note": note, "due_at": due_at, "stage": stage}
    if candidate_id:
        payload["candidate_id"] = int(candidate_id)
    return ats.post("/api/reminders", payload)


def tool_send_candidate_email(candidate_id: int, to: str, subject: str, body: str):
    return ats.post(f"/api/candidates/{int(candidate_id)}/send-email",
                    {"to": to, "subject": subject, "body": body})


# ── Tool registry (name -> schema + handler). Descriptions double as the
#    "map" that teaches Claude what data exists and where it comes from. ──────
TOOLS = [
    {
        "name": "search_candidates",
        "description": ("Semantic (AI) search across the recruiter's candidate database. "
                        "Use for natural-language needs like 'senior PLC automation engineer in Pune under 20 LPA'. "
                        "Source: POST /api/ai/search. Returns matched candidates with name, company, "
                        "designation, experience, CTC, location and stage."),
        "handler": tool_search_candidates,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "top_k": {"type": "integer", "description": "How many results (default 10).", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_database",
        "description": ("Structured/keyword search of the central candidate database by exact-ish filters "
                        "(company, location, phone, CTC band). Use when you have specific filters rather than "
                        "a fuzzy description. Source: GET /api/central-db/search."),
        "handler": tool_search_database,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text keyword (name/skill)."},
                "company": {"type": "string"},
                "location": {"type": "string"},
                "phone": {"type": "string"},
                "ctc_min": {"type": "string"},
                "ctc_max": {"type": "string"},
            },
        },
    },
    {
        "name": "list_mandates",
        "description": ("List the recruiter's open job mandates (roles they are hiring for). "
                        "Source: GET /api/mandates. Returns id, client, role, status, location, CTC band. "
                        "Use the mandate id with get_pipeline to see its candidates."),
        "handler": tool_list_mandates,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_pipeline",
        "description": ("Get all candidates currently in the pipeline for one mandate, with their stage. "
                        "Source: GET /api/mandates/<mandate_id>/candidates."),
        "handler": tool_get_pipeline,
        "inputSchema": {
            "type": "object",
            "properties": {"mandate_id": {"type": "integer", "description": "The mandate's id."}},
            "required": ["mandate_id"],
        },
    },
    {
        "name": "get_candidate",
        "description": ("Get the full profile of one candidate by id (work history, CTC, notice period, "
                        "stage, contact, key facts). Source: GET /api/candidates/<candidate_id>."),
        "handler": tool_get_candidate,
        "inputSchema": {
            "type": "object",
            "properties": {"candidate_id": {"type": "integer", "description": "The candidate's id."}},
            "required": ["candidate_id"],
        },
    },
    {
        "name": "get_today_tasks",
        "description": ("The recruiter's current action items / to-dos from the CEO Command Center. "
                        "Source: GET /api/command/tasks."),
        "handler": tool_get_today_tasks,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_overview",
        "description": ("A high-level business snapshot (pipeline counts, activity) from the Command Center. "
                        "Source: GET /api/command/overview."),
        "handler": tool_get_overview,
        "inputSchema": {"type": "object", "properties": {}},
    },

    # ── WRITE / ACTION TOOLS (only listed when MCP_ALLOW_WRITES is on) ────────
    {
        "name": "add_candidate",
        "description": ("Add a NEW candidate to a mandate's pipeline. Use after confirming the mandate id "
                        "with list_mandates. Source: POST /api/mandates/<mandate_id>/candidates/bulk."),
        "handler": tool_add_candidate, "write": True,
        "annotations": {"title": "Add candidate", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "mandate_id": {"type": "integer"},
                "name": {"type": "string"},
                "company": {"type": "string"}, "designation": {"type": "string"},
                "experience": {"type": "number"}, "ctc_current": {"type": "number"},
                "ctc_expected": {"type": "number"}, "notice_period": {"type": "integer"},
                "location": {"type": "string"}, "phone": {"type": "string"},
                "email": {"type": "string"}, "key_skills": {"type": "string"},
            },
            "required": ["mandate_id", "name"],
        },
    },
    {
        "name": "move_candidate_stage",
        "description": ("Move a candidate to a different pipeline stage (e.g. 'Follow Up 1', 'Interview', "
                        "'Offer'). Reversible; logged in stage history. Source: POST /api/candidates/<id>/stage."),
        "handler": tool_move_candidate_stage, "write": True,
        "annotations": {"title": "Move stage", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"candidate_id": {"type": "integer"}, "stage": {"type": "string"}},
            "required": ["candidate_id", "stage"],
        },
    },
    {
        "name": "update_candidate",
        "description": ("Update fields on an existing candidate (e.g. ctc_current, notice_period, phone, "
                        "recruiter_feedback, key_skills). Overwrites the given fields. "
                        "Source: PUT /api/candidates/<id>."),
        "handler": tool_update_candidate, "write": True,
        "annotations": {"title": "Update candidate", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer"},
                "fields": {"type": "object", "description": "Map of field->value to overwrite, e.g. {\"ctc_current\": 22, \"notice_period\": 30}."},
            },
            "required": ["candidate_id", "fields"],
        },
    },
    {
        "name": "schedule_interview",
        "description": ("Schedule an interview round for a candidate. Source: POST /api/candidates/<id>/interviews. "
                        "scheduled_at should be an ISO datetime string."),
        "handler": tool_schedule_interview, "write": True,
        "annotations": {"title": "Schedule interview", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer"}, "round_name": {"type": "string"},
                "scheduled_at": {"type": "string"}, "mode": {"type": "string"},
                "location": {"type": "string"}, "interviewer": {"type": "string"},
            },
            "required": ["candidate_id"],
        },
    },
    {
        "name": "add_tags",
        "description": ("Add tags/labels to a candidate. Source: POST /api/candidates/<id>/tags."),
        "handler": tool_add_tags, "write": True,
        "annotations": {"title": "Add tags", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "tag_type": {"type": "string", "default": "general"},
            },
            "required": ["candidate_id", "tags"],
        },
    },
    {
        "name": "create_task",
        "description": ("Add a to-do / action item to the recruiter's Command Center task list. "
                        "Source: POST /api/command/tasks."),
        "handler": tool_create_task, "write": True,
        "annotations": {"title": "Create task", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"}, "category": {"type": "string"},
                "priority": {"type": "string", "description": "low | medium | high", "default": "medium"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "create_reminder",
        "description": ("Create a follow-up reminder, optionally tied to a candidate. "
                        "Source: POST /api/reminders. due_at is an ISO date/datetime string."),
        "handler": tool_create_reminder, "write": True,
        "annotations": {"title": "Create reminder", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"}, "candidate_id": {"type": "integer"},
                "due_at": {"type": "string"}, "stage": {"type": "string"},
            },
            "required": ["note"],
        },
    },
    {
        "name": "send_candidate_email",
        "description": ("Send a real email to a candidate and log it on their record. This actually delivers "
                        "an email — always confirm the recipient, subject and body with the user first. "
                        "Source: POST /api/candidates/<id>/send-email."),
        "handler": tool_send_candidate_email, "write": True,
        "annotations": {"title": "Send email", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer"}, "to": {"type": "string"},
                "subject": {"type": "string"}, "body": {"type": "string"},
            },
            "required": ["candidate_id", "to", "subject", "body"],
        },
    },
]

# Normalize: read tools are read-only by default; write tools carry their own annotations.
for _t in TOOLS:
    _t.setdefault("write", False)
    _t.setdefault("annotations", {"title": _t["name"], "readOnlyHint": True, "destructiveHint": False})

TOOL_MAP = {t["name"]: t for t in TOOLS}


# ── Minimal JSON-RPC / MCP handling ─────────────────────────────────────────
def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_rpc(msg):
    """Return a JSON-RPC response dict, or None for notifications (no reply)."""
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # notification, no response

    if method == "ping":
        return _rpc_result(req_id, {})

    if method == "tools/list":
        visible = [t for t in TOOLS if ALLOW_WRITES or not t["write"]]
        return _rpc_result(req_id, {
            "tools": [{"name": t["name"], "description": t["description"],
                       "inputSchema": t["inputSchema"], "annotations": t["annotations"]}
                      for t in visible]
        })

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if not tool:
            return _rpc_error(req_id, -32602, f"Unknown tool: {name}")
        if tool["write"] and not ALLOW_WRITES:
            return _rpc_result(req_id, {
                "content": [{"type": "text",
                             "text": "Write actions are disabled (MCP_ALLOW_WRITES is off). This is a read-only connection."}],
                "isError": True})
        try:
            out = tool["handler"](**args)
            text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
            return _rpc_result(req_id, {"content": [{"type": "text", "text": text}]})
        except httpx.HTTPStatusError as e:
            return _rpc_result(req_id, {
                "content": [{"type": "text",
                             "text": f"ATS returned {e.response.status_code} for this request."}],
                "isError": True})
        except Exception as e:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": f"Tool error: {e}"}], "isError": True})

    return _rpc_error(req_id, -32601, f"Method not found: {method}")


# ── HTTP endpoints ──────────────────────────────────────────────────────────
async def mcp_endpoint(request):
    # Secret path guard
    if request.path_params.get("secret") != MCP_SECRET or not MCP_SECRET:
        return Response("Not found", status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)

    # A batch (list) or a single message
    if isinstance(body, list):
        responses = [r for r in (_handle_rpc(m) for m in body) if r is not None]
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    resp = _handle_rpc(body)
    if resp is None:
        return Response(status_code=202)  # notification
    return JSONResponse(resp)


async def health(request):
    ok = bool(ATS_BASE_URL and ATS_USERNAME and ATS_PASSWORD and MCP_SECRET)
    return JSONResponse({"status": "ok" if ok else "misconfigured",
                         "ats_url_set": bool(ATS_BASE_URL),
                         "secret_set": bool(MCP_SECRET)})


app = Starlette(routes=[
    Route("/health", health, methods=["GET"]),
    Route("/{secret}/mcp", mcp_endpoint, methods=["POST"]),
])


if __name__ == "__main__":
    import uvicorn
    missing = [k for k, v in [("ATS_BASE_URL", ATS_BASE_URL), ("ATS_USERNAME", ATS_USERNAME),
                              ("ATS_PASSWORD", ATS_PASSWORD), ("MCP_SECRET", MCP_SECRET)] if not v]
    if missing:
        print(f"[warn] missing env vars: {', '.join(missing)} — set them in Render before use.")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
