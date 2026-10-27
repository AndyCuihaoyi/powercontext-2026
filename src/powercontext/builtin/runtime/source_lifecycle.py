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

"""Source-driven availability propagation over an explicit dependency DAG."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator

from powercontext.artifacts.models import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.source_lifecycle import (
    SourceLifecycleRepository,
    StoredLifecycleEvent,
    StoredLifecycleNode,
)
from powercontext.sources.models import SourceRef
from powercontext.builtin.sources import validate_scope_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from powercontext.builtin.runtime.application import BuiltinRuntime

_PURPOSES = ("generation", "audit")
_POLICIES = ("all", "any")
_KINDS = ("source", "memory", "topic")


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event, or resolution violated its contract."""


def _require_node_id(value: str, /) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SourceLifecycleError("node_id must be a non-empty trimmed string")
    return value


class SourceLifecycleEvent(BaseModel):
    """One Source availability statement; replayed events are idempotent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: StrictInt
    sequence: StrictInt
    available: StrictBool
    reason: str

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, value: str) -> str:
        return _require_node_id(value)

    @field_validator("revision", "sequence")
    @classmethod
    def _validate_positive(cls, value: int, info) -> int:
        if value < 1:
            raise SourceLifecycleError(f"{info.field_name} must be a positive integer")
        return value

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise SourceLifecycleError("reason must not be blank")
        return value


class ScopedSourceLifecycleService:
    """Register one Scope's evidence DAG and propagate Source availability."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)
        self._database = database
        self._repository = SourceLifecycleRepository()

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        _require_node_id(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef")
        _require_revision(revision)
        async with self._runtime._scoped_operation(self.scope_id):
            async with self._runtime._locked(self.scope_id):
                binding = _binding("source", source_ref)
                await self._require_source_readable(source_ref, node_id)
                await self._register(
                    node_id,
                    kind="source",
                    binding=binding,
                    parents=(),
                    policy="all",
                    revision=revision,
                    artifact_id=None,
                )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: Literal["all", "any"],
    ) -> None:
        node_id = _require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation")
        parents = _require_parents(parents, node_id)
        _require_policy(policy)
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                binding = _binding("memory", citation)
                await self._require_citation_readable(context, citation, node_id)
                available = await self._register(
                    node_id,
                    kind="memory",
                    binding=binding,
                    parents=parents,
                    policy=policy,
                    revision=0,
                    artifact_id=None,
                )
                await self._sync_memory_node(
                    context, node_id, binding, available, reason="source-lifecycle-registration"
                )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: Literal["all", "any"],
    ) -> None:
        node_id = _require_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("artifact_ref must be an ArtifactRef")
        parents = _require_parents(parents, node_id)
        _require_policy(policy)
        async with self._runtime._scoped_operation(self.scope_id):
            async with self._runtime._locked(self.scope_id):
                binding = _binding("topic", artifact_ref)
                await self._require_topic_readable(artifact_ref, node_id)
                await self._register(
                    node_id,
                    kind="topic",
                    binding=binding,
                    parents=parents,
                    policy=policy,
                    revision=0,
                    artifact_id=artifact_ref.artifact_id,
                )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")
        if self._database is None:
            raise SourceLifecycleError("source lifecycle persistence is not configured")
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                await self._accept_event(event)
                nodes = await self._load_nodes()
                availability = _recompute(nodes)
                for node in nodes:
                    if node.kind == "memory":
                        await self._sync_memory_node(
                            context,
                            node.node_id,
                            node.binding,
                            availability[node.node_id],
                            reason=event.reason,
                        )
                await self._persist_availability(nodes, availability)
                return sorted(node_id for node_id, supported in availability.items() if supported)

    async def resolve(self, node_id: str, /, *, purpose: Literal["generation", "audit"]) -> object:
        _require_node_id(node_id)
        if purpose not in _PURPOSES:
            raise SourceLifecycleError(f"unknown lifecycle purpose: {purpose!r}")
        node = await self._load_node(node_id)
        if node is None:
            raise SourceLifecycleError(f"unknown lifecycle node: {node_id}")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"node is not available for generation: {node_id}")
        return _decode_binding(node)

    async def _register(
        self,
        node_id: str,
        /,
        *,
        kind: str,
        binding: dict[str, Any],
        parents: tuple[str, ...],
        policy: str,
        revision: int,
        artifact_id: str | None,
    ) -> bool:
        """Insert one node and return its computed availability; identical replay is a no-op."""

        if self._database is None:
            raise SourceLifecycleError("source lifecycle persistence is not configured")
        async with self._database.transaction() as connection:
            existing = await self._repository.load_node(connection, self.scope_id, node_id, for_update=True)
            if existing is not None:
                _require_identical(
                    existing, kind=kind, binding=binding, parents=parents, policy=policy, revision=revision
                )
                return existing.available
            nodes = await self._repository.load_nodes(connection, self.scope_id)
            by_id = {node.node_id: node for node in nodes}
            unknown = [parent for parent in parents if parent not in by_id]
            if unknown:
                raise SourceLifecycleError(f"parent nodes are not registered: {', '.join(sorted(unknown))}")
            ordinal = max((node.ordinal for node in nodes), default=-1) + 1
            availability = _recompute_from(nodes, node_id, parents, policy)
            created = StoredLifecycleNode(
                scope_id=self.scope_id,
                node_id=node_id,
                kind=kind,
                ordinal=ordinal,
                parents=parents,
                policy=policy,
                binding=binding,
                artifact_id=artifact_id,
                revision=revision,
                available=availability,
                last_sequence=0 if kind == "source" else None,
            )
            if not await self._repository.insert_node(connection, created):
                existing = await self._repository.load_node(connection, self.scope_id, node_id, for_update=True)
                if existing is None:
                    raise SourceLifecycleError(f"lifecycle node vanished during registration: {node_id}")
                _require_identical(
                    existing, kind=kind, binding=binding, parents=parents, policy=policy, revision=revision
                )
                return existing.available
            return availability

    async def _accept_event(self, event: SourceLifecycleEvent) -> None:
        async with self._database.transaction() as connection:
            node = await self._repository.load_node(connection, self.scope_id, event.node_id, for_update=True)
            if node is None or node.kind != "source":
                raise SourceLifecycleError(f"unknown lifecycle source: {event.node_id}")
            if node.revision != event.revision:
                raise SourceLifecycleError(
                    f"lifecycle revision mismatch for {event.node_id}: expected {node.revision}, got {event.revision}"
                )
            last_sequence = node.last_sequence or 0
            stored = await self._repository.load_event(connection, self.scope_id, event.node_id, event.sequence)
            if event.sequence < last_sequence or (stored is not None and event.sequence <= last_sequence):
                if stored is None or stored.revision != event.revision or stored.available != event.available:
                    raise SourceLifecycleError(
                        f"lifecycle sequence must increase for {event.node_id}: {event.sequence} after {last_sequence}"
                    )
                return
            if not await self._repository.insert_event(
                connection,
                StoredLifecycleEvent(
                    scope_id=self.scope_id,
                    node_id=event.node_id,
                    sequence=event.sequence,
                    revision=event.revision,
                    available=event.available,
                    reason=event.reason,
                ),
            ):
                raise SourceLifecycleError(
                    f"lifecycle sequence must increase for {event.node_id}: {event.sequence} after {last_sequence}"
                )
            await self._repository.update_node_state(
                connection,
                self.scope_id,
                event.node_id,
                available=event.available,
                last_sequence=event.sequence,
                set_last_sequence=True,
            )

    async def _load_nodes(self) -> list[StoredLifecycleNode]:
        async with self._database.transaction() as connection:
            return await self._repository.load_nodes(connection, self.scope_id)

    async def _load_node(self, node_id: str) -> StoredLifecycleNode | None:
        async with self._database.transaction() as connection:
            return await self._repository.load_node(connection, self.scope_id, node_id)

    async def _persist_availability(self, nodes: Sequence[StoredLifecycleNode], availability: dict[str, bool]) -> None:
        async with self._database.transaction() as connection:
            for node in nodes:
                if node.available != availability[node.node_id]:
                    await self._repository.update_node_state(
                        connection, self.scope_id, node.node_id, available=availability[node.node_id]
                    )

    async def _sync_memory_node(
        self,
        context: Any,
        node_id: str,
        binding: dict[str, Any],
        available: bool,
        /,
        *,
        reason: str,
    ) -> None:
        """Move one registered Memory entry to its computed state; equal state is a no-op."""

        if _binding_kind(binding) != "memory":
            return
        service = context.artifacts.memory
        citation = MemoryCitation.model_validate(binding["binding"])
        memory = await service.head(context.artifacts.memory_artifact_id)
        if memory is None:
            raise SourceLifecycleError(f"registered Memory entry is not readable: {node_id}")
        manifest_entry = next(
            (
                item
                for item in memory.content.manifest.entries
                if item.entry_id == citation.entry_id and item.entry_version_id == citation.entry_version_id
            ),
            None,
        )
        if manifest_entry is None:
            raise SourceLifecycleError(f"registered Memory entry is not readable: {node_id}")
        target = "active" if available else "inactive"
        if manifest_entry.state == target:
            return
        entry = await service.validate_citation(citation)
        if available:
            await service.reactivate(memory, entries=(entry,), reason=reason)
        else:
            await service.forget(memory, entries=(entry,), reason=reason)

    async def _require_source_readable(self, source_ref: SourceRef, node_id: str) -> None:
        try:
            records = self._runtime.records.for_scope(self.scope_id)
            await records.get_source(source_ref.source_type, source_ref.source_id)
        except Exception as error:
            raise SourceLifecycleError(f"registered Source is not readable: {node_id}") from error

    async def _require_topic_readable(self, artifact_ref: ArtifactRef, node_id: str) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
            )
        except Exception as error:
            raise SourceLifecycleError(f"registered Topic artifact is not readable: {node_id}") from error

    async def _require_citation_readable(self, context: Any, citation: MemoryCitation, node_id: str) -> None:
        from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCitationError, MemoryEntryNotFoundError
        from powercontext.errors import ArtifactNotFoundError

        service = context.artifacts.memory
        try:
            memory = await service.revision(citation.memory_ref)
            if not any(
                item.entry_id == citation.entry_id and item.entry_version_id == citation.entry_version_id
                for item in memory.content.manifest.entries
            ):
                raise MemoryEntryNotFoundError(citation.entry_id)
            await service.validate_citation(citation)
        except (ArtifactNotFoundError, InvalidMemoryCitationError, MemoryEntryNotFoundError) as error:
            raise SourceLifecycleError(f"registered Memory citation is not readable: {node_id}") from error


