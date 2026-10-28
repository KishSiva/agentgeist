import hashlib
import os
import re
from urllib.parse import urlsplit

import httpx

from .connections import MCPConnection


CHANNEL_ID = "1564842098740695100"


class NoDiscordConnection(RuntimeError):
    pass


def select_connection(integrations):
    connections = [c for c in integrations.get("connections", []) if c.get("platform") == "discord"]
    configured = os.getenv("ONE_DISCORD_CONNECTION_KEY")
    if configured:
        connections = [c for c in connections if c.get("key") == configured]
    writable = [c for c in connections if c.get("access", {}).get("policy") in ("full", "actions")
                or "POST" in c.get("access", {}).get("methods", [])]
    if len(writable) != 1:
        raise NoDiscordConnection("Exactly one writable Discord connection must be selected")
    return writable[0]["key"]


def action_contract(docs):
    if not isinstance(docs, str):
        raise RuntimeError("Unexpected One action documentation format")
    heading = re.search(r"### Create a Message in a Channel[^\n]*actionId: `([^`]+)`", docs)
    if not heading or not re.search(r"POST /channels/\{\{channel(?:_id|Id)\}\}/messages", docs):
        raise RuntimeError("One did not return the expected Discord create-message contract")
    return heading.group(1)


def message_receipt(response):
    if not isinstance(response, dict):
        raise RuntimeError("Discord delivery did not return a structured response")
    if str(response.get("channel_id")) == CHANNEL_ID and response.get("id"):
        receipt = {"status": "sent", "channel_id": CHANNEL_ID, "message_id": str(response["id"])}
        guild = str(response.get("guild_id", ""))
        if guild.isdigit() and str(response["id"]).isdigit():
            receipt["message_url"] = f"https://discord.com/channels/{guild}/{CHANNEL_ID}/{response['id']}"
        return receipt
    for key in ("data", "body", "response", "result"):
        if isinstance(response.get(key), dict):
            try:
                return message_receipt(response[key])
            except RuntimeError:
                pass
    raise RuntimeError("No message identifier for the approved Discord channel; delivery remains unverified")


