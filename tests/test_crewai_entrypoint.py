import asyncio
import json
import sys
import threading
from importlib.resources import files

import pytest
from pydantic import ValidationError

from agentgeist import cli, main, swarm
from agentgeist.budget import Ledger
from agentgeist.contracts import Tick


def test_packaged_fixtures_are_available():
    for name in ("outbreak-level-5", "control-tick"):
        data = json.loads(files("agentgeist").joinpath("fixtures", name + ".json").read_text())
        assert Tick.model_validate(data["ticks"][0]).sector_reports


def test_flow_defaults_do_not_submit_or_post():
    options = main.RunOptions()
    assert options.mode == "fixture"
    assert options.submit is False and options.discord is False
    assert main.RunOptions(team="Agentgeist").team == "agentgeist"


@pytest.mark.parametrize("options", [{"mode": "unknown"}, {"team": "x"}, {"scenario": "unknown"}])
def test_invalid_flow_options_are_rejected(options):
    with pytest.raises(ValidationError):
        main.RunOptions.model_validate(options)


def test_fixture_mode_uses_real_swarm_interface_without_venue_effects(monkeypatch):
    def fake_swarm(tick, ledger):
        assert isinstance(tick, Tick) and isinstance(ledger, Ledger)
        return {"decision": {"action": "evacuate", "rationale": "fixture"}}
    monkeypatch.setattr(swarm, "run_swarm", fake_swarm)
    monkeypatch.setattr(cli, "run", lambda *a: pytest.fail("Fixture must not run live CLI"))
    result = main.execute(main.RunOptions(submit=True, discord=True))
    assert result["submitted"] is False and result["discord_posted"] is False


def test_live_mode_requires_explicit_environment_permission(monkeypatch):
    monkeypatch.delenv("AGENTGEIST_ENABLE_LIVE", raising=False)
    monkeypatch.setattr(cli, "run", lambda *a: pytest.fail("Live mode is not enabled"))
    with pytest.raises(ValueError, match="AGENTGEIST_ENABLE_LIVE"):
        main.execute(main.RunOptions(mode="live"))


def test_live_mode_preserves_cli_flags_and_bounded_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTGEIST_ENABLE_LIVE", "true")
    monkeypatch.setenv("AGENTGEIST_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    monkeypatch.setenv("DISCORD_TRANSPORT", "webhook")
    def fake_run(args):
        assert args.ticks == 1 and args.submit is True and args.discord is True
        assert args.discord_transport == "webhook" and args.team == "agentgeist"
    monkeypatch.setattr(cli, "run", fake_run)
    result = main.execute(main.RunOptions(mode="live", submit=True, discord=True))
    assert result == {"mode": "live", "team": "agentgeist", "saved_incidents": []}


def test_flow_keeps_synchronous_mcp_work_outside_event_loop(monkeypatch):
    original_thread = threading.get_ident()
    def fake_execute(options):
        assert threading.get_ident() != original_thread
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return {"mode": options.mode, "ok": True}
    monkeypatch.setattr(main, "execute", fake_execute)
    flow = main.AgentgeistFlow(tracing=False)
    assert flow.kickoff(inputs={"crewai_trigger_payload": {"mode": "fixture"}}) == {"mode": "fixture", "ok": True}


def test_model_serialization_excludes_authenticated_client():
    llm = swarm.BudgetLLM(model="test", inner={"api_key": "private-model-key"}, ledger=Ledger(4, 10))
    result = llm.model_dump_json()
    assert "private-model-key" not in result
    assert "inner" not in llm.model_dump() and "ledger" not in llm.model_dump()


@pytest.mark.parametrize("payload", ["[]", "{invalid", '{"mode":"unknown"}'])
def test_trigger_rejects_invalid_input_before_kickoff(monkeypatch, payload):
    monkeypatch.setattr(sys, "argv", ["run_with_trigger", payload])
    monkeypatch.setattr(main, "AgentgeistFlow", lambda: pytest.fail("Invalid payload must not start a Flow"))
    with pytest.raises(ValueError):
        main.run_with_trigger()


def test_console_kickoff_returns_none_and_prints_json(monkeypatch, capsys):
    class FakeFlow:
        def __init__(self, **kwargs):
            assert kwargs["tracing"] is True
        def kickoff(self):
            return {"mode": "fixture", "submitted": False}
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "true")
    monkeypatch.setattr(main, "AgentgeistFlow", FakeFlow)
    assert main.kickoff() is None
    assert json.loads(capsys.readouterr().out)["submitted"] is False


def test_trigger_console_returns_none_and_forwards_input(monkeypatch, capsys):
    class FakeFlow:
        def __init__(self, **kwargs):
            pass
        def kickoff(self, inputs):
            assert inputs["crewai_trigger_payload"] == {"mode": "fixture"}
            return {"ok": True}
    monkeypatch.setattr(sys, "argv", ["run_with_trigger", '{"mode":"fixture"}'])
    monkeypatch.setattr(main, "AgentgeistFlow", FakeFlow)
    assert main.run_with_trigger() is None
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize("tracing", [None, "true"])
def test_live_mode_requires_tracing_explicitly_disabled(monkeypatch, tmp_path, tracing):
    monkeypatch.setenv("AGENTGEIST_ENABLE_LIVE", "true")
    monkeypatch.setenv("AGENTGEIST_STATE_DIR", str(tmp_path))
    if tracing is None:
        monkeypatch.delenv("CREWAI_TRACING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CREWAI_TRACING_ENABLED", tracing)
    monkeypatch.setattr(cli, "run", lambda *a: pytest.fail("Traced live execution must not start"))
    with pytest.raises(ValueError, match="CREWAI_TRACING_ENABLED=false"):
        main.execute(main.RunOptions(mode="live"))


@pytest.mark.parametrize("active", ["legacy", "session"])
def test_live_mode_rejects_active_trace_context(monkeypatch, tmp_path, active):
    from crewai.events.listeners.tracing import utils
    from crewai.telemetry.tracing import context
    monkeypatch.setenv("AGENTGEIST_ENABLE_LIVE", "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    monkeypatch.setenv("AGENTGEIST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "is_tracing_enabled_in_context", lambda: active == "legacy")
    monkeypatch.setattr(context, "get_trace_session", lambda: object() if active == "session" else None)
    monkeypatch.setattr(cli, "run", lambda *a: pytest.fail("Active trace context must block live mode"))
    with pytest.raises(ValueError, match="no active CrewAI tracing"):
        main.execute(main.RunOptions(mode="live"))


@pytest.mark.parametrize("directory", [None, "state"])
def test_live_mode_requires_explicit_absolute_state_directory(monkeypatch, directory):
    monkeypatch.setenv("AGENTGEIST_ENABLE_LIVE", "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    if directory is None:
        monkeypatch.delenv("AGENTGEIST_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("AGENTGEIST_STATE_DIR", directory)
    monkeypatch.setattr(cli, "run", lambda *a: pytest.fail("Unconfigured state must block live mode"))
    with pytest.raises(ValueError, match="AGENTGEIST_STATE_DIR"):
        main.execute(main.RunOptions(mode="live"))


def test_live_mode_sanitizes_failures(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTGEIST_ENABLE_LIVE", "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    monkeypatch.setenv("AGENTGEIST_STATE_DIR", str(tmp_path))
    def fail(args):
        raise ValueError("private-authenticated-url")
    monkeypatch.setattr(cli, "run", fail)
    with pytest.raises(RuntimeError) as caught:
        main.execute(main.RunOptions(mode="live"))
    assert str(caught.value) == "Live execution failed: ValueError"
    assert caught.value.__suppress_context__ is True
