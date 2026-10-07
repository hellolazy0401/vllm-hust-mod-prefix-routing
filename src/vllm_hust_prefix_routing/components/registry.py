"""Separate activation units with a shared, versioned source contract."""
from dataclasses import dataclass

from ..runtime import enabled, runtime_status


@dataclass(frozen=True)
class PrefixRoutingComponent:
    component_id: str
    mechanisms: tuple[str, ...]
    source_commits: tuple[str, ...] = ("feeb41719d168be598f2287be5d9a1d615c6c27c",)

    def check_compatibility(self, host):
        from ..adapters.core.contract import check_host
        return check_host(host)

    def runtime_status(self):
        status = runtime_status()
        return {"component_id": self.component_id, "configured": enabled(self.component_id),
                "mechanisms": self.mechanisms, "counters": status["counters"]}


COMPONENTS = (
    PrefixRoutingComponent("routing", ("cache_index", "routing_policy", "production_keys", "proxy")),
    PrefixRoutingComponent("http_events", ("incremental_upload", "snapshot_reconciliation")),
    PrefixRoutingComponent("zmq_replay", ("epoch_sequence_replay", "snapshot_reconciliation")),
)
# Cache-key equivalence, rank isolation and authentication are mandatory invariants,
# not independent performance switches. Lifecycle is owned by the host bridge.

