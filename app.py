from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
import os
import json
import httpx
import asyncio
import time
from typing import Any

# This FastAPI application implements a minimal MCP server for Sequence
# integration. It supports read-only MCP tools (search and fetch) for
# account data, a remote amount endpoint for Sequence's "Query Remote API"
# action, an optional trigger endpoint guarded by an admin token,
# and an SSE endpoint (/sse/) required by ChatGPT connectors.

app = FastAPI()

# ---------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------
SEQUENCE_API = "https://api.getsequence.io"
ACCESS = os.getenv("SEQUENCE_ACCESS_TOKEN", "")

# RULE_SECRETS should be a JSON-encoded map of rule IDs to API secrets.
# Example: {"ru_12345": "shh_secret_value"}
RULE_SECRETS = json.loads(os.getenv("SEQUENCE_RULE_SECRETS_JSON", "{}") or "{}")

# MCP_ADMIN_TOKEN protects write endpoints. Only requests with this token
# (in the x-admin header) can trigger rules.
ADMIN = os.getenv("MCP_ADMIN_TOKEN", "")

# Parameters for the remote amount calculation. Excess above the buffer will
# be swept at the given percentage, capped to a daily maximum in cents.
BUFFER = int(os.getenv("SWEEP_CHECKING_BUFFER", "1000"))
PCT = float(os.getenv("SWEEP_PERCENT", "0.30"))
CAP = int(os.getenv("SWEEP_DAILY_CAP_CENTS", "30000"))

# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    """Health check endpoint. Returns a simple OK response."""
    return {"ok": True}

# ---------------------------------------------------------------------
# SSE endpoint for ChatGPT MCP connector validation
# ---------------------------------------------------------------------
@app.get("/sse/")
async def sse(request: Request):
    """
    Minimal Server-Sent Events stream. ChatGPT uses this to validate the
    MCP server. We send an initial 'ready' event and periodic heartbeats
    until the client disconnects.
    """
    async def event_stream():
        yield "event: ready\ndata: ok\n\n"
        while True:
            if await request.is_disconnected():
                break
            yield f"event: heartbeat\ndata: {int(time.time())}\n\n"
            await asyncio.sleep(15)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), headers=headers, media_type="text/event-stream")

# ---------------------------------------------------------------------
# Helper: fetch accounts from Sequence
# ---------------------------------------------------------------------
async def seq_accounts() -> list:
    """Retrieve account data from Sequence via the remote API."""
    if not ACCESS:
        raise HTTPException(500, "SEQUENCE_ACCESS_TOKEN missing")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SEQUENCE_API}/accounts",
            headers={
                "x-sequence-access-token": f"Bearer {ACCESS}",
                "Content-Type": "application/json",
            },
            json={},
        )
        response.raise_for_status()
        body = response.json()
        return (body.get("data") or {}).get("accounts", [])

# ---------------------------------------------------------------------
# MCP tools (canonical implementations)
# ---------------------------------------------------------------------
@app.post("/mcp/search")
async def mcp_search(payload: dict) -> dict:
    """
    Accepts JSON with {"query": "..."} and returns up to 10 Sequence
    accounts as {"content":[{"type":"text","text": "{\"results\":[...]}" }]}.
    """
    query = (payload.get("query") or "").lower()
    accounts = await seq_accounts()
    results = []
    for account in accounts:
        if (
            not query
            or query in account["name"].lower()
            or query in str(account["id"]).lower()
            or query in ("balances", "accounts")
        ):
            bal = account.get("balance", {}) or {}
            amt = bal.get("amountInDollars")
            title = f"{account['name']} — ${amt}" if amt is not None else account["name"]
            url = f"https://app.getsequence.io/accounts/{account['id']}"
            results.append({"id": str(account["id"]), "title": title, "url": url})
    content = json.dumps({"results": results[:10]})
    return {"content": [{"type": "text", "text": content}]}

