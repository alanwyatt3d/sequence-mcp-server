from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
import os
import json
import httpx
import asyncio
import time

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
        # Initial hello so the client sees content immediately
        yield "event: ready\ndata: ok\n\n"
        while True:
            # Stop streaming if client disconnects
            if await request.is_disconnected():
                break
            # Periodic heartbeat with a timestamp
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
    """Retrieve account data from Sequence via the remote API.

    Uses the SEQUENCE_ACCESS_TOKEN for authentication. Raises an error
    if the token is missing or if the request fails.
    """
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
        # Defensive: some errors are reported under data/errors
        return (body.get("data") or {}).get("accounts", [])

# ---------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------
@app.post("/mcp/search")
async def mcp_search(payload: dict) -> dict:
    """MCP search tool implementation.

    Accepts a JSON payload with a "query" key and returns up to 10 results
    matching the query from the Sequence accounts list. Each result
    includes an ID, title, and URL.
    """
    query = (payload.get("query") or "").lower()
    accounts = await seq_accounts()
    results = []
    for account in accounts:
        # Include accounts when the query is empty, matches the name, or matches the ID.
        if (
            not query
            or query in account["name"].lower()
            or query in str(account["id"]).lower()
            or query in ("balances", "accounts")
        ):
            # Some accounts might not have balance due to provider errors
            bal = account.get("balance", {}) or {}
            amt = bal.get("amountInDollars")
            title = f"{account['name']} — ${amt}" if amt is not None else account["name"]
            url = f"https://app.getsequence.io/accounts/{account['id']}"
            results.append({"id": str(account["id"]), "title": title, "url": url})
    # Limit to 10 results for brevity
    content = json.dumps({"results": results[:10]})
    return {"content": [{"type": "text", "text": content}]}

@app.post("/mcp/fetch")
async def mcp_fetch(payload: dict) -> dict:
    """MCP fetch tool implementation.

    Given an ID, returns the full record for that account or a placeholder
    description for rules. The response always returns a single text content
    item containing JSON.
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
    # If not an account, and ID looks like a rule, return a simple descriptor
    if record_id.startswith("ru_"):
        doc = {
            "id": record_id,
            "title": f"Sequence Rule {record_id}",
            "text": "Rule descriptor. Use POST /rules/{id}/trigger with x-admin header to invoke.",
            "url": f"https://app.getsequence.io/rules/{record_id}",
        }
        return {"content": [{"type": "text", "text": json.dumps(doc)}]}
    # Otherwise, return a not-found error
    raise HTTPException(404, "Not found")

# ---------------------------------------------------------------------
# Sequence: Query Remote API amount calculator
# ---------------------------------------------------------------------
@app.post("/remote/amount")
async def remote_amount(payload: dict) -> dict:
    """Endpoint for Sequence's Query Remote API to compute transfer amounts.

    Expects the payload to contain the current checking balance. Calculates
    the amount to transfer in cents based on the buffer, percentage, and
    daily cap. Returns a JSON response with the amount in cents.
    """
    balance = float(payload.get("checkingBalance", 0))
    excess = max(0.0, balance - BUFFER)
    transfer_cents = int(min(excess * PCT * 100, CAP))
    return {"amountInCents": transfer_cents}

# ---------------------------------------------------------------------
# Protected rule trigger
# ---------------------------------------------------------------------
@app.post("/rules/{rule_id}/trigger")
async def trigger_rule(rule_id: str, request: Request, x_admin: str = Header(None)) -> dict:
    """Protected endpoint to trigger a Sequence rule.

    Requires the x-admin header matching MCP_ADMIN_TOKEN and the rule ID
    must be in RULE_SECRETS. Uses the rule's secret to call Sequence's
    remote API trigger endpoint. Returns the JSON response from Sequence
    on success.
    """
    # Check admin token
    if not x_admin or not x_admin.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token_value = x_admin.split(" ", 1)[1]
    if token_value != ADMIN:
        raise HTTPException(403, "Forbidden")

    # Find the secret for the rule
    secret = RULE_SECRETS.get(rule_id)
    if not secret:
        raise HTTPException(403, "Rule not whitelisted")

    # Call Sequence trigger endpoint
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
