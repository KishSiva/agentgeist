import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .budget import Ledger
from .contracts import Tick, safety_floor, team_handle
from .store import Store


def emit(event, **data):
    print(json.dumps({"event": event, **data}, default=str), flush=True)


def auth(team):
    result = {"team": team}
    if os.getenv("SCENARIO_TOKEN"):
        result["token"] = os.environ["SCENARIO_TOKEN"]
    return result


def submit(scenario, store, team, tick, result):
    # Persist the exact action before the network call. Retries are server-idempotent per tick.
    store.update(team, tick.tick_id, "decision", result)
    judgment = scenario.call("hack26-submit-containment", **auth(team),
                             tick_id=tick.tick_id, action=result["decision"]["action"])
    if not isinstance(judgment, dict):
        raise RuntimeError("Unexpected judgment format; retain outbox for inspection")
    store.update(team, tick.tick_id, "judgment", judgment)
    emit("judgment", tick_id=tick.tick_id, judgment=judgment)
    return judgment


def run(args):
    from .connections import MCPConnection
    from .swarm import run_swarm
    from .notifier import notify
    team = team_handle(args.team)
    store = Store(args.db)
    scenario = MCPConnection(os.environ["SCENARIO_MCP"])
    try:
        # Resume durable inbox/outbox first; never pull over an unfinished tick.
        for row in store.pending(team):
            if row["escalation"] and json.loads(row["escalation"]).get("status") == "unknown":
                raise RuntimeError("Prior Discord send is ambiguous. Inspect the channel and local outbox before retrying.")
            if not row["decision"]:
                process_tick(Tick.model_validate_json(row["tick"]), args, team, store, scenario, run_swarm, notify)
            elif args.submit:
                tick = Tick.model_validate_json(row["tick"])
                result = json.loads(row["decision"])
                judgment = json.loads(row["judgment"]) if row["judgment"] else submit(scenario, store, team, tick, result)
                escalate(tick, result, judgment, args, team, store, notify)
            else:
                raise RuntimeError("A decision awaits submission. Use run --submit to replay it before pulling another tick.")
        for _ in range(args.ticks):
            data = scenario.call("hack26-get-tick", **auth(team), scenario=args.scenario)
            if data.get("exhausted"):
                emit("feed_exhausted")
                break
            tick = Tick.model_validate(data.get("tick", data))
            store.save_tick(team, tick)
            process_tick(tick, args, team, store, scenario, run_swarm, notify)
            if not args.submit:
                break  # Leave one unsubmitted decision in the durable outbox.
        emit("scoreboard", data=scenario.call("hack26-get-scoreboard"))
    finally:
        scenario.close()


def process_tick(tick, args, team, store, scenario, run_swarm, notify):
    start = time.monotonic()
    ledger = tick_ledger(tick, team, store)
    result = run_swarm(tick, ledger)
    result["decision_seconds"] = round(time.monotonic() - start, 3)
    result["credits_reserved"] = store.spent(team, tick.tick_id)
    store.update(team, tick.tick_id, "decision", result)
    emit("decision", tick_id=tick.tick_id, **{k:v for k,v in result.items() if k != "evidence"})
    if args.submit:
        judgment = submit(scenario, store, team, tick, result)
        escalate(tick, result, judgment, args, team, store, notify, ledger)


def escalate(tick, result, judgment, args, team, store, notify, ledger=None):
    if result["decision"]["action"] == "observe":
        store.update(team, tick.tick_id, "escalation", {"status": "not_required"})
        return
    if not args.discord:
        emit("escalation_pending", tick_id=tick.tick_id, reason="Enable --discord to post through One MCP")
        return
    ledger = ledger or tick_ledger(tick, team, store)
    store.update(team, tick.tick_id, "escalation", {"status": "unknown"})
    try:
        receipt = notify(team, tick, result, judgment, ledger)
        store.update(team, tick.tick_id, "escalation", receipt)
        emit("discord_receipt", tick_id=tick.tick_id, receipt=receipt)
    except Exception as exc:
        from .notifier import NoDiscordConnection
        if isinstance(exc, NoDiscordConnection):
            store.update(team, tick.tick_id, "escalation", {"status": "pending", "reason": "No selected writable Discord connection"})
        # A Discord failure must not lose an accepted containment judgment.
        emit("escalation_pending", tick_id=tick.tick_id, error=type(exc).__name__)
        raise RuntimeError("Discord delivery remains unverified; do not retry blindly after an ambiguous send") from None


def tick_ledger(tick, team, store):
    return Ledger(tick.swarm_budget.max_active_agents,
                  max(0, tick.swarm_budget.api_credits_remaining - store.spent(team, tick.tick_id)),
                  on_reserve=lambda units: store.reserve(team, tick.tick_id, units))


def main():
    load_dotenv()
    os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    parser = argparse.ArgumentParser(description="Agentgeist supervised containment swarm")
    parser.add_argument("--team", default=os.getenv("TEAM_HANDLE", "agentgeist"))
    parser.add_argument("--db", default="state/agentgeist.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    offline = commands.add_parser("demo", help="Offline policy demonstration; no model or provider claims")
    offline.add_argument("--fixture", default="fixtures/outbreak-level-5.json")
    live = commands.add_parser("run", help="Pull real ticks and run the CrewAI hierarchy")
    live.add_argument("--scenario", choices=["outbreak-level-5", "control-tick"], default="outbreak-level-5")
    live.add_argument("--ticks", type=int, choices=range(1, 21), default=1)
    live.add_argument("--submit", action="store_true", help="Explicitly enable scored submissions")
    live.add_argument("--discord", action="store_true", help="Enable incident posting through One MCP")
    commands.add_parser("scoreboard", help="Read-only live scoreboard")
    args = parser.parse_args()
    try:
        if args.command == "demo":
            for data in json.loads(Path(args.fixture).read_text())["ticks"]:
                tick = Tick.model_validate(data)
                emit("OFFLINE_POLICY_DEMO", tick_id=tick.tick_id, decision=safety_floor(tick).model_dump(),
                     live_services_used=False, scored=False)
        elif args.command == "scoreboard":
            from .connections import MCPConnection
            c = MCPConnection(os.environ["SCENARIO_MCP"])
            try:
                emit("scoreboard", data=c.call("hack26-get-scoreboard"))
            finally:
                c.close()
        else:
            Path(args.db).parent.mkdir(parents=True, exist_ok=True)
            with open(args.db + ".lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                run(args)
    except Exception as exc:
        # Exceptions from HTTP libraries can include authenticated URLs. Print types only.
        emit("error", type=type(exc).__name__, message="Operation stopped. Inspect local state before retrying.")
        sys.exit(1)


if __name__ == "__main__":
    main()
