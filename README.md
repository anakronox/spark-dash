# spark-dash-homegrown

A scalable web dashboard for a home cluster of NVIDIA GB10-based inferencing
servers (ASUS GX10 / "DGX Spark" class hardware), showing GPU/system health and
live metrics for the LLM inferencing jobs (llama.cpp router + vLLM) running on
them.

Currently 1 node, growing to 3.

## Docs

- [Requirements](docs/requirements.md) — goals, current stack, functional/
  non-functional requirements.
- [Architecture](docs/architecture.md) — Prometheus for collection/storage,
  homegrown backend + frontend for the UI; component diagram; scaling approach.
- [Metrics catalog](docs/metrics.md) — exactly what's being collected, from
  vLLM, llama.cpp router mode, and GPU/system exporters, including GB10-specific
  caveats (unified memory, router autoload behavior).
- [Roadmap](docs/roadmap.md) — phased plan from single-node MVP to the full
  3-node cluster, plus open decisions.

Project tracking (issues/milestones) mirrors the roadmap on the
[Forgejo project](https://forgejo.indielab.tech/brian/spark-dash-homegrown).

