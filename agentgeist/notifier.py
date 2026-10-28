import hashlib
import os
import re

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
        return {"status": "sent", "channel_id": CHANNEL_ID, "message_id": str(response["id"])}
    for key in ("data", "body", "response", "result"):
        if isinstance(response.get(key), dict):
            try:
                return message_receipt(response[key])
            except RuntimeError:
                pass
    raise RuntimeError("No message identifier for the approved Discord channel; delivery remains unverified")


def notify(team, tick, result, judgment, ledger):
    """A deterministic escalation officer. No access to raw sensor or research tools."""
    with ledger.agents(1):
        ledger.reserve(1500)
        connection = MCPConnection(one=True)
        try:
            key = select_connection(connection.call("list_one_integrations"))
            docs = connection.call("find_one_actions", requests=[{"platform": "discord", "intent": "send a message to a channel"}],
                                   task="Post an approved containment decision and its judgment to the incident channel",
                                   ai_model="agentgeist-deterministic-escalation-officer")
            action = action_contract(docs)
            decision = result["decision"]
            message = (f"Agentgeist ({team}) | tick {tick.tick_id} | {decision['action']}\n"
                       f"{decision['rationale']}\n"
                       f"Score: {judgment.get('score', 'not supplied')}; false alarm: {judgment.get('false_alarm', 'not supplied')}. "
                       f"Peak agents: {result['peak_active_agents']}; decision time: {result.get('decision_seconds', 'n/a')}s.\n"
                       f"Sensor inspection order: {', '.join(result.get('sensor_inspection_priority', []))}")
            nonce = str(int(hashlib.sha256(f"{team}:{tick.tick_id}".encode()).hexdigest()[:16], 16))
            response = connection.call("execute_one_action", platform="discord", actionId=action, connectionKey=key,
                                       pathVariables={"channel_id": CHANNEL_ID, "channelId": CHANNEL_ID},
                                       data={"content": message[:1900], "allowed_mentions": {"parse": []},
                                             "nonce": nonce, "enforce_nonce": True})
            return message_receipt(response)
        finally:
            connection.close()
