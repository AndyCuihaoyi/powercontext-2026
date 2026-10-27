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

"""Source lifecycle DAG persistence."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.errors import InvalidRepositoryArgumentError
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)

_NODE_KINDS = ("source", "memory", "topic")
_POLICIES = ("all", "any")


class StoredLifecycleNode(BaseModel):
    """One registered dependency-graph node at its persisted availability."""

    scope_id: str
    node_id: str
    kind: str
    ordinal: int
    parents: tuple[str, ...]
    policy: str
    binding: dict[str, Any]
    artifact_id: str | None
    revision: int
    available: bool
    last_sequence: int | None


class StoredLifecycleEvent(BaseModel):
    """One accepted Source lifecycle event."""

    scope_id: str
    node_id: str
    sequence: int
    revision: int
    available: bool
    reason: str


class SourceLifecycleRepository:
    """Persist lifecycle nodes and events; callers own the transaction."""

    async def load_nodes(self, connection: AsyncConnection, scope_id: str, /) -> list[StoredLifecycleNode]:
        _require_identifier("scope_id", scope_id)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = (
            await connection.execute(
                select(table).where(table.c.scope_id == scope_id).order_by(table.c.ordinal, table.c.node_id)
            )
        ).mappings()
        return [_decode_node(row) for row in rows]

    async def load_node(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        for_update: bool = False,
    ) -> StoredLifecycleNode | None:
        _require_identifier("scope_id", scope_id)
        _require_identifier("node_id", node_id)
        statement = select(SOURCE_LIFECYCLE_NODES_TABLE).where(
            SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
            SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        return None if row is None else _decode_node(row)

    async def insert_node(self, connection: AsyncConnection, node: StoredLifecycleNode, /) -> bool:
        """Insert one node; return False when the identity already exists."""

        _require_identifier("scope_id", node.scope_id)
        _require_identifier("node_id", node.node_id)
        if node.kind not in _NODE_KINDS:
            raise InvalidRepositoryArgumentError("kind", "must be one of source, memory, topic")
        if node.policy not in _POLICIES:
            raise InvalidRepositoryArgumentError("policy", "must be 'all' or 'any'")
        values = {
            "scope_id": node.scope_id,
            "node_id": node.node_id,
            "kind": node.kind,
            "ordinal": node.ordinal,
            "parents": json.dumps(list(node.parents)).encode(),
            "policy": node.policy,
            "binding": json.dumps(node.binding, sort_keys=True).encode(),
            "artifact_id": node.artifact_id,
            "revision": node.revision,
            "available": node.available,
            "last_sequence": node.last_sequence,
        }
        if connection.dialect.name == "sqlite":
            statement = sqlite_insert(SOURCE_LIFECYCLE_NODES_TABLE).values(**values).on_conflict_do_nothing()
        elif connection.dialect.name == "mysql":
            statement = mysql_insert(SOURCE_LIFECYCLE_NODES_TABLE).values(**values).prefix_with("IGNORE")
        else:
            raise InvalidRepositoryArgumentError(
                "dialect", f"{connection.dialect.name!r} does not support lifecycle nodes"
            )
        return (await connection.execute(statement)).rowcount == 1

    async def update_node_state(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        available: bool,
        last_sequence: int | None = None,
        set_last_sequence: bool = False,
    ) -> None:
        _require_identifier("scope_id", scope_id)
        _require_identifier("node_id", node_id)
        values: dict[str, Any] = {"available": available}
        if set_last_sequence:
            values["last_sequence"] = last_sequence
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(**values)
        )

    async def load_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        sequence: int,
        /,
    ) -> StoredLifecycleEvent | None:
        _require_identifier("scope_id", scope_id)
        _require_identifier("node_id", node_id)
        table = SOURCE_LIFECYCLE_EVENTS_TABLE
        row = (
            await connection.execute(
                select(table).where(
                    table.c.scope_id == scope_id,
                    table.c.node_id == node_id,
                    table.c.sequence == sequence,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return StoredLifecycleEvent(
            scope_id=str(row["scope_id"]),
            node_id=str(row["node_id"]),
            sequence=int(row["sequence"]),
            revision=int(row["revision"]),
            available=bool(row["available"]),
            reason=str(row["reason"]),
        )

    async def insert_event(self, connection: AsyncConnection, event: StoredLifecycleEvent, /) -> bool:
        """Insert one event; return False when the identity already exists."""

        _require_identifier("scope_id", event.scope_id)
        _require_identifier("node_id", event.node_id)
        if event.sequence < 1:
            raise InvalidRepositoryArgumentError("sequence", "must be positive")
        values = {
            "scope_id": event.scope_id,
            "node_id": event.node_id,
            "sequence": event.sequence,
            "revision": event.revision,
            "available": event.available,
            "reason": event.reason,
        }
        if connection.dialect.name == "sqlite":
            statement = sqlite_insert(SOURCE_LIFECYCLE_EVENTS_TABLE).values(**values).on_conflict_do_nothing()
        elif connection.dialect.name == "mysql":
            statement = mysql_insert(SOURCE_LIFECYCLE_EVENTS_TABLE).values(**values).prefix_with("IGNORE")
        else:
            raise InvalidRepositoryArgumentError(
                "dialect", f"{connection.dialect.name!r} does not support lifecycle events"
            )
        return (await connection.execute(statement)).rowcount == 1

    async def unavailable_topic_artifact_ids(self, connection: AsyncConnection, scope_id: str, /) -> frozenset[str]:
        _require_identifier("scope_id", scope_id)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = await connection.scalars(
            select(table.c.artifact_id).where(
                table.c.scope_id == scope_id,
                table.c.kind == "topic",
                table.c.available.is_(False),
                table.c.artifact_id.is_not(None),
            )
        )
        return frozenset(str(row) for row in rows)


def _decode_node(row: Any) -> StoredLifecycleNode:
    return StoredLifecycleNode(
        scope_id=str(row["scope_id"]),
        node_id=str(row["node_id"]),
        kind=str(row["kind"]),
        ordinal=int(row["ordinal"]),
        parents=tuple(json.loads(_payload(row["parents"], "parents"))),
        policy=str(row["policy"]),
        binding=dict(json.loads(_payload(row["binding"], "binding"))),
        artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
        revision=int(row["revision"]),
        available=bool(row["available"]),
        last_sequence=None if row["last_sequence"] is None else int(row["last_sequence"]),
    )


def _payload(value: object, column: str) -> str:
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, bytearray | memoryview):
        return bytes(value).decode()
    raise InvalidRepositoryArgumentError(column, "payload column is not bytes")


def _require_identifier(field: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InvalidRepositoryArgumentError(field, "must be a non-empty trimmed string")