@app.post("/mcp/fetch")
async def mcp_fetch(payload: dict) -> dict:
    """
    Accepts JSON with {"id":"..."} and returns a single document object
    inside a text content item.
    """
    record_id = (payload.get("id") or "").strip()
    accounts = await seq_accounts()
    match = next((acct for acct in accounts if str(acct.get("id")) == record_id), None)
    if match:
        doc = {
            "id": str(match["id"]),
            "title": match["name"],
            "text": json.dumps(match),
            "url": f"https://app.getsequence.io/accounts/{match['id']}",
        }
        return {"content": [{"type": "text", "text": json.dumps(doc)}]}
    if record_id.startswith("ru_"):
        doc = {
            "id": record_id,
            "title": f"Sequence Rule {record_id}",
            "text": "Rule descriptor. Use POST /rules/{id}/trigger with x-admin header to invoke.",
            "url": f"https://app.getsequence.io/rules/{record_id}",
        }
        return {"content": [{"type": "text", "text": json.dumps(doc)}]}
    raise HTTPException(404, "Not found")

# ---------------------------------------------------------------------
# ChatGPT-facing wrappers (paths ChatGPT expects)
# ---------------------------------------------------------------------
def _normalize_payload(body: Any) -> dict:
    """
    Accepts either a JSON object or a raw string body and normalizes it.
    - For /search: raw string -> {"query": raw}
    - For /fetch:  raw string -> {"id": raw}
    """
    if isinstance(body, dict):
        return body
    # If body is already bytes/str from an upstream middleware, try to parse
    if isinstance(body, (bytes, bytearray)):
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "ignore")}
    if isinstance(body, str):
        # Caller decides which key to read (search uses 'query', fetch uses 'id')
        return {"raw": body}
    return {}

@app.post("/search")
async def search(body: Any) -> dict:
    """
    Wrapper for MCP search tool; supports either:
      - {"query": "<string>"}  (JSON)
      - "<string>"              (raw body)
    """
    payload = _normalize_payload(body)
    if "query" not in payload and "raw" in payload:
        payload = {"query": payload["raw"]}
    return await mcp_search(payload)

@app.post("/fetch")
async def fetch(body: Any) -> dict:
    """
    Wrapper for MCP fetch tool; supports either:
      - {"id": "<string>"}  (JSON)
      - "<string>"          (raw body)
    """
    payload = _normalize_payload(body)
    if "id" not in payload and "raw" in payload:
        payload = {"id": payload["raw"]}
    return await mcp_fetch(payload)

# ---------------------------------------------------------------------
# Sequence: Query Remote API amount calculator
# ---------------------------------------------------------------------
@app.post("/remote/amount")
async def remote_amount(payload: dict) -> dict:
    """Endpoint for Sequence's Query Remote API to compute transfer amounts."""
    balance = float(payload.get("checkingBalance", 0))
    excess = max(0.0, balance - BUFFER)
    transfer_cents = int(min(excess * PCT * 100, CAP))
    return {"amountInCents": transfer_cents}

# ---------------------------------------------------------------------
# Protected rule trigger
# ---------------------------------------------------------------------
@app.post("/rules/{rule_id}/trigger")
async def trigger_rule(rule_id: str, request: Request, x_admin: str = Header(None)) -> dict:
    """
    Protected endpoint to trigger a Sequence rule using RULE_SECRETS and MCP_ADMIN_TOKEN.
    """
    if not x_admin or not x_admin.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token_value = x_admin.split(" ", 1)[1]
    if token_value != ADMIN:
        raise HTTPException(403, "Forbidden")

    secret = RULE_SECRETS.get(rule_id)
    if not secret:
        raise HTTPException(403, "Rule not whitelisted")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SEQUENCE_API}/remote-api/rules/{rule_id}/trigger",
            headers={
                "x-sequence-signature": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            json={},
        )
        response.raise_for_status()
        return response.json()