class SourceLifecycleApplication:
    """Select the lifecycle service for one Scope and expose native search filtering."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        return ScopedSourceLifecycleService(self._runtime, scope_id, self._database)

    async def unavailable_topic_artifacts(self, scope_id: str, /) -> frozenset[str]:
        """Artifact ids this Scope's lifecycle DAG currently holds out of native search."""

        if self._database is None:
            return frozenset()
        async with self._database.transaction() as connection:
            return await SourceLifecycleRepository().unavailable_topic_artifact_ids(connection, scope_id)


def _recompute(nodes: Sequence[StoredLifecycleNode]) -> dict[str, bool]:
    by_id = {node.node_id: node for node in nodes}
    availability: dict[str, bool] = {}
    for node in nodes:
        if node.kind == "source":
            availability[node.node_id] = node.available
            continue
        support = [availability.get(parent, False) for parent in node.parents]
        availability[node.node_id] = all(support) if node.policy == "all" else any(support)
    return availability


def _recompute_from(
    nodes: Sequence[StoredLifecycleNode],
    node_id: str,
    parents: tuple[str, ...],
    policy: str,
) -> bool:
    """Registration-time state: derived nodes never default to available."""

    known = {node.node_id: node.available for node in nodes if node.node_id != node_id}
    support = [known.get(parent, False) for parent in parents]
    return all(support) if policy == "all" else any(support)


