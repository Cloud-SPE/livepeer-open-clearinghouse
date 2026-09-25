import datetime

from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from livepeer.registry.v1 import types_pb2 as _types_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ManifestCompatibility(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MANIFEST_COMPATIBILITY_UNSPECIFIED: _ClassVar[ManifestCompatibility]
    MANIFEST_COMPATIBILITY_UNKNOWN: _ClassVar[ManifestCompatibility]
    MANIFEST_COMPATIBILITY_VERIFIED_COMPATIBLE: _ClassVar[ManifestCompatibility]
    MANIFEST_COMPATIBILITY_CONFIRMED_INCOMPATIBLE: _ClassVar[ManifestCompatibility]

class ManifestAvailability(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MANIFEST_AVAILABILITY_UNSPECIFIED: _ClassVar[ManifestAvailability]
    MANIFEST_AVAILABILITY_UNKNOWN: _ClassVar[ManifestAvailability]
    MANIFEST_AVAILABILITY_AVAILABLE: _ClassVar[ManifestAvailability]
    MANIFEST_AVAILABILITY_UNAVAILABLE: _ClassVar[ManifestAvailability]

class DiscoveryFailureReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DISCOVERY_FAILURE_REASON_UNSPECIFIED: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_NONE: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_TRANSPORT: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_HTTP: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_MANIFEST_MISSING: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_MANIFEST_UNSUPPORTED: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_MANIFEST_INVALID: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_SIGNATURE_INVALID: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_MANIFEST_EXPIRED: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_PUBLICATION_REPLAY: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_CHAIN_UNAVAILABLE: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_SOURCE_MISSING: _ClassVar[DiscoveryFailureReason]
    DISCOVERY_FAILURE_REASON_INTERNAL: _ClassVar[DiscoveryFailureReason]

class CatalogCompleteness(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CATALOG_COMPLETENESS_UNSPECIFIED: _ClassVar[CatalogCompleteness]
    CATALOG_COMPLETENESS_UNINITIALIZED: _ClassVar[CatalogCompleteness]
    CATALOG_COMPLETENESS_PARTIAL: _ClassVar[CatalogCompleteness]
    CATALOG_COMPLETENESS_COMPLETE: _ClassVar[CatalogCompleteness]
MANIFEST_COMPATIBILITY_UNSPECIFIED: ManifestCompatibility
MANIFEST_COMPATIBILITY_UNKNOWN: ManifestCompatibility
MANIFEST_COMPATIBILITY_VERIFIED_COMPATIBLE: ManifestCompatibility
MANIFEST_COMPATIBILITY_CONFIRMED_INCOMPATIBLE: ManifestCompatibility
MANIFEST_AVAILABILITY_UNSPECIFIED: ManifestAvailability
MANIFEST_AVAILABILITY_UNKNOWN: ManifestAvailability
MANIFEST_AVAILABILITY_AVAILABLE: ManifestAvailability
MANIFEST_AVAILABILITY_UNAVAILABLE: ManifestAvailability
DISCOVERY_FAILURE_REASON_UNSPECIFIED: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_NONE: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_TRANSPORT: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_HTTP: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_MANIFEST_MISSING: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_MANIFEST_UNSUPPORTED: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_MANIFEST_INVALID: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_SIGNATURE_INVALID: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_MANIFEST_EXPIRED: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_PUBLICATION_REPLAY: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_CHAIN_UNAVAILABLE: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_SOURCE_MISSING: DiscoveryFailureReason
DISCOVERY_FAILURE_REASON_INTERNAL: DiscoveryFailureReason
CATALOG_COMPLETENESS_UNSPECIFIED: CatalogCompleteness
CATALOG_COMPLETENESS_UNINITIALIZED: CatalogCompleteness
CATALOG_COMPLETENESS_PARTIAL: CatalogCompleteness
CATALOG_COMPLETENESS_COMPLETE: CatalogCompleteness

class ResolveByAddressRequest(_message.Message):
    __slots__ = ("eth_address", "allow_legacy_fallback", "allow_unsigned", "force_refresh")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_LEGACY_FALLBACK_FIELD_NUMBER: _ClassVar[int]
    ALLOW_UNSIGNED_FIELD_NUMBER: _ClassVar[int]
    FORCE_REFRESH_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    allow_legacy_fallback: bool
    allow_unsigned: bool
    force_refresh: bool
    def __init__(self, eth_address: _Optional[str] = ..., allow_legacy_fallback: bool = ..., allow_unsigned: bool = ..., force_refresh: bool = ...) -> None: ...

class ResolveResult(_message.Message):
    __slots__ = ("eth_address", "resolved_uri", "mode", "nodes", "freshness_status", "cached_at", "fetched_at", "schema_version", "discovery_status")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_URI_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    FRESHNESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    CACHED_AT_FIELD_NUMBER: _ClassVar[int]
    FETCHED_AT_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_STATUS_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    resolved_uri: str
    mode: _types_pb2.ResolveMode
    nodes: _containers.RepeatedCompositeFieldContainer[_types_pb2.Node]
    freshness_status: _types_pb2.FreshnessStatus
    cached_at: _timestamp_pb2.Timestamp
    fetched_at: _timestamp_pb2.Timestamp
    schema_version: str
    discovery_status: DiscoveryStatus
    def __init__(self, eth_address: _Optional[str] = ..., resolved_uri: _Optional[str] = ..., mode: _Optional[_Union[_types_pb2.ResolveMode, str]] = ..., nodes: _Optional[_Iterable[_Union[_types_pb2.Node, _Mapping]]] = ..., freshness_status: _Optional[_Union[_types_pb2.FreshnessStatus, str]] = ..., cached_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., fetched_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., schema_version: _Optional[str] = ..., discovery_status: _Optional[_Union[DiscoveryStatus, _Mapping]] = ...) -> None: ...

class SelectRequest(_message.Message):
    __slots__ = ("capability", "offering", "tier", "min_weight")
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    TIER_FIELD_NUMBER: _ClassVar[int]
    MIN_WEIGHT_FIELD_NUMBER: _ClassVar[int]
    capability: str
    offering: str
    tier: str
    min_weight: int
    def __init__(self, capability: _Optional[str] = ..., offering: _Optional[str] = ..., tier: _Optional[str] = ..., min_weight: _Optional[int] = ...) -> None: ...

class SelectResult(_message.Message):
    __slots__ = ("route",)
    ROUTE_FIELD_NUMBER: _ClassVar[int]
    route: SelectedRoute
    def __init__(self, route: _Optional[_Union[SelectedRoute, _Mapping]] = ...) -> None: ...

class SelectManyResult(_message.Message):
    __slots__ = ("routes",)
    ROUTES_FIELD_NUMBER: _ClassVar[int]
    routes: _containers.RepeatedCompositeFieldContainer[SelectedRoute]
    def __init__(self, routes: _Optional[_Iterable[_Union[SelectedRoute, _Mapping]]] = ...) -> None: ...

class SelectedRoute(_message.Message):
    __slots__ = ("worker_url", "eth_address", "capability", "offering", "price_per_work_unit_wei", "work_unit", "extra_json", "constraints_json", "quote_id", "quote_version", "constraint_fingerprint", "route_fingerprint", "units_per_price", "protocol", "settlement_keys", "work_unit_estimator", "settlement_domain_id")
    WORKER_URL_FIELD_NUMBER: _ClassVar[int]
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    PRICE_PER_WORK_UNIT_WEI_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_FIELD_NUMBER: _ClassVar[int]
    EXTRA_JSON_FIELD_NUMBER: _ClassVar[int]
    CONSTRAINTS_JSON_FIELD_NUMBER: _ClassVar[int]
    QUOTE_ID_FIELD_NUMBER: _ClassVar[int]
    QUOTE_VERSION_FIELD_NUMBER: _ClassVar[int]
    CONSTRAINT_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    ROUTE_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    UNITS_PER_PRICE_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    SETTLEMENT_KEYS_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_ESTIMATOR_FIELD_NUMBER: _ClassVar[int]
    SETTLEMENT_DOMAIN_ID_FIELD_NUMBER: _ClassVar[int]
    worker_url: str
    eth_address: str
    capability: str
    offering: str
    price_per_work_unit_wei: str
    work_unit: str
    extra_json: bytes
    constraints_json: bytes
    quote_id: str
    quote_version: int
    constraint_fingerprint: bytes
    route_fingerprint: bytes
    units_per_price: int
    protocol: str
    settlement_keys: _containers.RepeatedCompositeFieldContainer[SettlementKey]
    work_unit_estimator: _types_pb2.Estimator
    settlement_domain_id: str
    def __init__(self, worker_url: _Optional[str] = ..., eth_address: _Optional[str] = ..., capability: _Optional[str] = ..., offering: _Optional[str] = ..., price_per_work_unit_wei: _Optional[str] = ..., work_unit: _Optional[str] = ..., extra_json: _Optional[bytes] = ..., constraints_json: _Optional[bytes] = ..., quote_id: _Optional[str] = ..., quote_version: _Optional[int] = ..., constraint_fingerprint: _Optional[bytes] = ..., route_fingerprint: _Optional[bytes] = ..., units_per_price: _Optional[int] = ..., protocol: _Optional[str] = ..., settlement_keys: _Optional[_Iterable[_Union[SettlementKey, _Mapping]]] = ..., work_unit_estimator: _Optional[_Union[_types_pb2.Estimator, _Mapping]] = ..., settlement_domain_id: _Optional[str] = ...) -> None: ...

class SettlementKey(_message.Message):
    __slots__ = ("public_key", "not_before", "expires_at", "introduced_in_publication_seq")
    PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    NOT_BEFORE_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    INTRODUCED_IN_PUBLICATION_SEQ_FIELD_NUMBER: _ClassVar[int]
    public_key: str
    not_before: str
    expires_at: str
    introduced_in_publication_seq: int
    def __init__(self, public_key: _Optional[str] = ..., not_before: _Optional[str] = ..., expires_at: _Optional[str] = ..., introduced_in_publication_seq: _Optional[int] = ...) -> None: ...

class ListKnownRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListKnownResult(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[KnownEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[KnownEntry, _Mapping]]] = ...) -> None: ...

class KnownEntry(_message.Message):
    __slots__ = ("eth_address", "mode", "freshness_status", "cached_at", "discovery_status")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    FRESHNESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    CACHED_AT_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_STATUS_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    mode: _types_pb2.ResolveMode
    freshness_status: _types_pb2.FreshnessStatus
    cached_at: _timestamp_pb2.Timestamp
    discovery_status: DiscoveryStatus
    def __init__(self, eth_address: _Optional[str] = ..., mode: _Optional[_Union[_types_pb2.ResolveMode, str]] = ..., freshness_status: _Optional[_Union[_types_pb2.FreshnessStatus, str]] = ..., cached_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., discovery_status: _Optional[_Union[DiscoveryStatus, _Mapping]] = ...) -> None: ...

class DiscoveryStatus(_message.Message):
    __slots__ = ("source_uri", "compatibility", "availability", "failure_reason", "consecutive_failures", "last_verified_at", "next_retry_at", "last_attempt_at", "compatibility_checked_at", "compatibility_valid_until", "source_checked_at", "retry_class", "policy_step")
    SOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    COMPATIBILITY_FIELD_NUMBER: _ClassVar[int]
    AVAILABILITY_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: _ClassVar[int]
    CONSECUTIVE_FAILURES_FIELD_NUMBER: _ClassVar[int]
    LAST_VERIFIED_AT_FIELD_NUMBER: _ClassVar[int]
    NEXT_RETRY_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_ATTEMPT_AT_FIELD_NUMBER: _ClassVar[int]
    COMPATIBILITY_CHECKED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPATIBILITY_VALID_UNTIL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_CHECKED_AT_FIELD_NUMBER: _ClassVar[int]
    RETRY_CLASS_FIELD_NUMBER: _ClassVar[int]
    POLICY_STEP_FIELD_NUMBER: _ClassVar[int]
    source_uri: str
    compatibility: ManifestCompatibility
    availability: ManifestAvailability
    failure_reason: DiscoveryFailureReason
    consecutive_failures: int
    last_verified_at: _timestamp_pb2.Timestamp
    next_retry_at: _timestamp_pb2.Timestamp
    last_attempt_at: _timestamp_pb2.Timestamp
    compatibility_checked_at: _timestamp_pb2.Timestamp
    compatibility_valid_until: _timestamp_pb2.Timestamp
    source_checked_at: _timestamp_pb2.Timestamp
    retry_class: str
    policy_step: int
    def __init__(self, source_uri: _Optional[str] = ..., compatibility: _Optional[_Union[ManifestCompatibility, str]] = ..., availability: _Optional[_Union[ManifestAvailability, str]] = ..., failure_reason: _Optional[_Union[DiscoveryFailureReason, str]] = ..., consecutive_failures: _Optional[int] = ..., last_verified_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., next_retry_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., last_attempt_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., compatibility_checked_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., compatibility_valid_until: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., source_checked_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., retry_class: _Optional[str] = ..., policy_step: _Optional[int] = ...) -> None: ...

class RegistryResolutionDetail(_message.Message):
    __slots__ = ("eth_address", "discovery_status", "evaluated_at")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_STATUS_FIELD_NUMBER: _ClassVar[int]
    EVALUATED_AT_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    discovery_status: DiscoveryStatus
    evaluated_at: _timestamp_pb2.Timestamp
    def __init__(self, eth_address: _Optional[str] = ..., discovery_status: _Optional[_Union[DiscoveryStatus, _Mapping]] = ..., evaluated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ListOfferingsRequest(_message.Message):
    __slots__ = ("capability", "offering", "tier", "min_weight", "include_expired")
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    TIER_FIELD_NUMBER: _ClassVar[int]
    MIN_WEIGHT_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_EXPIRED_FIELD_NUMBER: _ClassVar[int]
    capability: str
    offering: str
    tier: str
    min_weight: int
    include_expired: bool
    def __init__(self, capability: _Optional[str] = ..., offering: _Optional[str] = ..., tier: _Optional[str] = ..., min_weight: _Optional[int] = ..., include_expired: bool = ...) -> None: ...

class CatalogOffering(_message.Message):
    __slots__ = ("offering", "worker_id", "selectable", "exclusion_reason", "verified_at", "valid_until", "expired", "health_valid_until")
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    SELECTABLE_FIELD_NUMBER: _ClassVar[int]
    EXCLUSION_REASON_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_AT_FIELD_NUMBER: _ClassVar[int]
    VALID_UNTIL_FIELD_NUMBER: _ClassVar[int]
    EXPIRED_FIELD_NUMBER: _ClassVar[int]
    HEALTH_VALID_UNTIL_FIELD_NUMBER: _ClassVar[int]
    offering: SelectedRoute
    worker_id: str
    selectable: bool
    exclusion_reason: str
    verified_at: _timestamp_pb2.Timestamp
    valid_until: _timestamp_pb2.Timestamp
    expired: bool
    health_valid_until: _timestamp_pb2.Timestamp
    def __init__(self, offering: _Optional[_Union[SelectedRoute, _Mapping]] = ..., worker_id: _Optional[str] = ..., selectable: bool = ..., exclusion_reason: _Optional[str] = ..., verified_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., valid_until: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., expired: bool = ..., health_valid_until: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CatalogCoverage(_message.Message):
    __slots__ = ("known_addresses", "verified_compatible_addresses", "confirmed_incompatible_addresses", "unknown_addresses", "expired_addresses", "deferred_addresses", "unavailable_addresses")
    KNOWN_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_COMPATIBLE_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    CONFIRMED_INCOMPATIBLE_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    EXPIRED_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    DEFERRED_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    known_addresses: int
    verified_compatible_addresses: int
    confirmed_incompatible_addresses: int
    unknown_addresses: int
    expired_addresses: int
    deferred_addresses: int
    unavailable_addresses: int
    def __init__(self, known_addresses: _Optional[int] = ..., verified_compatible_addresses: _Optional[int] = ..., confirmed_incompatible_addresses: _Optional[int] = ..., unknown_addresses: _Optional[int] = ..., expired_addresses: _Optional[int] = ..., deferred_addresses: _Optional[int] = ..., unavailable_addresses: _Optional[int] = ...) -> None: ...

class ListOfferingsResult(_message.Message):
    __slots__ = ("entries", "completeness", "coverage", "snapshot_at", "evaluated_at", "discovery_scope", "discovery_scope_authoritative", "discovery_observed_at", "discovery_valid_until", "coverage_valid_until")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    COMPLETENESS_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_AT_FIELD_NUMBER: _ClassVar[int]
    EVALUATED_AT_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_SCOPE_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_SCOPE_AUTHORITATIVE_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    DISCOVERY_VALID_UNTIL_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_VALID_UNTIL_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[CatalogOffering]
    completeness: CatalogCompleteness
    coverage: CatalogCoverage
    snapshot_at: _timestamp_pb2.Timestamp
    evaluated_at: _timestamp_pb2.Timestamp
    discovery_scope: str
    discovery_scope_authoritative: bool
    discovery_observed_at: _timestamp_pb2.Timestamp
    discovery_valid_until: _timestamp_pb2.Timestamp
    coverage_valid_until: _timestamp_pb2.Timestamp
    def __init__(self, entries: _Optional[_Iterable[_Union[CatalogOffering, _Mapping]]] = ..., completeness: _Optional[_Union[CatalogCompleteness, str]] = ..., coverage: _Optional[_Union[CatalogCoverage, _Mapping]] = ..., snapshot_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., evaluated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., discovery_scope: _Optional[str] = ..., discovery_scope_authoritative: bool = ..., discovery_observed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., discovery_valid_until: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., coverage_valid_until: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class RefreshRequest(_message.Message):
    __slots__ = ("eth_address", "force")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    FORCE_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    force: bool
    def __init__(self, eth_address: _Optional[str] = ..., force: bool = ...) -> None: ...

class GetAuditLogRequest(_message.Message):
    __slots__ = ("eth_address", "since", "limit")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    SINCE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    since: _timestamp_pb2.Timestamp
    limit: int
    def __init__(self, eth_address: _Optional[str] = ..., since: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., limit: _Optional[int] = ...) -> None: ...

class AuditLogResult(_message.Message):
    __slots__ = ("events",)
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[AuditEvent]
    def __init__(self, events: _Optional[_Iterable[_Union[AuditEvent, _Mapping]]] = ...) -> None: ...

class AuditEvent(_message.Message):
    __slots__ = ("at", "eth_address", "kind", "mode", "detail")
    AT_FIELD_NUMBER: _ClassVar[int]
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    at: _timestamp_pb2.Timestamp
    eth_address: str
    kind: str
    mode: _types_pb2.ResolveMode
    detail: str
    def __init__(self, at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., eth_address: _Optional[str] = ..., kind: _Optional[str] = ..., mode: _Optional[_Union[_types_pb2.ResolveMode, str]] = ..., detail: _Optional[str] = ...) -> None: ...

class HealthResult(_message.Message):
    __slots__ = ("mode", "chain_ok", "manifest_fetcher_ok", "cache_size", "last_chain_success")
    MODE_FIELD_NUMBER: _ClassVar[int]
    CHAIN_OK_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FETCHER_OK_FIELD_NUMBER: _ClassVar[int]
    CACHE_SIZE_FIELD_NUMBER: _ClassVar[int]
    LAST_CHAIN_SUCCESS_FIELD_NUMBER: _ClassVar[int]
    mode: str
    chain_ok: bool
    manifest_fetcher_ok: bool
    cache_size: int
    last_chain_success: _timestamp_pb2.Timestamp
    def __init__(self, mode: _Optional[str] = ..., chain_ok: bool = ..., manifest_fetcher_ok: bool = ..., cache_size: _Optional[int] = ..., last_chain_success: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
