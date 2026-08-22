import datetime

from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from livepeer.registry.v1 import types_pb2 as _types_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

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
    __slots__ = ("eth_address", "resolved_uri", "mode", "nodes", "freshness_status", "cached_at", "fetched_at", "schema_version")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_URI_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    FRESHNESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    CACHED_AT_FIELD_NUMBER: _ClassVar[int]
    FETCHED_AT_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    resolved_uri: str
    mode: _types_pb2.ResolveMode
    nodes: _containers.RepeatedCompositeFieldContainer[_types_pb2.Node]
    freshness_status: _types_pb2.FreshnessStatus
    cached_at: _timestamp_pb2.Timestamp
    fetched_at: _timestamp_pb2.Timestamp
    schema_version: str
    def __init__(self, eth_address: _Optional[str] = ..., resolved_uri: _Optional[str] = ..., mode: _Optional[_Union[_types_pb2.ResolveMode, str]] = ..., nodes: _Optional[_Iterable[_Union[_types_pb2.Node, _Mapping]]] = ..., freshness_status: _Optional[_Union[_types_pb2.FreshnessStatus, str]] = ..., cached_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., fetched_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., schema_version: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("worker_url", "eth_address", "capability", "offering", "price_per_work_unit_wei", "work_unit", "extra_json", "constraints_json", "quote_id", "quote_version", "constraint_fingerprint", "route_fingerprint", "units_per_price", "protocol", "settlement_keys", "work_unit_estimator")
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
    def __init__(self, worker_url: _Optional[str] = ..., eth_address: _Optional[str] = ..., capability: _Optional[str] = ..., offering: _Optional[str] = ..., price_per_work_unit_wei: _Optional[str] = ..., work_unit: _Optional[str] = ..., extra_json: _Optional[bytes] = ..., constraints_json: _Optional[bytes] = ..., quote_id: _Optional[str] = ..., quote_version: _Optional[int] = ..., constraint_fingerprint: _Optional[bytes] = ..., route_fingerprint: _Optional[bytes] = ..., units_per_price: _Optional[int] = ..., protocol: _Optional[str] = ..., settlement_keys: _Optional[_Iterable[_Union[SettlementKey, _Mapping]]] = ..., work_unit_estimator: _Optional[_Union[_types_pb2.Estimator, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("eth_address", "mode", "freshness_status", "cached_at")
    ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    FRESHNESS_STATUS_FIELD_NUMBER: _ClassVar[int]
    CACHED_AT_FIELD_NUMBER: _ClassVar[int]
    eth_address: str
    mode: _types_pb2.ResolveMode
    freshness_status: _types_pb2.FreshnessStatus
    cached_at: _timestamp_pb2.Timestamp
    def __init__(self, eth_address: _Optional[str] = ..., mode: _Optional[_Union[_types_pb2.ResolveMode, str]] = ..., freshness_status: _Optional[_Union[_types_pb2.FreshnessStatus, str]] = ..., cached_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

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
