# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Context Pack wire protocol for incremental context transport.

A Context Pack represents one complete prepared context as ordered chunks with a digest and
byte count. Follow-up turns reuse a receiver-held base pack: a delta names the shared chunk
prefix and carries only the appended target chunks, so a receiver can rebuild the exact
target from serialized bytes alone.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_PACK_KEYS = {"schema", "items", "digest", "byte_count"}
_DELTA_KEYS = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}


class ContextPackError(Exception):
    """Raised when a pack, delta, or chunking payload violates the wire protocol."""


@dataclass(frozen=True)
class ContextPack:
    """One complete context as ordered chunks with a verifiable digest."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        """Build one pack from an ordered bytes tuple."""

        chunks = tuple(items)
        digest = _pack_digest(chunks)
        return cls(items=chunks, digest=digest, byte_count=len(_pack_to_bytes(chunks, digest)))

    @classmethod
    def from_content(cls, content: str, /, *, chunk_bytes: int = 512) -> ContextPack:
        """Chunk ``content`` deterministically, keeping every chunk valid UTF-8."""

        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        chunks: list[bytes] = []
        current: list[str] = []
        current_bytes = 0
        for character in content:
            width = len(character.encode("utf-8"))
            if width > chunk_bytes:
                raise ContextPackError("chunk_bytes cannot hold a single UTF-8 code point")
            if current_bytes + width > chunk_bytes:
                chunks.append("".join(current).encode("utf-8"))
                current = [character]
                current_bytes = width
            else:
                current.append(character)
                current_bytes += width
        if current:
            chunks.append("".join(current).encode("utf-8"))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        """Serialize this pack into its canonical wire payload."""

        return _pack_to_bytes(self.items, self.digest)

    @classmethod
    def from_bytes(cls, payload: bytes, /) -> ContextPack:
        """Decode and fully validate one serialized pack."""

        body = _decode_json(payload, _PACK_KEYS, _PACK_SCHEMA)
        items = _decode_items(body["items"])
        digest = body["digest"]
        if not isinstance(digest, str) or digest != _pack_digest(items):
            raise ContextPackError("context pack digest mismatch")
        byte_count = body["byte_count"]
        if type(byte_count) is not int:
            raise ContextPackError("context pack byte_count must be an integer")
        canonical = _pack_to_bytes(items, digest)
        if byte_count != len(canonical):
            raise ContextPackError("context pack byte_count mismatch")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True)
class ContextPackDelta:
    """One incremental update: reuse a base prefix, then append target-only chunks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Select the longest valid common chunk prefix and carry the target suffix."""

        prefix = 0
        for base_item, target_item in zip(base.items, target.items):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        """Exact wire size of this delta, including every protocol field."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize this delta into its canonical wire payload."""

        return json.dumps(
            {
                "schema": _DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": [_b64_encode(item) for item in self.appended],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes, /) -> ContextPackDelta:
        """Decode and validate one serialized delta; prefix bounds stay receiver-side."""

        body = _decode_json(payload, _DELTA_KEYS, _DELTA_SCHEMA)
        base_digest = _validated_digest(body["base_digest"], "base")
        target_digest = _validated_digest(body["target_digest"], "target")
        prefix = body["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("delta prefix_items must be a non-negative integer")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix,
            appended=_decode_items(body["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Validate a delta against its base and rebuild the exact target pack."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("apply_context_pack_delta requires a pack and a delta")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the provided base pack")
    if type(delta.prefix_items) is not int or not 0 <= delta.prefix_items <= len(base.items):
        raise ContextPackError("delta prefix_items is out of bounds for the base pack")
    items = (*base.items[: delta.prefix_items], *delta.appended)
    if _pack_digest(items) != delta.target_digest:
        raise ContextPackError("delta does not rebuild the declared target pack")
    return ContextPack.build(items)


def _pack_to_bytes(items: tuple[bytes, ...], digest: str) -> bytes:
    encoded = [_b64_encode(item) for item in items]
    byte_count = 0
    while True:
        payload = json.dumps(
            {"schema": _PACK_SCHEMA, "items": encoded, "digest": digest, "byte_count": byte_count},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) == byte_count:
            return payload
        byte_count = len(payload)


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = json.dumps(
        {"schema": _PACK_SCHEMA, "items": [_b64_encode(item) for item in items]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(unsigned).hexdigest()


def _decode_json(payload: bytes, keys: set[str], schema: str) -> dict[str, object]:
    try:
        body = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as error:
        raise ContextPackError("payload is not valid JSON") from error
    if not isinstance(body, dict) or set(body) != keys:
        raise ContextPackError(f"payload must contain exactly {sorted(keys)}")
    if body["schema"] != schema:
        raise ContextPackError(f"payload schema must be {schema!r}")
    return body


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("wire items must be a list of base64 strings")
    chunks: list[bytes] = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError("wire items must be base64 strings")
        try:
            chunks.append(base64.b64decode(item.encode("ascii"), validate=True))
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise ContextPackError("wire items contain invalid base64") from error
    return tuple(chunks)


def _b64_encode(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _validated_digest(value: object, role: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise ContextPackError(f"delta {role}_digest is not a sha256 digest")
    return value


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
