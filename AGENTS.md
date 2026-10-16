# Working on spark-dash: notes for agents

For an agent changing this repository (Claude Code, Hermes, Codex and the
like). An agent that only *queries* a running dashboard needs none of this:
connect to its MCP endpoint (README, step 8) and read the guides it serves,
`sparkdash://guide/investigating` first.

## Where things live

| Path | What it is |
|---|---|
| `agent/` | The per-node collector on each GB10: GPU, memory, processes, engines, temperatures, network. Exports Prometheus metrics and a live JSON snapshot on `:9500`. |
| `backend/` | FastAPI on the monitoring host: the REST API, the live WebSocket, the MCP endpoint (`agent_tools.py`, `agent_docs.py`), the Ask loop (`assistant.py`), the setup checklist (`setup.py`), notifications (`notifications.py`). Serves the built frontend. |
| `common/` | The snapshot models shared by agent and backend (`models.py`). |
| `frontend/` | Svelte 5 + Tailwind. `src/lib/` holds logic kept out of components so it can be run under node by the tests. |
| `central/` | The monitoring host's compose stack and config: Prometheus, Alertmanager, alert rules, the cluster file example. |
| `node/` | Each GB10's compose stack. |
| `docs/` | `roadmap.md` is the decision log; `deployment.md`, `app-design.md`, `architecture.md`, `metrics.md` the references. |
| `tests/` | Repo-wide guards (see below). `backend/tests/` is the backend's own suite. |

## Test and build

```bash
uv run pytest backend/tests tests      # everything; the JS logic runs under node via esbuild
cd frontend && npm run check           # svelte-check: types and a11y, must be 0 errors 0 warnings
cd frontend && npm run build           # what the image ships
uv run ruff check --ignore E501 <files you touched>
```

Images: `./scripts/build-images.sh` locally, `./scripts/publish-images.sh` to a
registry. The backend image builds the frontend itself.

## Guards that catch the usual mistakes

Run the suite before you call anything done; these fail loudly on purpose.

- **A named series** added to `HISTORY_QUERIES` (`backend/.../prometheus.py`)
  needs a chip, a network key or an entry in `QUERIES_WITHOUT_CHIPS`
  (`tests/test_history_metrics.py`), **and** a unit and meaning in
  `agent_tools.METRICS`, or agents are handed a name they cannot read.
- **A table column** needs a cell renderer and a sort value
  (`tests/test_table_columns.py`).
- **A scoped CSS class** must not share a name with a Tailwind utility
  (`tests/test_tailwind_shadowing.py`): `resize`, `ring`, `grow` and the like
  silently pick up Tailwind's rules.
- **Card sizing and paging** (`tests/test_card_resize.py`): Section measures
  cards with their fill lifted; read the comments in `Section.svelte` before
  touching `measure()` or `resizePlots()`.
- **Doc links** resolve (`tests/test_docs_links.py`).
- **Node colours** stay clear of the status colours (`scripts/palette_check.py`,
  `tests/test_palettes.py`): green, amber and red always mean a state.

## Rules the code depends on

- **GB10 has one memory pool.** NVML's memory figure is wrong on it; memory
  comes from the kernel. Every GPU workload competes for the same pool.
- **Temperatures: the hottest, never the mean, decides a state.** Averages
  exist as an overview only, and their colour still comes from the sensor
  closest to its limit.
- **Read-only by default.** The dashboard does not act on processes or models.
  Each exception (silences, maintenance windows, DGX OS updates, agent notes,
  the ntfy topic) is recorded in `docs/architecture.md`.
- **Secrets are files beside the cluster file, never in it**: `cluster.yml` is
  served to the browser. `assistant.key` and `mcp.key` are 0600; the ntfy
  topic is `secrets/ntfy-url`, shared with Alertmanager by group.
- **Agent tools pick series by name**, never raw PromQL, unless that switch is
  on; every result is capped and says when it was cut.
- **Relative mount paths in `central/compose.yaml` are deliberate**; a deploy
  tool's own copy may use absolute ones.

## Documenting a change

- **What and why** goes in `docs/roadmap.md`, under its lettered section,
  with what was measured. The roadmap keeps what was deliberately not built,
  too.
- **How to use it** goes in `README.md` (features, quickstart) and
  `docs/deployment.md` (the optional features in full).
- **An API route** goes in the table in `docs/app-design.md`.
- **Anything an agent querying the dashboard should know** goes in
  `backend/.../agent_docs.py` (served as MCP resources) or a tool's own
  description in `agent_tools.py`, since that is what a connected agent reads.
- Write plainly: the measured fact, without the paragraph defending it.
