from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ResetSessionRequest(_message.Message):
    __slots__ = ("sender", "recipient", "capability", "offering")
    SENDER_FIELD_NUMBER: _ClassVar[int]
    RECIPIENT_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    OFFERING_FIELD_NUMBER: _ClassVar[int]
    sender: bytes
    recipient: bytes
    capability: str
    offering: str
    def __init__(self, sender: _Optional[bytes] = ..., recipient: _Optional[bytes] = ..., capability: _Optional[str] = ..., offering: _Optional[str] = ...) -> None: ...

class ResetSessionResponse(_message.Message):
    __slots__ = ("reset", "old_work_id")
    RESET_FIELD_NUMBER: _ClassVar[int]
    OLD_WORK_ID_FIELD_NUMBER: _ClassVar[int]
    reset: bool
    old_work_id: str
    def __init__(self, reset: bool = ..., old_work_id: _Optional[str] = ...) -> None: ...
