# Sequence MCP Server Starter

This repository contains a minimal implementation of a Model Context Protocol (MCP) server for
integrating [Sequence.io](https://getsequence.io) accounts with ChatGPT. The server is built
with **FastAPI** and can be deployed on a free tier at Render or any other hosting
provider.

## Features

- **Search (`/mcp/search`)**: Search for Sequence accounts by name or ID. Returns a list of
  results in the MCP content format.
- **Fetch (`/mcp/fetch`)**: Retrieve the full record for a Sequence account or a placeholder
  description for rules.
- **Remote amount (`/remote/amount`)**: Compute a transfer amount based on a configurable
  checking buffer, sweep percentage, and daily cap. Suitable for Sequence's “Query Remote API”
  action.
- **Trigger rule (`/rules/{rule_id}/trigger`)** (optional): Trigger a Sequence rule via
  remote API. Protected by an admin token and rule-specific secrets.

## Deployment

This repository includes a `render.yaml` file for quick deployment on
[Render](https://render.com). After uploading the repository to GitHub:

1. Create a new **Web Service** on Render and connect your GitHub repository.
2. Set the build command to `pip install -r requirements.txt` and the start command to
   `uvicorn app:app --host 0.0.0.0 --port $PORT`.
3. Add the required environment variables in the Render dashboard:
   - `SEQUENCE_ACCESS_TOKEN`: Your Sequence access token.
   - `SEQUENCE_RULE_SECRETS_JSON` (optional): JSON map of rule IDs to API secrets.
   - `MCP_ADMIN_TOKEN` (optional): A secret token to secure rule triggering.
   - `SWEEP_CHECKING_BUFFER`: Minimum checking balance before sweeping excess.
   - `SWEEP_PERCENT`: Percentage of excess to sweep into savings/investing.
   - `SWEEP_DAILY_CAP_CENTS`: Maximum cents to sweep per day.
4. Deploy the service. Once live, your MCP endpoints will be available under your
   Render domain (e.g. `https://your-service.onrender.com`).

## Usage

Configure ChatGPT to use your MCP server by adding it as a connector in
ChatGPT’s settings. Allow the `search` and `fetch` tools. For Sequence rule
actions, point the rule’s **Query Remote API** URL to `/remote/amount` on your
service, and optionally use the rule trigger endpoint with an admin token.

---

This starter kit is provided as-is and is intended to accelerate the setup of
personal or small-scale Sequence integrations. Customize the code to suit your
exact workflow needs.