def notification_body(team, tick, result, judgment):
    from .contracts import safety_floor
    decision = safety_floor(tick).model_dump()  # Never publish untrusted model or web text.
    message = (f"Agentgeist ({team}) | SIMULATION | tick {tick.tick_id} | {decision['action']}\n"
               f"{decision['rationale']}\n"
               f"Score: {judgment.get('score', 'not supplied')}; false alarm: {judgment.get('false_alarm', 'not supplied')}. "
               f"Peak agents: {result['peak_active_agents']}; decision time: {result.get('decision_seconds', 'n/a')}s.\n"
               f"Sensor inspection order: {', '.join(result.get('sensor_inspection_priority', []))}")
    nonce = str(int(hashlib.sha256(f"{team}:{tick.tick_id}".encode()).hexdigest()[:16], 16))
    return {"content": message[:1900], "allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}


def _duration(value):
    if type(value) not in (int, float) or not 0 <= value < 86400:
        return None
    return f"{value:.3f}".rstrip("0").rstrip(".")


def run_notification_body(team, run_id, mode, run_status, tick=None, result=None, submission_requested=False):
    """Build a trusted run-level message without publishing model or venue content."""
    from .contracts import safety_floor
    if mode not in ("fixture", "live") or run_status not in ("completed", "failed"):
        raise ValueError("Invalid run notification state")
    if not re.fullmatch(r"[a-f0-9-]{16,64}", run_id):
        raise ValueError("Invalid run identifier")
    state = "COMPLETE" if run_status == "completed" else "FAILED"
    lines = [f"Agentgeist ({team}) | RUN {state}", f"Run ID: {run_id}"]
    submission = "requested" if submission_requested else "not requested"
    lines.append(f"Venue submission: {submission}. This is a run summary, not a judgment.")
    if tick is not None:
        decision = safety_floor(tick).model_dump()
        lines.append(f"Safety decision: {decision['action']} — {decision['rationale']}")
    if isinstance(result, dict):
        metrics = []
        peak = result.get("peak_active_agents")
        if type(peak) is int and 0 <= peak <= 100:
            metrics.append(f"peak agents: {peak}")
        decision_seconds = _duration(result.get("decision_seconds"))
        if decision_seconds is not None:
            metrics.append(f"decision: {decision_seconds}s")
        timings = result.get("timings")
        if isinstance(timings, dict):
            for name in ("specialists", "commander", "total"):
                duration = _duration(timings.get(name + "_seconds"))
                if duration is not None:
                    metrics.append(f"{name}: {duration}s")
        if metrics:
            lines.append("Metrics: " + "; ".join(metrics))
    message = "\n".join(lines)
    nonce = str(int(hashlib.sha256(f"run:{team}:{run_id}".encode()).hexdigest()[:16], 16))
    return {"content": message[:1900], "allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}


def notify_one(body, before_send):
    connection = MCPConnection(one=True)
    try:
        key = select_connection(connection.call("list_one_integrations"))
        docs = connection.call("find_one_actions", requests=[{"platform": "discord", "intent": "send a message to a channel"}],
                               task="Post an approved containment decision and its judgment to the incident channel",
                               ai_model="agentgeist-deterministic-escalation-officer")
        action = action_contract(docs)
        before_send()
        response = connection.call("execute_one_action", platform="discord", actionId=action, connectionKey=key,
                                   pathVariables={"channel_id": CHANNEL_ID, "channelId": CHANNEL_ID}, data=body)
        return {**message_receipt(response), "delivery": "one"}
    finally:
        connection.close()


def webhook_url():
    value = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "discord.com" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/[0-9]+/[A-Za-z0-9._-]+", parsed.path)):
        raise ValueError("Configure a valid Discord webhook URL without query parameters")
    return value


def notify_webhook(body, before_send):
    # The URL contains a credential. Never return it or include it in an exception.
    import logging
    url = webhook_url()
    logger = logging.getLogger("httpx")
    was_disabled = logger.disabled
    logger.disabled = True
    try:
        with httpx.Client(timeout=25, follow_redirects=False) as client:
            metadata = client.get(url)
            metadata.raise_for_status()
            target = metadata.json()
            if not isinstance(target, dict) or str(target.get("channel_id")) != CHANNEL_ID:
                raise RuntimeError("Webhook does not target the approved incident channel")
            before_send()  # Persist unknown only immediately before the write.
            response = client.post(url, params={"wait": "true"},
                                   json={"content": body["content"], "allowed_mentions": {"parse": []}})
            response.raise_for_status()
            message = response.json()
            if isinstance(message, dict) and not message.get("guild_id"):
                message["guild_id"] = target.get("guild_id")
            return {**message_receipt(message), "delivery": "webhook"}
    except Exception:
        raise RuntimeError("Webhook delivery failed; inspect the saved delivery state before retrying") from None
    finally:
        logger.disabled = was_disabled


def _deliver(body, ledger, before_send, transport):
    """One officer, with operator-approved webhook delivery and no ambiguous retry."""
    transport = transport or os.getenv("DISCORD_TRANSPORT", "auto")
    if transport not in ("auto", "one", "webhook"):
        raise ValueError("Discord transport must be auto, one, or webhook")
    with ledger.agents(1):
        attempted = False

        def mark_attempted():
            nonlocal attempted
            before_send()
            attempted = True

        if transport == "webhook":
            return notify_webhook(body, mark_attempted)
        try:
            # Only One uses incident credits. Venue webhook I/O needs no model/provider reservation.
            ledger.reserve(1500)
            return notify_one(body, mark_attempted)
        except Exception:
            if transport != "auto" or attempted or not os.getenv("DISCORD_WEBHOOK_URL"):
                raise
            return notify_webhook(body, mark_attempted)


def notify(team, tick, result, judgment, ledger, before_send=lambda: None, transport=None):
    return _deliver(notification_body(team, tick, result, judgment), ledger, before_send, transport)


def notify_run(team, run_id, mode, run_status, ledger, tick=None, result=None,
               submission_requested=False, before_send=lambda: None, transport=None):
    return _deliver(run_notification_body(team, run_id, mode, run_status, tick, result, submission_requested),
                    ledger, before_send, transport)
