"""Tell someone when a run in the web UI needs them, through a webhook.

    HARNESS_NOTIFY_URL=https://hooks.slack.com/services/...
    HARNESS_NOTIFY_ON=signoff          or: all (also when a run finishes or fails)

The webhook gets a JSON POST with `text` (Slack, Mattermost, Rocket.Chat) and `content`
(Discord) set to one line, plus the fields for anything else (n8n, Zapier, Home Assistant).
The line includes the start of the request, so choose a webhook you trust with that. The
link opens the conversation; it carries no token, so it works only where you are signed in.
"""

import logging
from typing import Literal

import httpx

log = logging.getLogger(__name__)

NotifyOn = Literal["signoff", "all"]

SAID = {
    "needs_human": "is waiting for your sign-off",
    "done": "is done",
    "max_steps": "stopped before finishing",
    "failed": "failed",
}


def should_notify(on: str, status: str) -> bool:
    if on == "all":
        return status in SAID
    return status == "needs_human"


def message(status: str, goal: str, link: str) -> str:
    request = " ".join(goal.split())
    if len(request) > 120:
        request = request[:119].rstrip() + "…"
    return f"Hemisphere Harness: “{request}” {SAID.get(status, status)}. {link}"


async def notify(
    url: str, *, run_id: str, status: str, goal: str, link: str, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """POST the notice. Never raises: a webhook that is down must not break the run."""
    line = message(status, goal, link)
    payload = {"text": line, "content": line, "event": status, "run_id": run_id, "goal": goal[:500], "link": link}
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            response = await client.post(url, json=payload)
        if response.is_success:
            return True
        log.warning("the notification webhook answered HTTP %s", response.status_code)
    except httpx.HTTPError as exc:
        log.warning("the notification webhook could not be reached: %s", type(exc).__name__)
    return False
