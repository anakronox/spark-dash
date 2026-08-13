# Per-node deployment (GX10)

Two containers: stock `node-exporter` plus our `spark-dash-agent`. Nothing is
installed on the base OS — see [../../docs/deployment.md](../../docs/deployment.md).

## First run

From a clone of this repo **on the GX10**:

```bash
# 1. Build the agent image. Build context is the repo root, not agent/ —
#    the image needs the local common/ package.
docker build -f agent/Dockerfile -t spark-dash-agent:latest .

# 2. Configure this node.
cd deploy/node
cp .env.example .env
$EDITOR .env          # set NODE_ID, INFERENCE_NETWORK, LLAMA_ROUTER_URL, VLLM_URLS

# 3. Bring it up.
docker compose up -d
```

## Verify

```bash
# Agent is alive and says whether any collector failed.
curl -s localhost:9500/health | jq

# The live-view payload the backend will poll.
curl -s localhost:9500/snapshot | jq

# What Prometheus will scrape.
curl -s localhost:9500/metrics | head -40
```

### What to actually check on first run

These are the GB10-specific things that can only be validated on real hardware:

1. **Unified memory was detected.** `/snapshot` should show
   `memory.unified: true`, and `memory.total_bytes` should be ~128GB (the whole
   shared pool). If `unified` is false, NVML reported a GPU memory total that
   doesn't match system RAM, and the detection heuristic needs revisiting.
2. **Memory numbers are believable.** Cross-check against
   `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` and `free -h`
   while a model is loaded. This is the number every standard GPU exporter gets
   wrong on GB10, so it's worth confirming by hand once.
3. **Process attribution works.** `/snapshot` `processes[]` should show real
   process names (`llama-server`, `python`), not `pid-1234`. Empty names mean
   the host PID namespace isn't visible — check `pid: "host"` took effect.
4. **The router collector didn't wake anything.** With models registered but
   idle/evicted, `/snapshot` should list them in `known_model_count` while
   `loaded_models` stays empty, and those models must **stay** evicted. If
   idle models start loading themselves after the agent starts, stop it and
   set `LLAMA_SCRAPE_LOADED_MODEL_METRICS=false` — that's the autoload bug
   (ggml-org/llama.cpp#23096) reaching further than expected.
5. **Clock state reads `PASS` under load, `IDLE` at rest.** A `THROTTLED`
   reading on a healthy node means the 1400MHz threshold needs calibrating for
   your hardware.

## Rolling out to nodes 2 and 3

Copy the repo, `cp .env.example .env`, change `NODE_ID`, `docker compose up -d`.
Nothing else differs.

## Notes

- **`pid: "host"`** is required for per-process GPU attribution: NVML returns
  host PIDs, which are meaningless inside a private PID namespace.
- **The agent runs as non-root.** If process names come back empty for
  containers running as other users, that's the tradeoff — set `user: "0:0"` on
  the service if full attribution matters more than least privilege here.
- **If your runtimes aren't on a shared Compose network**, delete the
  `inference` network from `docker-compose.yml` and point `LLAMA_ROUTER_URL` /
  `VLLM_URLS` at host ports instead.
