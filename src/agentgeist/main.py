"""CrewAI AMP Flow entry point. The default runs real agents on a saved fixture."""

import asyncio
import fcntl
import json
import os
import sys
import uuid
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from importlib.resources import files
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Load tracing switches before importing CrewAI's tracing machinery.
load_dotenv()

from crewai.flow import Flow, start  # noqa: E402

from agentgeist.contracts import Tick, team_handle  # noqa: E402


class RunOptions(BaseModel):
    mode: Literal["fixture", "live"] = "fixture"
    team: str = "agentgeist"
    scenario: Literal["outbreak-level-5", "control-tick"] = "outbreak-level-5"
    submit: bool = False
    # Every real app execution reports its outcome. This remains for input compatibility,
    # but false no longer disables the mandatory run-level summary.
    discord: bool = True

    @field_validator("team")
    @classmethod
    def normalize_team(cls, value):
        return team_handle(value)


class AgentgeistState(RunOptions):
    result: dict = Field(default_factory=dict)


def execute(options: RunOptions):
    run_id = uuid.uuid4().hex
    if options.mode == "fixture":
        from agentgeist.budget import Ledger
        from agentgeist.swarm import run_swarm
        data = json.loads(files("agentgeist").joinpath("fixtures", options.scenario + ".json").read_text())
        tick = Tick.model_validate(data["ticks"][0])
        result = None
        try:
            result = run_swarm(tick, Ledger(tick.swarm_budget.max_active_agents, tick.swarm_budget.api_credits_remaining))
        except Exception as error:
            from agentgeist.errors import RunFailed
            receipt = _fixture_notification(team=options.team, run_id=run_id, tick=tick, result=None,
                                            run_status="failed", transport=os.getenv("DISCORD_TRANSPORT", "webhook"))
            raise RunFailed(type(error).__name__, run_id, receipt) from None
        notification = _fixture_notification(team=options.team, run_id=run_id, tick=tick, result=result,
                                             run_status="completed", transport=os.getenv("DISCORD_TRANSPORT", "webhook"))
        return {"mode": "fixture", "run_id": run_id, "tick_id": tick.tick_id, "submitted": False,
                "discord_posted": notification["status"] == "sent", "run_notification": notification, **result}

    if os.getenv("AGENTGEIST_ENABLE_LIVE", "false").lower() != "true":
        raise ValueError("Set AGENTGEIST_ENABLE_LIVE=true only after configuring durable, single-worker state")
    from crewai.events import crewai_event_bus
    from crewai.events.event_listener import event_listener
    from crewai.events.listeners.tracing.utils import is_tracing_enabled_in_context
    from crewai.telemetry.tracing.context import get_trace_session

    # Venue MCP events contain credentials. Never run them inside a traced Flow.
    if (os.getenv("CREWAI_TRACING_ENABLED", "").lower() != "false"
            or is_tracing_enabled_in_context() or get_trace_session() is not None):
        raise ValueError("Live mode requires CREWAI_TRACING_ENABLED=false and no active CrewAI tracing")
    state_directory = os.getenv("AGENTGEIST_STATE_DIR", "")
    if not state_directory or not Path(state_directory).is_absolute():
        raise ValueError("Set AGENTGEIST_STATE_DIR to an absolute durable directory before live mode")

    from agentgeist.cli import run, run_summary
    from agentgeist.errors import RunFailed
    from agentgeist.store import Store
    directory = Path(state_directory)
    db = str(directory / "agentgeist.sqlite3")
    args = Namespace(team=options.team, db=db, scenario=options.scenario, ticks=1,
                     submit=options.submit, discord=options.discord, run_id=run_id,
                     discord_transport=os.getenv("DISCORD_TRANSPORT", "webhook"))
    report = {}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(db + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # MCP console events include authenticated URLs and tool arguments.
            with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                try:
                    report = run(args) or {}
                finally:
                    pending_error = sys.exc_info()[1]
                    event_listener.formatter.verbose = False
                    if not crewai_event_bus.flush() and pending_error is None:
                        raise RuntimeError("Live console event shutdown timed out")
            store = Store(db)
            try:
                rows = store.db.execute("SELECT tick_id,judgment,escalation,status FROM incidents WHERE team=? ORDER BY tick_id DESC LIMIT 20",
                                        (options.team,)).fetchall()
                saved = [{"tick_id": row[0], "judgment": json.loads(row[1]) if row[1] else None,
                          "escalation": json.loads(row[2]) if row[2] else None, "status": row[3]} for row in rows]
            finally:
                store.db.close()
    except RunFailed:
        raise
    except Exception as error:
        # HTTP exceptions can contain authenticated URLs; do not expose their text.
        receipt = report.get("notification") or run_summary(options.team, run_id, None, None, False,
                                                            bool(options.submit), args.discord_transport)
        raise RunFailed(type(error).__name__, run_id, receipt) from None
    return {"mode": "live", "team": options.team, "run_id": run_id,
            "run_notification": report.get("notification", {"status": "failed", "run_id": run_id}),
            "saved_incidents": saved}


def _fixture_notification(team, run_id, tick, result, run_status, transport):
    """Fixture receipts are returned with the Flow result, never written as venue records."""
    from agentgeist.budget import Ledger
    from agentgeist.notifier import notify_run
    receipt = {"status": "failed", "run_id": run_id}

    def before_send():
        receipt.update(status="unknown")

    try:
        delivered = notify_run(team, run_id, "fixture", run_status, Ledger(1, 0), tick=tick, result=result,
                               before_send=before_send, transport=transport)
    except Exception:
        return receipt
    return {**delivered, "run_id": run_id}


class AgentgeistFlow(Flow[AgentgeistState]):
    @start()
    async def respond(self, crewai_trigger_payload: dict | None = None):
        raw = crewai_trigger_payload if crewai_trigger_payload is not None else self.state.model_dump()
        options = RunOptions.model_validate(raw)
        for key, value in options.model_dump().items():
            setattr(self.state, key, value)
        # MCP resolution uses synchronous event-loop helpers. Keep it outside the Flow loop.
        self.state.result = await asyncio.to_thread(execute, options)
        return self.state.result


def kickoff():
    """Standard `crewai run` entry point: real provider calls, no venue side effects."""
    tracing = os.getenv("CREWAI_TRACING_ENABLED", "").lower()
    flow = AgentgeistFlow(tracing=tracing == "true" if tracing in ("true", "false") else None)
    print(json.dumps(flow.kickoff()))


def run_with_trigger():
    """Standard AMP trigger entry point. The platform supplies a JSON payload."""
    if len(sys.argv) != 2:
        raise ValueError("Provide exactly one JSON trigger payload")
    payload = json.loads(sys.argv[1])
    if not isinstance(payload, dict):
        raise ValueError("Trigger payload must be a JSON object")
    RunOptions.model_validate(payload)
    tracing = os.getenv("CREWAI_TRACING_ENABLED", "").lower()
    flow = AgentgeistFlow(tracing=tracing == "true" if tracing in ("true", "false") else None)
    print(json.dumps(flow.kickoff(inputs={"crewai_trigger_payload": payload})))


if __name__ == "__main__":
    kickoff()
