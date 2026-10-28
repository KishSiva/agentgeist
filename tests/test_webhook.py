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


def test_competition_message_includes_measured_metrics_and_real_judgment():
    tick, result, judgment = incident()
    tick.sector_reports.append(tick.sector_reports[0].model_copy(update={"sector": "entrance"}))
    result.update(decision_seconds=11.586, sensor_inspection_priority=["catering_coffee_bar", "entrance"])
    scored = notifier.notification_body("agentgeist", tick, result, judgment)
    summary = notifier.run_notification_body("agentgeist", "a" * 32, "live", "completed",
                                             tick, result, judgment=judgment)
    assert summary["content"].startswith(scored["content"])
    assert "Score: 1; false alarm: False. Peak agents: 4; decision time: 11.586s." in summary["content"]
    assert "Sensor inspection order: catering_coffee_bar, entrance" in summary["content"]
    assert "RUN COMPLETE" not in summary["content"]


def test_exhausted_run_never_invents_decision_metrics_or_a_score():
    body = notifier.run_notification_body("agentgeist", "a" * 32, "live", "completed")
    assert "no new tick: no decision was made" in body["content"]
    assert "Score: not supplied; false alarm: not supplied." in body["content"]
    assert "Peak agents: not available; decision time: not available." in body["content"]
    assert "Sensor inspection order: not available" in body["content"]


def test_competition_metrics_reject_untrusted_text_and_invalid_numbers():
    tick, result, _ = incident()
    result.update(peak_active_agents=True, decision_seconds=float("nan"),
                  sensor_inspection_priority=["@everyone", "entrance"])
    body = notifier.run_notification_body("agentgeist", "a" * 32, "fixture", "completed", tick, result,
                                          judgment={"score": float("inf"), "false_alarm": "@everyone"})
    assert "@everyone" not in body["content"]
    assert "decision time: not available" in body["content"]
    assert "Score: not supplied; false alarm: not supplied" in body["content"]


def test_run_summary_uses_neutral_format_for_fixture_and_live():
    tick, result, _ = incident()
    run_id = "a" * 32
    fixture = notifier.run_notification_body("agentgeist", run_id, "fixture", "completed", tick, result)
    live = notifier.run_notification_body("agentgeist", run_id, "live", "completed", tick, result)
    assert fixture == live
    assert "SIMULATION | tick 142 | evacuate" in fixture["content"]
    assert "Peak agents: 4; decision time: 1.5s." in fixture["content"]
    assert "Sensor inspection order: catering_coffee_bar" in fixture["content"]
    assert "TEST" not in fixture["content"] and "FIXTURE" not in fixture["content"]
    assert "Score: not supplied; false alarm: not supplied." in fixture["content"]
    assert "run summary, not a judgment" not in fixture["content"]
    assert fixture["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize("submit", [False, True])
def test_live_observe_run_posts_one_summary_with_available_judgment(monkeypatch, tmp_path, submit):
    tick_data = json.loads(Path("fixtures/control-tick.json").read_text())["ticks"][0]

    class Scenario:
        def call(self, tool, **kwargs):
            if tool == "hack26-get-tick":
                return {"tick": tick_data}
            if tool == "hack26-submit-containment":
                return {"score": 1, "false_alarm": False}
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
                               ticks=1, submit=submit, discord=False, discord_transport="webhook"))
    assert report["notification"] == {"status": "sent", "message_id": "123", "run_id": report["run_id"]}
    assert len(calls) == 1
    assert calls[0][2:4] == ("live", "completed")
    assert calls[0][4]["submission_requested"] is submit
    assert calls[0][4]["judgment"] == ({"score": 1, "false_alarm": False} if submit else None)
    assert calls[0][4]["result"]["decision"]["action"] == "observe"


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_live_failure_posts_one_failed_summary(monkeypatch, tmp_path, cleanup_failure):
    from agentgeist import connections

    class Scenario:
        def call(self, *args, **kwargs):
            if cleanup_failure:
                return {"exhausted": True}
            raise RuntimeError("private venue error")

        def close(self):
            if cleanup_failure:
                raise RuntimeError("private cleanup error")

    monkeypatch.setenv("SCENARIO_MCP", "mock://scenario")
    monkeypatch.setattr(connections, "MCPConnection", lambda *args: Scenario())
    messages = []

    def notify(team, run_id, mode, status, ledger, **kwargs):
        messages.append(notifier.run_notification_body(team, run_id, mode, status))
        return {"status": "sent", "message_id": "123"}

    monkeypatch.setattr(notifier, "notify_run", notify)
    with pytest.raises(RuntimeError) as caught:
        cli.run(Namespace(team="agentgeist", db=str(tmp_path / "state.db"), ticks=1,
                          scenario="outbreak-level-5", submit=True, discord=False, discord_transport="webhook"))
    assert "private venue error" not in str(caught.value)
    assert "private cleanup error" not in str(caught.value)
    assert caught.value.run_notification["message_id"] == "123"
    assert len(messages) == 1 and "RUN FAILED" in messages[0]["content"]
    assert "private venue error" not in messages[0]["content"]


@pytest.mark.parametrize("second_tick", ["failed", "duplicate"])
def test_second_tick_without_decision_never_borrows_previous_metrics(monkeypatch, tmp_path, second_tick):
    from agentgeist import connections, swarm
    first, first_result, _ = incident()
    second = first.model_copy(update={"tick_id": first.tick_id + 1}, deep=True)
    db = str(tmp_path / "two-ticks.sqlite3")
    if second_tick == "duplicate":
        store = Store(db)
        store.save_tick("agentgeist", second)
        store.update("agentgeist", second.tick_id, "decision", first_result)
        store.update("agentgeist", second.tick_id, "judgment", {"score": 0, "false_alarm": True})
        store.update("agentgeist", second.tick_id, "escalation", {"status": "sent"})
        store.db.close()
    feed = iter([{"tick": first.model_dump()}, {"tick": second.model_dump()}, {"exhausted": True}])

    class Scenario:
        def call(self, tool, **kwargs):
            if tool == "hack26-get-tick":
                return next(feed)
            if tool == "hack26-submit-containment":
                return {"score": 1, "false_alarm": False}
            assert tool == "hack26-get-scoreboard"
            return {}

        def close(self):
            pass

    def analyze(tick, ledger):
        if tick.tick_id == second.tick_id:
            raise RuntimeError("second tick failed")
        return dict(first_result)

    messages = []

    def notify(team, run_id, mode, status, ledger, **kwargs):
        messages.append((status, kwargs))
        return {"status": "sent", "message_id": "123"}

    monkeypatch.setenv("SCENARIO_MCP", "mock://scenario")
    monkeypatch.setattr(connections, "MCPConnection", lambda *args: Scenario())
    monkeypatch.setattr(swarm, "run_swarm", analyze)
    monkeypatch.setattr(notifier, "notify_run", notify)
    args = Namespace(team="agentgeist", db=db, scenario="outbreak-level-5", ticks=3,
                     submit=True, discord=False, discord_transport="webhook")
    if second_tick == "failed":
        with pytest.raises(RuntimeError):
            cli.run(args)
    else:
        cli.run(args)
    assert len(messages) == 1
    status, data = messages[0]
    assert data["tick"].tick_id == second.tick_id
    assert data["result"] is None and data["judgment"] is None
    body = notifier.run_notification_body("agentgeist", "a" * 32, "live", status,
                                          tick=data["tick"], result=data["result"], judgment=data["judgment"])
    assert "decision time: not available" in body["content"]
    assert "Score: not supplied; false alarm: not supplied" in body["content"]


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
