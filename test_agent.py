"""Quick Workspace Agent API sanity check.

Run from the Sales_App_Backend directory:
    python test_agent.py
"""

from pathlib import Path
import os

import httpx

BASE = "https://api.chatgpt.com/v1"
DEFAULT_TRIGGER_ID = "agtch_6a42944ed300819194b33fc75540665e"
ENV_FILE = Path(__file__).resolve().parent / ".env"


def _load_env_value(name: str) -> str:
    value = os.getenv(name, "").strip().strip("\"' ")
    if value:
        return value

    if not ENV_FILE.exists():
        return ""

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip() == name:
            return raw_value.strip().strip("\"' ")
    return ""


def _mask(value: str) -> str:
    if not value:
        return "(missing)"
    if len(value) <= 8:
        return "<set>"
    return f"{value[:4]}...{value[-2:]}"


def _print_diagnosis(status_code: int) -> None:
    if status_code == 202:
        print("Diagnosis: trigger accepted.")
    elif status_code == 401:
        print("Diagnosis: wrong token type or scope. Use a Workspace Agent access token with the Workspace Agents scope.")
    elif status_code == 403:
        print("Diagnosis: token is valid, but the token owner cannot run this published agent in the workspace.")
    elif status_code == 404:
        print("Diagnosis: wrong agtch_ trigger ID or channel not visible.")
    elif status_code == 409:
        print("Diagnosis: agent or API channel is not runnable. Publish the agent and confirm the API channel is active.")
    else:
        print("Diagnosis: unexpected response, inspect the response body.")


def main() -> None:
    token = _load_env_value("AGENT_ACCESS_TOKEN")
    trigger_id = _load_env_value("WORKSPACE_AGENT_TRIGGER_ID") or DEFAULT_TRIGGER_ID

    if not token:
        raise SystemExit("Missing AGENT_ACCESS_TOKEN in environment or Sales_App_Backend/.env.")

    endpoint = f"{BASE}/workspace_agents/{trigger_id}/trigger"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": (
            "Health check from Sales_App_Backend/test_agent.py. "
            "Confirm that the Workspace Agent trigger is reachable."
        )
    }

    print(f"Endpoint: {endpoint}")
    print(f"Token:    {_mask(token)}")
    print("\n=== POST /trigger ===")

    response = httpx.post(
        endpoint,
        json=payload,
        headers=headers,
        timeout=30,
    )
    print(f"Status: {response.status_code}")
    print(f"Body:   {response.text[:1000]}")
    _print_diagnosis(response.status_code)


if __name__ == "__main__":
    main()
