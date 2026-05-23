"""gRPC client for service-registry-daemon over a Unix domain socket.

See `docs/references/service-registry-daemon.md` for the API surface
Livepeer Open Clearinghouse uses.

Phase 4 ships a MockRegistryClient; the real gRPC client is wired up in
Phase 6/7 alongside the docker compose stack.
"""

from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    CachingRegistryClient,
    CapabilityInfo,
    GrpcRegistryClient,
    MockRegistryClient,
    OfferingInfo,
    OrchestratorInfo,
    RegistryClient,
    SelectedRoute,
)

__all__ = [
    "CachingRegistryClient",
    "CapabilityInfo",
    "GrpcRegistryClient",
    "MockRegistryClient",
    "OfferingInfo",
    "OrchestratorInfo",
    "RegistryClient",
    "SelectedRoute",
]
