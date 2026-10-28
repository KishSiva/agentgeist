# Agentgeist — The Exorcism Protocol

A supervised CrewAI hierarchy for Track 1. The team display name is **Agentgeist**.
The scenario API requires the lowercase handle `agentgeist`.

## Run

Use Python 3.10–3.13 and Node.js with `npx`.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
cp .env.example .env
# Fill the credentials in .env. Never commit .env.
.venv/bin/agentgeist demo
.venv/bin/agentgeist demo --fixture fixtures/control-tick.json
.venv/bin/agentgeist scoreboard
# Real CrewAI and provider calls; pulls one real tick but does not submit it:
.venv/bin/agentgeist run
# Explicit scored submissions and public Discord incident evidence:
.venv/bin/agentgeist run --ticks 2 --submit --discord
```

`demo` is an **offline policy demonstration**, not a simulated successful partner integration.
The fixtures come from the event's published fixture files. Demo mode never submits a decision.
`run` uses real providers. It stops if required specialist evidence is missing.

## Partial knowledge and supervision

| Role | Private input | Assigned tool |
|---|---|---|
| Sensor analyst | Sensors from every sector | Parasail embeddings and signature comparison |
| Sighting researcher | Sightings from every sector | You.com Search and Contents |
| Civilian marshal | Civilian status and containment state | Local civilian triage |
| Incident commander | Specialist reports | CrewAI hierarchical delegation |
| Escalation officer | Approved decision and judgment only | One MCP Discord action |

The three specialists do not receive the full tick. A mandatory sensing phase executes each specialist's tool before its model analysis.
The commander then coordinates their reports through a CrewAI hierarchical crew.
The supervisor counts all four CrewAI agents before work starts. The escalation officer runs after the crew finishes.
Each specialist must actually use its assigned tool. The final decision includes tool evidence and any supervisor override.

The supervisor checks every sector. It prioritizes civilians at risk in a breached or corroborated dangerous sector.
It requires multiple physical indicators plus a credible sighting or breached containment before containment without evacuation.
It treats crowd motion and folklore alone as insufficient evidence. These transparent simulation thresholds are not real emergency guidance.

## Budget and failure behavior

- The active-agent gate rejects a tick with fewer than four available slots before model or partner calls.
- The shared ledger reserves credits before each model, embedding, research, and escalation call.
- The credit unit is a conservative local token-equivalent estimate, **not verified venue billing**. The feed does not document a monetary conversion.
- Model reservations use twice the cl100k input token count plus an output allowance. Automatic model retries are disabled.
- Each specialist and the commander gets one model iteration. Invalid commander JSON triggers an explicit, recorded safety-policy fallback.
- HTTP requests have bounded timeouts. A failed tick remains in the durable inbox.
- SQLite saves each tick before inference and each decision before submission.
- A restart processes pending ticks before it advances the team cursor.
- Submission replay uses the original tick and action. The feed documents one accepted answer per team per tick.
- Discord delivery requires a returned message identifier. An ambiguous delivery stays pending for manual inspection.
- `observe` does not post an incident alert. This reduces venue noise and false alarms.

The default database is `state/agentgeist.sqlite3`. Keep this database between runs.
Do not run multiple CLI processes for the same team at once.

## Connections

The scenario connection uses `MCPServerHTTP(url=..., streamable=True, headers={})`, not URL shorthand.
The scenario endpoint normally needs no key. Set `SCENARIO_TOKEN` only if organizers enable team tokens.

One's hosted endpoint uses OAuth, not an API key as a bearer token.
This project uses the official `@withone/mcp@2.3.2` stdio server with `ONE_SECRET=ONE_API_KEY`.
It discovers the Discord action and its request contract before it sends anything.
No Discord bot token or direct webhook bypass is required.

The configured `ONE_MCP_URL` remains available for an OAuth-capable client, but this CLI does not use it with the API key.
The default models were listed by the live Parasail catalog during setup. Override the model names in `.env` if needed.
Search and Contents use `X-API-Key`. CrewAI tracing is disabled by default.

## Verify

```bash
.venv/bin/python -m pytest -q
```

Source guide: https://youdotcom-oss.github.io/halloween-hack-2026/
