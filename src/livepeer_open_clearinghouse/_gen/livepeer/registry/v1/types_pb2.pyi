from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ResolveMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RESOLVE_MODE_UNSPECIFIED: _ClassVar[ResolveMode]
    RESOLVE_MODE_WELL_KNOWN: _ClassVar[ResolveMode]
    RESOLVE_MODE_CSV: _ClassVar[ResolveMode]
    RESOLVE_MODE_LEGACY: _ClassVar[ResolveMode]

class SignatureStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SIGNATURE_STATUS_UNSPECIFIED: _ClassVar[SignatureStatus]
    SIGNATURE_STATUS_VERIFIED: _ClassVar[SignatureStatus]
    SIGNATURE_STATUS_UNSIGNED: _ClassVar[SignatureStatus]
    SIGNATURE_STATUS_LEGACY: _ClassVar[SignatureStatus]

class FreshnessStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FRESHNESS_STATUS_UNSPECIFIED: _ClassVar[FreshnessStatus]
    FRESHNESS_STATUS_FRESH: _ClassVar[FreshnessStatus]
    FRESHNESS_STATUS_STALE_RECOVERABLE: _ClassVar[FreshnessStatus]
    FRESHNESS_STATUS_STALE_FAILING: _ClassVar[FreshnessStatus]

class Source(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOURCE_UNSPECIFIED: _ClassVar[Source]
    SOURCE_MANIFEST: _ClassVar[Source]
    SOURCE_LEGACY: _ClassVar[Source]
    SOURCE_STATIC_OVERLAY: _ClassVar[Source]
    SOURCE_CSV_FALLBACK: _ClassVar[Source]
RESOLVE_MODE_UNSPECIFIED: ResolveMode
RESOLVE_MODE_WELL_KNOWN: ResolveMode
RESOLVE_MODE_CSV: ResolveMode
RESOLVE_MODE_LEGACY: ResolveMode
SIGNATURE_STATUS_UNSPECIFIED: SignatureStatus
SIGNATURE_STATUS_VERIFIED: SignatureStatus
SIGNATURE_STATUS_UNSIGNED: SignatureStatus
SIGNATURE_STATUS_LEGACY: SignatureStatus
FRESHNESS_STATUS_UNSPECIFIED: FreshnessStatus
FRESHNESS_STATUS_FRESH: FreshnessStatus
FRESHNESS_STATUS_STALE_RECOVERABLE: FreshnessStatus
FRESHNESS_STATUS_STALE_FAILING: FreshnessStatus
SOURCE_UNSPECIFIED: Source
SOURCE_MANIFEST: Source
SOURCE_LEGACY: Source
SOURCE_STATIC_OVERLAY: Source
SOURCE_CSV_FALLBACK: Source

class Capability(_message.Message):
    __slots__ = ("name", "work_unit", "offerings", "extra_json", "work_unit_estimator")
    NAME_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_FIELD_NUMBER: _ClassVar[int]
    OFFERINGS_FIELD_NUMBER: _ClassVar[int]
    EXTRA_JSON_FIELD_NUMBER: _ClassVar[int]
    WORK_UNIT_ESTIMATOR_FIELD_NUMBER: _ClassVar[int]
    name: str
    work_unit: str
    offerings: _containers.RepeatedCompositeFieldContainer[Offering]
    extra_json: bytes
    work_unit_estimator: Estimator
    def __init__(self, name: _Optional[str] = ..., work_unit: _Optional[str] = ..., offerings: _Optional[_Iterable[_Union[Offering, _Mapping]]] = ..., extra_json: _Optional[bytes] = ..., work_unit_estimator: _Optional[_Union[Estimator, _Mapping]] = ...) -> None: ...

class Estimator(_message.Message):
    __slots__ = ("id", "rounding", "exactness", "package", "fixtures")
    ID_FIELD_NUMBER: _ClassVar[int]
    ROUNDING_FIELD_NUMBER: _ClassVar[int]
    EXACTNESS_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_FIELD_NUMBER: _ClassVar[int]
    FIXTURES_FIELD_NUMBER: _ClassVar[int]
    id: str
    rounding: str
    exactness: str
    package: str
    fixtures: str
    def __init__(self, id: _Optional[str] = ..., rounding: _Optional[str] = ..., exactness: _Optional[str] = ..., package: _Optional[str] = ..., fixtures: _Optional[str] = ...) -> None: ...

class Offering(_message.Message):
    __slots__ = ("id", "price_per_work_unit_wei", "constraints_json")
    ID_FIELD_NUMBER: _ClassVar[int]
    PRICE_PER_WORK_UNIT_WEI_FIELD_NUMBER: _ClassVar[int]
    CONSTRAINTS_JSON_FIELD_NUMBER: _ClassVar[int]
    id: str
    price_per_work_unit_wei: str
    constraints_json: bytes
    def __init__(self, id: _Optional[str] = ..., price_per_work_unit_wei: _Optional[str] = ..., constraints_json: _Optional[bytes] = ...) -> None: ...

class Node(_message.Message):
    __slots__ = ("id", "url", "worker_eth_address", "extra_json", "capabilities", "source", "signature_status", "operator_address", "enabled", "tier_allowed", "weight")
    ID_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    WORKER_ETH_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    EXTRA_JSON_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_STATUS_FIELD_NUMBER: _ClassVar[int]
    OPERATOR_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    TIER_ALLOWED_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    id: str
    url: str
    worker_eth_address: str
    extra_json: bytes
    capabilities: _containers.RepeatedCompositeFieldContainer[Capability]
    source: Source
    signature_status: SignatureStatus
    operator_address: str
    enabled: bool
    tier_allowed: _containers.RepeatedScalarFieldContainer[str]
    weight: int
    def __init__(self, id: _Optional[str] = ..., url: _Optional[str] = ..., worker_eth_address: _Optional[str] = ..., extra_json: _Optional[bytes] = ..., capabilities: _Optional[_Iterable[_Union[Capability, _Mapping]]] = ..., source: _Optional[_Union[Source, str]] = ..., signature_status: _Optional[_Union[SignatureStatus, str]] = ..., operator_address: _Optional[str] = ..., enabled: bool = ..., tier_allowed: _Optional[_Iterable[str]] = ..., weight: _Optional[int] = ...) -> None: ...
