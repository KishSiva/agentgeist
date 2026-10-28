import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

from agentgeist import cli, notifier
from agentgeist.budget import Ledger
from agentgeist.contracts import Tick, safety_floor
from agentgeist.store import Store


URL = "https://discord.com/api/webhooks/123/test-token"
CHANNEL = notifier.CHANNEL_ID


def incident():
    tick = Tick.model_validate(json.loads(Path("fixtures/outbreak-level-5.json").read_text())["ticks"][0])
    result = {"decision": safety_floor(tick).model_dump(), "peak_active_agents": 4,
              "decision_seconds": 1.5, "sensor_inspection_priority": ["catering_coffee_bar"]}
    return tick, result, {"score": 1, "false_alarm": False}


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    monkeypatch.setenv("DISCORD_TRANSPORT", "webhook")
    original = httpx.Client
    calls = []

    def install(handler):
        def capture(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(notifier.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(capture), **kw))
        return calls

    return install


def success(request):
    if request.method == "GET":
        return httpx.Response(200, json={"channel_id": CHANNEL, "guild_id": "1234"})
    return httpx.Response(200, json={"channel_id": CHANNEL, "id": "5678"})


@pytest.mark.parametrize("url", ["", "http://discord.com/api/webhooks/1/token",
    "https://example.com/api/webhooks/1/token", "https://discord.com.evil.test/api/webhooks/1/token",
    "https://user:pass@discord.com/api/webhooks/1/token", URL + "?wait=true", URL + "#fragment",
    URL + "/messages/123", "https://discord.com:443/api/webhooks/1/token"])
def test_rejects_unsafe_url(monkeypatch, url):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", url)
    with pytest.raises(ValueError) as error:
        notifier.webhook_url()
    assert "test-token" not in str(error.value)


def test_webhook_delivery_verifies_channel_and_receipt(webhook, monkeypatch, caplog):
    calls = webhook(success)
    monkeypatch.delenv("ONE_API_KEY", raising=False)
    monkeypatch.setattr(notifier, "MCPConnection", lambda **kw: pytest.fail("Webhook must not use One"))
    before = []
    ledger = Ledger(8, 45000)
    with caplog.at_level(logging.INFO, logger="httpx"):
        receipt = notifier.notify("agentgeist", *incident(), ledger, before_send=lambda: before.append(True))
    assert [r.method for r in calls] == ["GET", "POST"]
    assert calls[-1].url.params["wait"] == "true"
    body = json.loads(calls[-1].content)
    assert body["allowed_mentions"] == {"parse": []}
    assert "SIMULATION" in body["content"]
    assert "nonce" not in body
    assert len(before) == 1
    assert receipt["delivery"] == "webhook"
    assert receipt["message_url"] == f"https://discord.com/channels/1234/{CHANNEL}/5678"
    assert ledger.peak == 1 and ledger.spent == 1500
    assert "test-token" not in caplog.text


def test_wrong_target_never_posts(webhook):
    calls = webhook(lambda request: httpx.Response(200, json={"channel_id": "wrong"}))
    before = []
    with pytest.raises(RuntimeError):
        notifier.notify("agentgeist", *incident(), Ledger(8, 45000), before_send=lambda: before.append(True))
    assert len(calls) == 1 and calls[0].method == "GET"
    assert not before


def test_auto_prefers_one(monkeypatch):
    monkeypatch.setenv("DISCORD_TRANSPORT", "auto")
    monkeypatch.setattr(notifier, "notify_one", lambda body, cb: {"delivery": "one"})
    monkeypatch.setattr(notifier, "notify_webhook", lambda *a: pytest.fail("One succeeded"))
    assert notifier.notify("agentgeist", *incident(), Ledger(8, 45000))["delivery"] == "one"


def test_auto_falls_back_only_before_send(webhook, monkeypatch):
    webhook(success)
    monkeypatch.setenv("DISCORD_TRANSPORT", "auto")
    def unavailable(body, callback):
        raise notifier.NoDiscordConnection()
    monkeypatch.setattr(notifier, "notify_one", unavailable)
    assert notifier.notify("agentgeist", *incident(), Ledger(8, 45000))["delivery"] == "webhook"


def test_ambiguous_one_send_never_falls_back(webhook, monkeypatch):
    calls = webhook(success)
    monkeypatch.setenv("DISCORD_TRANSPORT", "auto")
    def uncertain(body, callback):
        callback()
        raise RuntimeError("Response lost")
    monkeypatch.setattr(notifier, "notify_one", uncertain)
    with pytest.raises(RuntimeError):
        notifier.notify("agentgeist", *incident(), Ledger(8, 45000))
    assert calls == []


def test_one_mode_never_falls_back(webhook, monkeypatch):
    calls = webhook(success)
    def unavailable(body, callback):
        raise notifier.NoDiscordConnection()
    monkeypatch.setattr(notifier, "notify_one", unavailable)
    with pytest.raises(notifier.NoDiscordConnection):
        notifier.notify("agentgeist", *incident(), Ledger(8, 45000), transport="one")
    assert not calls


@pytest.mark.parametrize("failure", ["timeout", "no_receipt", "wrong_receipt", "rejected"])
def test_uncertain_post_stays_unknown_and_does_not_retry(webhook, tmp_path, failure):
    def handler(request):
        if request.method == "GET":
            return success(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret URL must not escape", request=request)
        if failure == "no_receipt":
            return httpx.Response(204)
        if failure == "rejected":
            return httpx.Response(403)
        return httpx.Response(200, json={"id": "5678", "channel_id": "wrong"})
    calls = webhook(handler)
    tick, result, judgment = incident()
    store = Store(str(tmp_path / "state.db"))
    store.save_tick("agentgeist", tick)
    store.update("agentgeist", tick.tick_id, "decision", result)
    store.update("agentgeist", tick.tick_id, "judgment", judgment)
    from argparse import Namespace
    args = Namespace(discord=True, discord_transport="webhook")
    cli.escalate(tick, result, judgment, args, "agentgeist", store, notifier.notify)
    assert json.loads(store.get("agentgeist", tick.tick_id)["escalation"])["status"] == "unknown"
    cli.escalate(tick, result, judgment, args, "agentgeist", store, notifier.notify)
    assert sum(r.method == "POST" for r in calls) == 1


def test_notify_cli_posts_saved_judgment_once_without_scenario(webhook, monkeypatch, tmp_path):
    calls = webhook(success)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(notifier, "MCPConnection", lambda **kw: pytest.fail("No One or scenario call expected"))
    tick, result, judgment = incident()
    db = str(tmp_path / "saved.db")
    store = Store(db)
    store.save_tick("agentgeist", tick)
    store.update("agentgeist", tick.tick_id, "decision", result)
    store.update("agentgeist", tick.tick_id, "judgment", judgment)
    monkeypatch.setattr(sys, "argv", ["agentgeist", "--db", db, "notify", "--tick-id", str(tick.tick_id)])
    cli.main()
    cli.main()
    assert sum(r.method == "POST" for r in calls) == 1
    assert json.loads(store.get("agentgeist", tick.tick_id)["escalation"])["delivery"] == "webhook"
