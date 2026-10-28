# CrewAI tracing and website deployment

## Connect local tracing

Use the same Python environment as Agentgeist. Install `uv` before setup: https://docs.astral.sh/uv/getting-started/installation/
Keep the environment on `PATH` so CrewAI can find `uv`:

```bash
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/crewai login
.venv/bin/crewai org list
```

Complete the device authorization in your browser. Select the intended workspace.
Set these values in the ignored `.env` file, not GitHub:

```dotenv
CREWAI_TRACING_ENABLED=true
OTEL_SDK_DISABLED=false
```

Run the standard Flow to trace the real specialists and commander without touching the venue feed:

```bash
.venv/bin/crewai run
```

The default Flow reads the packaged outbreak fixture, calls real Parasail and You.com services,
returns the decision, and posts a neutral run-completion receipt to Discord. It does not submit a judgment.
Open the workspace tracing view in https://app.crewai.com/ after the run.
The existing `agentgeist` CLI remains available.

Traces contain incident inputs, source excerpts, task outputs, and timing.
Use only a workspace authorized to receive that data. Credentials remain environment variables.
The custom model wrapper excludes its underlying authenticated client and credit ledger from model serialization.

## Deploy the standard Flow

This project uses the standard `src/agentgeist/main.py` Flow entry point,
`kickoff` and `run_with_trigger` scripts, and `[tool.crewai] type = "flow"`.
The committed `uv.lock` supplies deployment dependencies.

```bash
uv sync --extra test
uv run crewai deploy validate
uv run crewai deploy create
```

Connect https://github.com/KishSiva/agentgeist through the CrewAI GitHub integration.
If CrewAI cannot access this private repository, authorize it in the intended GitHub account.
Use the existing plan. Do not purchase a plan or accept a paid upgrade as part of setup.

Configure deployment secrets directly in CrewAI:

| Name | Purpose |
|---|---|
| `PARASAIL_API_KEY` | Model calls and embeddings |
| `YOU_API_KEY` | Search and Contents |
| `SCENARIO_MCP` | Venue feed, only for live mode |
| `DISCORD_WEBHOOK_URL` | Discord credential for mandatory run-completion receipts |
| `DISCORD_TRANSPORT` | Set to `webhook` for direct posting |
| `CREWAI_TRACING_ENABLED` | Set to `true` for fixture runs; `false` for live runs |
| `OTEL_SDK_DISABLED` | Set to `false` |

Never put secret values in kickoff inputs, source code, trace labels, screenshots, or GitHub.
One credentials are optional when direct webhook transport is selected.

The default website run uses safe fixture mode. A trigger payload can select another fixture:

```json
{"mode":"fixture","scenario":"control-tick"}
```

## Live website runs

Live mode advances the team cursor even when `submit` is false.
First configure durable storage and a single execution worker for the team.
Set `AGENTGEIST_STATE_DIR` to that durable directory's absolute path and `AGENTGEIST_ENABLE_LIVE=true`.
Live mode rejects missing or relative state paths. The operator must verify storage durability.
Set `CREWAI_TRACING_ENABLED=false` for live runs. Live mode rejects active CrewAI trace sessions.
Venue MCP calls contain credentials, so only fixture runs support tracing.
Live failures report only the exception type, not credential-bearing error text.
Live execution suppresses provider console output because MCP events can contain credentials.
Keep the same state database between runs and redeployments.
A local file lock does not coordinate separate replicas with separate disks.

Then use an explicit trigger payload:

```json
{"mode":"live","team":"agentgeist","scenario":"outbreak-level-5","submit":true,"discord":true}
```

Each kickoff processes at most one new tick after the saved inbox. It does not install a polling daemon.
Do not enable live mode on ephemeral or multi-worker storage until shared state is configured.

Mandatory run summaries require `DISCORD_WEBHOOK_URL`, even when incident escalation uses One.
