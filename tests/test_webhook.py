import json
import logging
import sys
from argparse import Namespace
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
    assert ledger.peak == 1 and ledger.spent == 0 and ledger.remaining == 45000
    assert "test-token" not in caplog.text


def test_run_summary_uses_neutral_format_for_fixture_and_live():
    tick, result, _ = incident()
    run_id = "a" * 32
    fixture = notifier.run_notification_body("agentgeist", run_id, "fixture", "completed", tick, result)
    live = notifier.run_notification_body("agentgeist", run_id, "live", "completed", tick, result)
    assert fixture == live
    assert "RUN COMPLETE" in fixture["content"]
    assert "TEST" not in fixture["content"] and "FIXTURE" not in fixture["content"]
    assert "Score:" not in fixture["content"] and "judgment" in fixture["content"]
    assert fixture["allowed_mentions"] == {"parse": []}


def test_live_observe_run_posts_one_summary_without_judgment(monkeypatch, tmp_path):
    tick_data = json.loads(Path("fixtures/control-tick.json").read_text())["ticks"][0]

    class Scenario:
        def call(self, tool, **kwargs):
            if tool == "hack26-get-tick":
                return {"tick": tick_data}
            assert tool == "hack26-get-scoreboard"
            return {}
        def close(self):
            pass

    monkeypatch.setenv("SCENARIO_MCP", "mock://scenario")
    from agentgeist import connections, swarm
    monkeypatch.setattr(connections, "MCPConnection", lambda *args: Scenario())
    monkeypatch.setattr(swarm, "run_swarm", lambda tick, ledger: {
        "decision": safety_floor(tick).model_dump(), "peak_active_agents": 4,
    })
    calls = []
    def fake_notify(team, run_id, mode, status, ledger, **kwargs):
        calls.append((team, run_id, mode, status, kwargs))
        return {"status": "sent", "message_id": "123"}
    monkeypatch.setattr(notifier, "notify_run", fake_notify)

    report = cli.run(Namespace(team="agentgeist", db=str(tmp_path / "state.db"), scenario="control-tick",
                               ticks=1, submit=False, discord=False, discord_transport="webhook"))
    assert report["notification"] == {"status": "sent", "message_id": "123", "run_id": report["run_id"]}
    assert len(calls) == 1
    assert calls[0][2:4] == ("live", "completed")
    assert calls[0][4]["submission_requested"] is False
    assert calls[0][4]["result"]["decision"]["action"] == "observe"


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


def test_webhook_succeeds_with_no_incident_credits(webhook):
    calls = webhook(success)
    def forbidden_reservation(units):
        pytest.fail("Webhook must not reserve incident credits")
    ledger = Ledger(1, 0, on_reserve=forbidden_reservation)
    assert notifier.notify("agentgeist", *incident(), ledger)["status"] == "sent"
    assert [r.method for r in calls] == ["GET", "POST"]
    assert ledger.spent == 0 and ledger.remaining == 0 and ledger.active == 0


@pytest.mark.parametrize("failure", ["bad_url", "wrong_channel", "metadata_failure", "post_timeout"])
def test_repeated_webhook_failures_preserve_durable_credits(webhook, monkeypatch, tmp_path, failure):
    def handler(request):
        if failure == "metadata_failure":
            return httpx.Response(503)
        if failure == "wrong_channel":
            return httpx.Response(200, json={"channel_id": "wrong"})
        if request.method == "POST":
            raise httpx.ReadTimeout("lost receipt", request=request)
        return success(request)
    webhook(handler)
    if failure == "bad_url":
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "not-a-webhook")
    tick, result, judgment = incident()
    store = Store(str(tmp_path / "credits.db"))
    store.save_tick("agentgeist", tick)
    from agentgeist.cli import tick_ledger
    # Direct function calls model independent operator attempts. The CLI still guards unknown writes.
    for _ in range(3):
        ledger = tick_ledger(tick, "agentgeist", store)
        with pytest.raises((ValueError, RuntimeError)):
            notifier.notify("agentgeist", tick, result, judgment, ledger)
        assert ledger.spent == 0 and ledger.active == 0
    assert store.spent("agentgeist", tick.tick_id) == 0
    assert store.remaining("agentgeist") == tick.swarm_budget.api_credits_remaining


def test_one_attempt_keeps_credit_reservation(monkeypatch):
    monkeypatch.setattr(notifier, "notify_one", lambda body, callback: {"status": "sent", "delivery": "one"})
    ledger = Ledger(1, 1500)
    assert notifier.notify("agentgeist", *incident(), ledger, transport="one")["delivery"] == "one"
    assert ledger.spent == 1500 and ledger.remaining == 0


def test_one_cannot_exceed_credit_limit(monkeypatch):
    from agentgeist.budget import BudgetExceeded
    monkeypatch.setattr(notifier, "notify_one", lambda *args: pytest.fail("Unfunded One call"))
    ledger = Ledger(1, 1499)
    with pytest.raises(BudgetExceeded):
        notifier.notify("agentgeist", *incident(), ledger, transport="one")
    assert ledger.spent == 0 and ledger.remaining == 1499


def test_auto_fallback_charges_only_the_one_attempt(webhook, monkeypatch):
    webhook(success)
    def unavailable(body, callback):
        raise notifier.NoDiscordConnection()
    monkeypatch.setattr(notifier, "notify_one", unavailable)
    ledger = Ledger(1, 1500)
    assert notifier.notify("agentgeist", *incident(), ledger, transport="auto")["delivery"] == "webhook"
    assert ledger.spent == 1500 and ledger.remaining == 0


def test_auto_uses_webhook_when_one_budget_is_exhausted(webhook, monkeypatch):
    webhook(success)
    monkeypatch.setattr(notifier, "notify_one", lambda *args: pytest.fail("Unfunded One call"))
    ledger = Ledger(1, 0)
    assert notifier.notify("agentgeist", *incident(), ledger, transport="auto")["delivery"] == "webhook"
    assert ledger.spent == 0


def test_free_webhook_still_respects_agent_limit(webhook):
    from agentgeist.budget import BudgetExceeded
    calls = webhook(success)
    ledger = Ledger(0, 0)
    with pytest.raises(BudgetExceeded):
        notifier.notify("agentgeist", *incident(), ledger)
    assert not calls and ledger.active == 0 and ledger.spent == 0