def _require_identical(
    existing: StoredLifecycleNode,
    /,
    *,
    kind: str,
    binding: dict[str, Any],
    parents: tuple[str, ...],
    policy: str,
    revision: int,
) -> None:
    if (
        existing.kind != kind
        or existing.binding != binding
        or existing.parents != parents
        or existing.policy != policy
        or (kind == "source" and existing.revision != revision)
    ):
        raise SourceLifecycleError(f"node is already registered with a different binding: {existing.node_id}")


def _binding(kind: str, reference: BaseModel) -> dict[str, Any]:
    return {"kind": kind, "binding": reference.model_dump(mode="json")}


def _binding_kind(binding: dict[str, Any]) -> str:
    kind = binding.get("kind")
    if kind not in _KINDS:
        raise SourceLifecycleError(f"unknown lifecycle binding kind: {kind!r}")
    return str(kind)


def _decode_binding(node: StoredLifecycleNode) -> object:
    kind = _binding_kind(node.binding)
    payload = node.binding["binding"]
    if kind == "source":
        return SourceRef.model_validate(payload)
    if kind == "memory":
        return MemoryCitation.model_validate(payload)
    return ArtifactRef.model_validate(payload)


def _require_revision(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceLifecycleError("revision must be a positive integer")


def _require_policy(value: str) -> None:
    if value not in _POLICIES:
        raise SourceLifecycleError(f"policy must be 'all' or 'any', got {value!r}")


def _require_parents(parents: Sequence[str], node_id: str) -> tuple[str, ...]:
    if not isinstance(parents, tuple | list) or any(not isinstance(parent, str) for parent in parents):
        raise SourceLifecycleError("parents must be a sequence of node ids")
    tuple_parents = tuple(parents)
    if not tuple_parents:
        raise SourceLifecycleError(f"derived node requires at least one parent: {node_id}")
    if len(set(tuple_parents)) != len(tuple_parents):
        raise SourceLifecycleError(f"parent nodes must not repeat: {node_id}")
    if node_id in tuple_parents:
        raise SourceLifecycleError(f"dependency graph must not contain cycles: {node_id}")
    return tuple_parents
