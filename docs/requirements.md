# Requirements

## Goal

A web dashboard that exposes useful metrics about a small cluster of NVIDIA GB10-based
inferencing servers (ASUS GX10 / "DGX Spark" class hardware) and the LLM inferencing
jobs running on them.

## Current environment

- **Hardware today:** 1x ASUS GX10 (NVIDIA GB10 Grace Blackwell Superchip, 128GB unified
  LPDDR5x CPU/GPU memory, ARM64).
- **Hardware planned:** +2x additional GX10 units, forming a 3-node cluster.
- **Inference runtimes:**
  - Multiple `llama.cpp` (`llama-server`) containers, mostly run in **router mode**
    (one router process fronting several models, on-demand load/unload, LRU eviction).
  - Dockerized **vLLM** instances.
  - Both runtimes need to be supported by the dashboard, on every node.
- **Orchestration:** Docker Compose per host today. No cross-host orchestration
  (Swarm/Kubernetes) yet — see [roadmap](roadmap.md) for how this affects rollout.
- **GPU/system monitoring today:** `nvidia-smi` only, ad hoc. No persistent metrics
  collection in place yet.
- **Network/access:**
  - Primary usage is on the home/office LAN.
  - Also published externally via a Cloudflare Tunnel, fronted by Google OAuth.
  - Single primary user (Brian), but the Cloudflare/OAuth front door means it should
    not assume "localhost-only, no auth" — treat the public path as a real edge.
- **Source control / issue tracking:** self-hosted Forgejo at
  `forgejo.indielab.tech`, repo `brian/spark-dash-homegrown`. Issues, project boards,
  and wiki are all available and should be used to track this project, in addition to
  the markdown docs in this repo.

## Functional requirements

1. **Per-node system metrics**
   - GPU utilization, memory usage (see unified-memory caveat in
     [metrics.md](metrics.md)), temperature, power draw.
   - CPU, host memory, disk, network as secondary/system-health signals.
   - Node up/down (liveness) status — important once there are 3 nodes and a node
     drop needs to be obvious at a glance.
2. **Per-job / per-model inferencing metrics**
   - Which models are currently loaded/running on which node and which runtime
     (llama.cpp router vs vLLM).
   - Throughput: tokens/sec (prompt + generation), requests/sec.
   - Latency: time-to-first-token, per-request latency.
   - Queue depth / in-flight vs. queued requests.
   - KV-cache utilization.
   - For llama.cpp router mode specifically: which models are currently loaded vs.
     evicted, and recent load/swap events (router swaps are a real operational signal
     — a swap mid-conversation is a latency spike users will notice).
3. **Cluster-level view**
   - A single dashboard that aggregates across all nodes, not one dashboard per node.
   - Must degrade gracefully as nodes are added — adding node 2 and 3 should not
     require dashboard code changes, only config/inventory changes.
4. **History, not just live state**
   - At least short-to-medium retention (days-to-weeks) of time-series history for
     trend/regression spotting, not just an instantaneous snapshot.
5. **Access**
   - Usable unauthenticated on the LAN (or with minimal friction).
   - Safe to expose through the existing Cloudflare Tunnel + Google OAuth front door
     without the dashboard itself needing to reimplement auth (OAuth is handled at
     the tunnel edge) — but the dashboard should not assume it's unreachable from
     the public internet.

## Non-functional requirements

- **Scalable from 1 → 3 nodes without redesign.** This is the primary architectural
  driver — see [architecture.md](architecture.md).
- **Low operational overhead.** This is a homelab-scale project for one primary
  operator; avoid components that need significant ongoing babysitting.
- **Runs on ARM64.** GB10 is ARM-based (Cortex-X925/A725 + Blackwell GPU) — every
  component in the stack (exporters, backend, base images) must have ARM64 support,
  which rules out some x86-only tooling.
- **Reasonable resource footprint.** The monitoring stack is a guest on hardware
  whose primary job is inferencing; it shouldn't meaningfully compete with
  llama.cpp/vLLM for GPU/CPU/memory.

## Explicit non-goals (for now)

- Multi-tenant / multi-user access control beyond the OAuth front door.
- Managing or scheduling inferencing jobs (this is a monitoring dashboard, not an
  orchestrator) — though the design shouldn't preclude adding control actions later.
- Cross-cluster (i.e., beyond these 3 nodes) or cloud-hybrid monitoring.

## Open questions

Tracked in [roadmap.md](roadmap.md#open-decisions) so they don't get lost.
