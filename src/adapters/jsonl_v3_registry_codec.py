"""Private v3 registry path-token wire grammar."""

from __future__ import annotations

import re
from dataclasses import fields, is_dataclass

from src.adapters.jsonl_v3_errors import V3PathError, V3ValidationError
from src.adapters.jsonl_v3_storage_paths import (
    V3DamagedOffsetPathToken,
    V3DamagedSegmentPathToken,
    V3OffsetPathToken,
    V3RegistryToken,
    V3SegmentPathToken,
    V3TerminalStagingToken,
    utc_filename_token,
)


def token_wire(token: object) -> str:
    if token is V3RegistryToken.WORK_REGISTRY:
        return token.value
    if isinstance(token, (V3SegmentPathToken, V3OffsetPathToken)):
        suffix = "offsets" if isinstance(token, V3OffsetPathToken) else "jsonl"
        return f"v3seg1:{token.blackout_id}:{token.logical_segment_id}:{utc_filename_token(token.started_utc)}:p{token.ordinal:06d}:{token.storage_id}:{suffix}"
    if isinstance(token, (V3DamagedSegmentPathToken, V3DamagedOffsetPathToken)):
        suffix = "offsets" if isinstance(token, V3DamagedOffsetPathToken) else "jsonl"
        return f"v3dam1:{token.blackout_id}:{token.logical_segment_id}:p{token.ordinal:06d}:{token.storage_id}:{token.file_sha256}:{suffix}"
    if isinstance(token, V3TerminalStagingToken):
        return f"tail-{token.blackout_id}.jsonl"
    raise V3ValidationError("unsupported registry token")


def token_parse(value: object, kind: str) -> object:
    if not isinstance(value, str):
        raise V3ValidationError("path token must be text")
    if kind in {"segment", "offset"}:
        suffix = "offsets" if kind == "offset" else "jsonl"
        match = re.fullmatch(
            rf"v3seg1:([0-9a-f]{{32}}):([0-9a-f]{{32}}):(\d{{8}}T\d{{12}}Z):p(\d{{6}}):([0-9a-f]{{32}}):{suffix}",
            value,
        )
        if match is None:
            raise V3PathError("active path token grammar mismatch")
        token_type = V3OffsetPathToken if kind == "offset" else V3SegmentPathToken
        return token_type(
            match.group(1),
            match.group(2),
            _utc_text(match.group(3)),
            int(match.group(4)),
            match.group(5),
        )
    if kind in {"damaged_segment", "damaged_offset"}:
        suffix = "offsets" if kind == "damaged_offset" else "jsonl"
        match = re.fullmatch(
            rf"v3dam1:([0-9a-f]{{32}}):([0-9a-f]{{32}}):p(\d{{6}}):([0-9a-f]{{32}}):([0-9a-f]{{64}}):{suffix}",
            value,
        )
        if match is None:
            raise V3PathError("damaged path token grammar mismatch")
        token_type = (
            V3DamagedOffsetPathToken if kind == "damaged_offset" else V3DamagedSegmentPathToken
        )
        return token_type(
            match.group(1), match.group(2), int(match.group(3)), match.group(4), match.group(5)
        )
    match = re.fullmatch(r"tail-([0-9a-f]{32})\.jsonl", value)
    if kind == "tail" and match is not None:
        return V3TerminalStagingToken(match.group(1))
    raise V3PathError("registry token grammar mismatch")


def _utc_text(value: str) -> str:
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}T{value[9:11]}:{value[11:13]}:{value[13:15]}.{value[15:21]}Z"


def state_wire(value: object) -> dict[str, object]:
    if not is_dataclass(value):
        raise V3ValidationError("registry state is not typed")
    return {field.name: wire_value(getattr(value, field.name)) for field in fields(value)}


def wire_value(value: object) -> object:
    if isinstance(
        value,
        (
            V3SegmentPathToken,
            V3OffsetPathToken,
            V3DamagedSegmentPathToken,
            V3DamagedOffsetPathToken,
            V3TerminalStagingToken,
            V3RegistryToken,
        ),
    ):
        return token_wire(value)
    if is_dataclass(value):
        return state_wire(value)
    if isinstance(value, tuple):
        return [wire_value(item) for item in value]
    return value
