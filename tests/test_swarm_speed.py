import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from agentgeist import swarm
from agentgeist.budget import BudgetExceeded, Ledger
from agentgeist.contracts import Tick, safety_floor
from agentgeist.store import Store


def tick(name="outbreak-level-5"):
    return Tick.model_validate(json.loads(Path(f"fixtures/{name}.json").read_text())["ticks"][0])


def fake_swarm(monkeypatch, execute=None):
    state = SimpleNamespace(agents=[], closed=False, finished=[], tasks=[], retrievals=[])

    class Partners:
        def __init__(self, ledger):
            self.ledger = ledger

        def embeddings(self, texts):
            state.retrievals.append("embeddings")
            if getattr(state, "retrieval_gate", None):
                state.retrieval_gate.wait()
            return [[1.0, 0.0] for _ in texts]

        def research(self, entities):
            state.retrievals.append("research")
            if getattr(state, "retrieval_gate", None):
                state.retrieval_gate.wait()
            return {"sources": [], "untrusted": True}

        def close(self):
            state.closed = True

    class Agent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            state.agents.append(self)

        def execute_task(self, task):
            state.tasks.append(task)
            if execute:
                execute(self)
            if self.role == "Incident commander":
                assert len(state.finished) == 3
                return state.commander_answer
            # A repeated tool selection must use the already cached evidence.
            self.tools[0]._run()
            state.finished.append(self.role)
            return "report: " + self.role

    monkeypatch.setenv("PARASAIL_API_KEY", "unit-test")
    monkeypatch.setattr(swarm, "Partners", Partners)
    monkeypatch.setattr(swarm, "Agent", Agent)
    monkeypatch.setattr(swarm, "Task", SimpleNamespace)
    monkeypatch.setattr(swarm, "LLM", lambda **kwargs: SimpleNamespace(**kwargs))
    state.commander_answer = json.dumps({"action": "evacuate", "rationale": "corroborated danger"})
    return state


def test_parallel_specialists_keep_context_order_scope_and_distinct_clients(monkeypatch):
    ready = Barrier(3, timeout=5)
    context = ContextVar("execution", default=None)
    seen = []

    def execute(agent):
        if agent.role != "Incident commander":
            seen.append(context.get())
            ready.wait()

    state = fake_swarm(monkeypatch, execute)
    state.retrieval_gate = Barrier(2, timeout=5)
    token = context.set("flow-context")
    ledger = Ledger(4, 45000)
    try:
        result = swarm.run_swarm(tick(), ledger)
    finally:
        context.reset(token)
    assert seen == ["flow-context"] * 3
    assert len({id(a.llm.inner) for a in state.agents}) == 4
    assert all(a.llm.inner.max_tokens == 256 for a in state.agents)
    assert all(a.llm.ledger is ledger for a in state.agents)
    assert state.retrievals.count("embeddings") == state.retrievals.count("research") == 1
    assert all(not a.allow_delegation for a in state.agents)
    assert all("do not call tools again" in a.prompt_template.lower() for a in state.agents)
    commander_task = next(t for t in state.tasks if t.agent.role == "Incident commander")
    reports = json.loads(commander_task.description.split("Reports: ", 1)[1])
    assert [r["role"] for r in reports] == [a.role for a in state.agents[:3]]
    assert "sensor_signatures" not in commander_task.description
    assert "ground_sightings" not in commander_task.description
    assert state.closed and ledger.active == 0 and ledger.peak == 4
    assert result["model_decision_valid"] is True
    assert result["decision_seconds"] >= result["timings"]["commander_seconds"] >= 0
    assert len(result["timings"]["specialists"]) == 3


def test_failure_drains_running_workers_before_closing_clients_and_releasing_slots(monkeypatch):
    ready = Barrier(3, timeout=5)
    failed, release = Event(), Event()

    def execute(agent):
        if agent.role == "Incident commander":
            pytest.fail("Commander must not run after a specialist failure")
        ready.wait()
        if agent.role == "Sensor analyst":
            failed.set()
            raise RuntimeError("specialist failure")
        assert release.wait(5)

    state = fake_swarm(monkeypatch, execute)
    ledger = Ledger(4, 45000)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(swarm.run_swarm, tick(), ledger)
        try:
            assert failed.wait(5)
            assert not state.closed and ledger.active == 4
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="specialist failure"):
            future.result(timeout=5)
    assert len(state.finished) == 2
    assert state.closed and ledger.active == 0


@pytest.mark.parametrize("name,answer", [
    ("control-tick", '{"action":"evacuate","rationale":"wrong"}'),
    ("outbreak-level-5", "invalid JSON"),
    ("outbreak-level-5", '```json\n{"action":"evacuate","rationale":"danger"}\n```'),
])
def test_parallel_swarm_preserves_final_decisions(monkeypatch, name, answer):
    state = fake_swarm(monkeypatch)
    state.commander_answer = answer
    data = tick(name)
    result = swarm.run_swarm(data, Ledger(4, 45000))
    assert result["decision"]["action"] == safety_floor(data).action
    assert result["model_decision_valid"] == (answer != "invalid JSON")


def test_concurrent_tool_calls_retrieve_evidence_once():
    started, release = Event(), Event()
    calls = []

    def callback():
        calls.append(True)
        started.set()
        assert release.wait(5)
        return {"value": 1}

    evidence = {}
    tool = swarm.ScopedTool(name="cached", description="unit", callback=callback, evidence=evidence)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(tool._run) for _ in range(8)]
        try:
            assert started.wait(5)
        finally:
            release.set()
        assert [f.result(timeout=5) for f in futures] == ['{"value": 1}'] * 8
    assert calls == [True] and evidence == {"cached": {"value": 1}}


def test_concurrent_reservations_respect_shared_and_durable_budget(tmp_path):
    store = Store(str(tmp_path / "budget.sqlite3"))
    data = tick()
    store.save_tick("agentgeist", data)
    ledger = Ledger(4, 5, on_reserve=lambda units: store.reserve("agentgeist", data.tick_id, units))

    def reserve(_):
        try:
            ledger.reserve(1)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=10) as executor:
        assert sum(executor.map(reserve, range(20))) == 5
    assert ledger.spent == store.spent("agentgeist", data.tick_id) == 5
    assert ledger.remaining == 0
    store.db.close()
