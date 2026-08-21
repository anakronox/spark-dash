"""Naming the shards of a model that spans nodes.

In tensor-parallel inference one node runs the API and the rest hold shards
with no endpoint of their own. The agent attributes its own processes by
counting CONFIGURED instances, so a worker node has none and leaves every
shard unattributed.

Observed on the live cluster 2026-08-21: `sparketa` reported
`VLLM::Worker_TP0` correctly named `deepseek-v4-flash-0731`, while `sparkjr`'s
identical 96.8 GiB `VLLM::Worker_TP1` carried `model=None` and read as
anonymous vLLM memory. Half of a 193.6 GiB model was invisible as that model.
"""

from __future__ import annotations

from datetime import UTC, datetime

from spark_dash_backend.poller import attribute_cluster_shards
from spark_dash_common.models import (
    EngineMetrics,
    NodeSnapshot,
    ProcessInfo,
    Runtimes,
)


def node(node_id, cluster, *, serves=None, procs=(), runtime="vllm"):
    return NodeSnapshot(
        node_id=node_id,
        ts=datetime.now(UTC),
        up=True,
        cluster=cluster,
        processes=[
            ProcessInfo(pid=p[0], name=p[1], gpu_mem_bytes=10, runtime=runtime, model=p[2])
            for p in procs
        ],
        runtimes=Runtimes(**{runtime: [
            EngineMetrics(model=m, server=f"{node_id}:8000") for m in (serves or [])
        ]}) if serves else Runtimes(),
    )


def test_a_worker_shard_is_named_from_the_head_node():
    head = node("sparketa", "danflashes", serves=["deepseek-v4-flash-0731"],
                procs=[(1, "VLLM::Worker_TP0", "deepseek-v4-flash-0731")])
    worker = node("sparkjr", "danflashes", procs=[(2, "VLLM::Worker_TP1", None)])
    attribute_cluster_shards([head, worker])

    assert worker.processes[0].model == "deepseek-v4-flash-0731"
    assert worker.processes[0].shard is True, "an inferred name must be marked as inferred"


def test_a_process_that_named_itself_is_not_overwritten():
    """The head node's own shard was attributed by the agent, from data the
    agent actually had. Nothing here should second-guess it."""
    head = node("sparketa", "danflashes", serves=["deepseek-v4-flash-0731"],
                procs=[(1, "VLLM::Worker_TP0", "deepseek-v4-flash-0731")])
    attribute_cluster_shards([head])
    assert head.processes[0].shard is False


def test_two_models_in_one_cluster_are_left_unattributed():
    """The same wall the agent hits with two local instances: there is no way
    to tell which shard belongs to which model. A 96.8 GiB block attributed to
    the WRONG model is worse than an unlabelled one, because the memory band
    would then confidently mis-state what is holding the pool."""
    a = node("n1", "c", serves=["model-a"], procs=[(1, "VLLM::Worker_TP0", "model-a")])
    b = node("n2", "c", serves=["model-b"], procs=[(2, "VLLM::Worker_TP0", "model-b")])
    worker = node("n3", "c", procs=[(3, "VLLM::Worker_TP1", None)])
    attribute_cluster_shards([a, b, worker])
    assert worker.processes[0].model is None


def test_nodes_outside_the_cluster_are_untouched():
    """Attribution pools WITHIN a cluster, never across -- the same boundary
    that stops free memory being summed across clusters."""
    head = node("sparketa", "danflashes", serves=["deepseek-v4-flash-0731"],
                procs=[(1, "VLLM::Worker_TP0", "deepseek-v4-flash-0731")])
    outsider = node("sparky", None, procs=[(2, "VLLM::EngineCore", None)])
    attribute_cluster_shards([head, outsider])
    assert outsider.processes[0].model is None


def test_an_unreachable_instance_does_not_name_shards():
    """A configured endpoint that did not answer carries its own address in
    `model` as a placeholder. Attributing shards to that string would spread a
    host:port through the memory band as though it were a model."""
    head = NodeSnapshot(
        node_id="sparketa", ts=datetime.now(UTC), up=True, cluster="danflashes",
        runtimes=Runtimes(vllm=[EngineMetrics(
            model="192.168.50.62:8000", server="192.168.50.62:8000", reachable=False)]),
    )
    worker = node("sparkjr", "danflashes", procs=[(2, "VLLM::Worker_TP1", None)])
    attribute_cluster_shards([head, worker])
    assert worker.processes[0].model is None


def test_runtimes_do_not_cross_contaminate():
    """A cluster serving vLLM says nothing about an unattributed SGLang
    process on one of its nodes."""
    head = node("n1", "c", serves=["dsv4"], procs=[(1, "VLLM::Worker_TP0", "dsv4")])
    worker = NodeSnapshot(
        node_id="n2", ts=datetime.now(UTC), up=True, cluster="c",
        processes=[ProcessInfo(pid=2, name="sglang", gpu_mem_bytes=10, runtime="sglang")],
    )
    attribute_cluster_shards([head, worker])
    assert worker.processes[0].model is None
