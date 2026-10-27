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

"""Capture one selected Pi session branch as immutable per-event Sources."""

from __future__ import annotations

import codecs
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME: Final = "pi-event"
PI_EVENT_SOURCE_VERSION: Final = "1"
PI_SESSION_CONNECTOR_NAME: Final = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION: Final = "1"

_IDENTITY_PREFIX: Final = "pi"
_CHECKPOINT_VERSION: Final = 1


class PiSessionInput(BaseModel):
    """One offline Pi v3 JSONL archive and the branch leaf to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, retaining its raw record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A durable Source for exactly one visible Pi session event."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable identity shared by repeated captures of one event."""

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    readable = f"{_IDENTITY_PREFIX}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(readable) <= MAX_SOURCE_ID_LENGTH:
        return readable
    digest = hashlib.sha256()
    for part in (session_id, event_id):
        encoded = part.encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b":")
        digest.update(encoded)
    return f"{_IDENTITY_PREFIX}:{digest.hexdigest()}"


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse the selected branch into visible events ordered from root to leaf."""

    if not isinstance(value, PiSessionInput):
        raise ValueError("value must be a PiSessionInput")
    return _parse_graph(value.content, value.leaf_id).events


@dataclass(frozen=True, slots=True)
class _Graph:
    session_id: str
    events: tuple[PiEventInput, ...]
    records: tuple[dict[str, JsonValue], ...]


def _parse_graph(content: bytes, leaf_id: str | None) -> _Graph:
    raw = bytes(content)
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8) :]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("content must be valid UTF-8") from error

    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as error:
            raise ValueError(f"content is not valid JSONL: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError("each JSONL record must be a JSON object")
        records.append(parsed)

    if not records:
        raise ValueError("content must contain a session header")
    header = records[0]
    if header.get("type") != "session":
        raise ValueError("first record must be a session header")
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 3:
        raise ValueError("session version must be 3")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session id must be a non-empty string")

    nodes: dict[str, dict[str, JsonValue]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event id must be a non-empty string")
        if event_id in nodes or event_id == session_id:
            raise ValueError(f"duplicate event id: {event_id!r}")
        if "parentId" not in record:
            raise ValueError("event record must contain parentId")
        parent_id = record["parentId"]
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError("parentId must be null or a non-empty string")
            if parent_id not in nodes:
                raise ValueError(f"missing parent: {parent_id!r}")
        nodes[event_id] = record
        order.append(event_id)

    if not order:
        raise ValueError("session must contain at least one event")
    if leaf_id is None:
        selected = order[-1]
    elif isinstance(leaf_id, str) and leaf_id in nodes:
        selected = leaf_id
    else:
        raise ValueError(f"leaf not found: {leaf_id!r}")

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = selected
    while current is not None:
        if current in seen:
            raise ValueError("session contains a parent cycle")
        seen.add(current)
        if current not in nodes:
            raise ValueError(f"missing parent: {current!r}")
        chain.append(current)
        parent = nodes[current].get("parentId")
        current = parent if isinstance(parent, str) else None
    chain.reverse()

    events = tuple(
        event
        for event in (_visible_event(session_id, nodes[event_id]) for event_id in chain)
        if event is not None
    )
    return _Graph(
        session_id=session_id,
        events=events,
        records=tuple(nodes[event_id] for event_id in order),
    )


def _reject_json_constant(value: str) -> JsonValue:
    raise ValueError(f"invalid JSON constant: {value}")


def _visible_event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    kind = record.get("type")
    if kind == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("message event must contain a message object")
        role = message.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("message role must be a non-empty string")
        if role == "toolResult":
            body = _render_tool_result(message)
        elif role == "bashExecution":
            command = message.get("command")
            output = message.get("output")
            body = f"$ {command if isinstance(command, str) else ''}\n"
            body += output if isinstance(output, str) else ""
        else:
            body = _render_message_content(message.get("content"))
        event_type = "message"
    elif kind == "compaction" or kind == "branch_summary":
        role = kind
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
        event_type = kind
    elif kind == "custom_message":
        role = "custom"
        body = _render_text_content(record.get("content"))
        event_type = kind
    else:
        return None

    if not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record.get("parentId"),
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _render_message_content(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "toolCall":
            parts.append(_render_tool_call(block))
    return "\n".join(parts)


def _render_text_content(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _render_tool_call(block: dict[str, JsonValue]) -> str:
    name = _token(block.get("name"))
    call_id = _token(block.get("id"))
    return f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments', {}))}"


def _render_tool_result(message: dict[str, JsonValue]) -> str:
    name = _token(message.get("toolName"))
    call_id = _token(message.get("toolCallId"))
    return f"[tool_result {name} {call_id}] {_render_text_content(message.get('content'))}"


def _token(value: JsonValue) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _compact_json(value: JsonValue) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _PiEventProjection:
    """Expose one session event as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


@dataclass(frozen=True, slots=True)
class _PiEventSourceDefinition:
    """Bind the Pi event adapter to its durable version and projection."""

    name: str = PI_EVENT_SOURCE_NAME
    version: str = PI_EVENT_SOURCE_VERSION
    input_class: type[PiEventInput] = PiEventInput
    source_class: type[PiEventSource] = PiEventSource
    projections: tuple[_PiEventProjection, ...] = (_PiEventProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        if not isinstance(value, PiEventInput):
            raise ValueError("value must be a PiEventInput")
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION: _PiEventSourceDefinition = _PiEventSourceDefinition()


@dataclass(frozen=True, slots=True)
class PiSessionConnector:
    """Import visible events from one selected branch with durable progress."""

    path: str | Path
    leaf_id: str | None = None
    name: str = PI_SESSION_CONNECTOR_NAME
    version: str = PI_SESSION_CONNECTOR_VERSION
    source_definitions: frozenset[str] = frozenset({PI_EVENT_SOURCE_NAME})

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        try:
            content = Path(self.path).read_bytes()
        except OSError as error:
            raise ValueError(f"cannot read Pi session: {error}") from error
        graph = _parse_graph(content, self.leaf_id)
        stored = _load_checkpoint(session.checkpoint)

        for record in graph.records:
            source_id = pi_event_source_id(graph.session_id, record["id"])
            if source_id in stored and stored[source_id] != _record_digest(record):
                raise ValueError(f"already imported event changed: {record['id']!r}")

        imported = dict(stored)
        for event in graph.events:
            source_id = pi_event_source_id(event.session_id, event.event_id)
            if source_id in stored:
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                imported[source_id] = _record_digest(event.record)

        checkpoint = {
            "version": _CHECKPOINT_VERSION,
            "events": {source_id: imported[source_id] for source_id in sorted(imported)},
        }
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)


def _load_checkpoint(value: JsonValue) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Pi session checkpoint must be a JSON object")
    if value.get("version") != _CHECKPOINT_VERSION:
        raise ValueError("unsupported Pi session checkpoint version")
    events = value.get("events")
    if not isinstance(events, dict):
        raise ValueError("Pi session checkpoint must contain an events object")
    stored: dict[str, str] = {}
    for source_id, digest in events.items():
        if not isinstance(source_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint entries must be strings")
        stored[source_id] = digest
    return stored


def _record_digest(record: dict[str, JsonValue]) -> str:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_VERSION",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
