# Architecture Notes

## Scope

This document theorizes about a single logical Codex agent with a local anchor and a burst-capable execution fabric. It is not a design for a general multi-agent swarm. The scheduler serves one primary identity and one operator-visible workspace.

## Principles

| Principle | Meaning |
| --- | --- |
| `Local anchor` | The agent's canonical state lives on local infrastructure first. |
| `Elastic workers` | Remote compute is rented briefly and discarded aggressively. |
| `Provider neutrality` | Scheduling logic should not assume one cloud provider is always best. |
| `Index, do not scrape` | Search systems consume extracted manifests instead of crawling raw storage repeatedly. |
| `Provable replay` | Every burst task should be reconstructable from logs, manifests, and checkpoints. |

## Control Plane

The control plane decides whether a task stays local or bursts. It should evaluate at least four vectors:

1. `Capability fit`: CPU, RAM, GPU, regional affinity, and tool availability.
2. `Cost fit`: expected spend ceiling for the task and current provider price.
3. `Latency fit`: queue time, data transfer time, and expected completion time.
4. `Risk fit`: provider reliability, credential exposure, and recovery cost.

Suggested control-plane artifacts:

- `task lease`: signed unit of work with time and budget limits.
- `execution receipt`: immutable outcome summary, checksums, timing, and worker identity.
- `artifact manifest`: normalized list of outputs written to NFS and object storage.
- `index event`: metadata payload emitted for downstream query systems.

## Execution Plane

Each burst worker should be thin:

- fetch the lease;
- mount or sync the minimum workspace slice;
- execute the bounded task;
- write results back to NFS;
- emit receipts and manifests;
- terminate.

That model avoids long-lived pets. It also makes provider substitution easier because the runner contract is mostly about inputs, outputs, and receipts.

## Data Plane

The NFS share remains the canonical workspace because it already stores repos, artifacts, logs, and checkpoints in one operator-visible place. The main weakness of NFS is not authority; it is query ergonomics at scale.

The proposed answer is a derived index plane:

1. Extract manifests, content hashes, path metadata, task lineage, and optional semantic embeddings from the NFS tree.
2. Publish those derived records to S3 as append-friendly objects or compacted parquet tables.
3. Surface those records through BigQuery or BigLake-style external tables for large joins, analytics, and audit queries.

That gives two useful modes:

- `hot path`: the agent reads and writes the local workspace directly.
- `cold path`: operators and analytics jobs query the derived index without hammering NFS.

## Why S3 Plus BigQuery

The combination is attractive when object storage is the interchange layer and BigQuery is the analysis layer:

- S3 is cheap and widely interoperable for manifests, chunks, snapshots, and parquet outputs.
- BigQuery is strong at structured search over large metadata sets, especially when operators want SQL, audit views, and cost reporting.
- The index becomes portable because object storage is not tied to the compute provider that ran the burst task.

The constraint is that this is an indexing architecture, not a transactional storage architecture. If near-real-time consistency is required, the control plane still has to consult the local workspace or a more authoritative metadata service.

## Scheduling Ideas

### Simple heuristic phase

Start with weighted scoring:

`score = capability + speed - cost - transfer_penalty - risk_penalty`

This is enough for early experiments and makes outcomes easy to explain.

### Market phase

Later, the scheduler could treat providers as a dynamic market:

- publish a workload profile;
- estimate transfer cost from the local data anchor;
- rank providers by expected finish time per dollar;
- reserve short leases with a hard budget cap;
- retry on the next-best provider when the lease fails.

### Overflow phase

For very large jobs, burst workers could stage outputs in object storage first, then hydrate selected results back onto NFS. That reduces NFS write amplification while preserving the local workspace as the curated working set.

## Failure Modes

| Failure | Likely effect | Mitigation |
| --- | --- | --- |
| Provider outage | Lease cannot start or finish. | Multi-provider fallback and short leases. |
| Data transfer bottleneck | Burst speedup disappears. | Ship minimal workspace slices and cache dependencies. |
| Index lag | BigQuery answers are stale. | Mark freshness explicitly and separate hot vs cold queries. |
| NFS contention | Local authority becomes a bottleneck. | Batch writes, compact manifests, and tier artifact placement. |
| Provenance gaps | Offloaded work becomes unauditable. | Require receipts, checksums, and manifest publication. |

## Experimental Path

1. Build a manifest extractor for one repo tree on the local NFS share.
2. Emit parquet or JSONL manifests into S3-compatible object storage.
3. Expose those manifests to a SQL query layer for path, artifact, and lineage search.
4. Add one burst worker backend and compare local vs remote completion time.
5. Introduce a second provider only after receipts and replayability are trustworthy.

## Summary

The viable version of this idea is not "one super-agent everywhere at once." It is "one anchored agent that rents specialized compute temporarily, keeps its truth local, and maintains a derived cloud index that is optimized for search, lineage, and cost analysis."
