"""gRPC client for service-registry-daemon over a Unix domain socket.

See `docs/references/service-registry-daemon.md` for the API surface
PymtHouse uses.

Phase 4 ships a MockRegistryClient; the real gRPC client is wired up in
Phase 6/7 alongside the docker compose stack.
"""

from pymthouse.providers.registry_daemon.client import (
    CapabilityInfo,
    GrpcRegistryClient,
    MockRegistryClient,
    OfferingInfo,
    OrchestratorInfo,
    RegistryClient,
    SelectedRoute,
)

__all__ = [
    "CapabilityInfo",
    "GrpcRegistryClient",
    "MockRegistryClient",
    "OfferingInfo",
    "OrchestratorInfo",
    "RegistryClient",
    "SelectedRoute",
]
