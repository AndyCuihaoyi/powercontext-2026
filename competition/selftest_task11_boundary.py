"""Boundary checks for retrieval-02: one native hybrid candidate statement on seekdb."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import Engine, event

from powercontext.builtin.artifacts.memory import CapabilityNotSupportedError, EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

PROFILE = EmbeddingProfile(
    profile_id="retrieval-02-hybrid-search-selftest-v1",
    model="deterministic",
    dimension=3,
    distance="l2",
    normalization="unit",
)


class _Embedding:
    profile = PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple(
                (1.0, 0.0, 0.0)
                if "alpha" in text.casefold()
                else (0.0, 0.0, 1.0)
                if "gamma" in text.casefold()
                else (0.0, 1.0, 0.0)
                for text in texts
            )
        )


def _listen(engine_target: Engine) -> list[tuple[str, object]]:
    statements: list[tuple[str, object]] = []

    def record(_connection, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", record)
    return statements


def _is_candidate(statement: str) -> bool:
    upper = statement.upper()
    return "AGAINST" in upper or "l2_distance" in statement.lower()


def _is_hybrid_candidate(statement: str) -> bool:
    return (
        "pc_memory_entry_heads" in statement
        and "pc_memory_vector_entries" in statement
        and "AGAINST" in statement.upper()
        and "l2_distance" in statement.lower()
    )


async def main() -> None:  # noqa: PLR0915
    database_path = Path(tempfile.mkdtemp(prefix="retrieval-02-selftest-")) / "db"
    async with open_builtin_contexts(
        BuiltinConfig(database=SeekDBConfig(path=database_path)), embedding_model=_Embedding()
    ) as contexts:
        memory = (await contexts.get("project")).artifacts.memory
        head = await memory.remember(
            memory=None,
            entries=(
                MemoryEntryInput(kind="fact", text="Alpha semantic record."),
                MemoryEntryInput(kind="fact", text="Beta lexical record."),
                MemoryEntryInput(kind="fact", text="海洋观测站记录温度。"),
            ),
            mode="append",
        )
        assert head is not None

        # 1. Exactly one candidate statement per hybrid search, and it is the fused one.
        statements = _listen(Engine)
        result = await memory.search("alpha", memories=(head,), mode="hybrid", limit=5)
        candidates = [s for s, _ in statements if _is_candidate(s)]
        hybrid = [s for s in candidates if _is_hybrid_candidate(s)]
        assert len(statements) > 0
        assert len(candidates) == 1, f"{len(candidates)} candidate statements in one hybrid search"
        assert len(hybrid) == 1
        assert len(result.hits) == 1
        assert result.hits[0].text == "Alpha semantic record."
        assert result.hits[0].matched_by == ("fts", "vector")

        # 2. Repeat searches stay single-statement and stable in identity and order.
        first = [(hit.entry_id, hit.entry_version_id, hit.memory_ref) for hit in result.hits]
        statements.clear()
        repeated = await memory.search("alpha", memories=(head,), mode="hybrid", limit=5)
        second = [(hit.entry_id, hit.entry_version_id, hit.memory_ref) for hit in repeated.hits]
        assert second == first
        assert len([s for s, _ in statements if _is_candidate(s)]) == 1

        # 3. Different queries on the same data select different evidence.
        statements.clear()
        lexical = await memory.search("beta", memories=(head,), mode="hybrid", limit=1)
        assert lexical.hits[0].text == "Beta lexical record."
        assert len([s for s, _ in statements if _is_candidate(s)]) == 1

        # 4. FTS-only and vector-only public behaviour is unchanged (two separate paths).
        statements.clear()
        fts = await memory.search("alpha", memories=(head,), mode="fts")
        fts_candidates = [s for s, _ in statements if _is_candidate(s)]
        assert len(fts_candidates) == 1 and not _is_hybrid_candidate(fts_candidates[0])
        assert "AGAINST" in fts_candidates[0].upper() and "l2_distance" not in fts_candidates[0].lower()
        statements.clear()
        vector = await memory.search("alpha", memories=(head,), mode="vector")
        vector_candidates = [s for s, _ in statements if _is_candidate(s)]
        assert len(vector_candidates) == 1 and not _is_hybrid_candidate(vector_candidates[0])
        assert "l2_distance" in vector_candidates[0].lower() and "AGAINST" not in vector_candidates[0].upper()
        assert fts.hits[0].text == vector.hits[0].text == "Alpha semantic record."

        # 5. Hybrid with a tag filter: exactly one fused statement, filtered in-database.
        beta = next(entry for entry in await memory.entries(head) if entry.text == "Beta lexical record.")
        target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=beta.entry_id)
        empty = await contexts.records.get_tags("project", target)
        await contexts.records.replace_tags("project", target, ("keep",), expected_etag=empty.etag)
        statements.clear()
        tagged = await memory.search(
            "beta", memories=(head,), mode="hybrid", limit=5, tag_filter=TagFilter(tags=("keep",))
        )
        tagged_candidates = [s for s, _ in statements if _is_candidate(s)]
        assert len(tagged_candidates) == 1 and _is_hybrid_candidate(tagged_candidates[0])
        assert [hit.entry_id for hit in tagged.hits] == [beta.entry_id]
        # match=all with two keys, one of which is absent -> no hit.
        miss = await memory.search(
            "beta", memories=(head,), mode="hybrid", limit=5, tag_filter=TagFilter(tags=("keep", "absent"))
        )
        assert len(miss.hits) == 0
        any_hit = await memory.search(
            "beta", memories=(head,), mode="hybrid", limit=5, tag_filter=TagFilter(tags=("absent",), match="any")
        )
        assert len(any_hit.hits) == 0

        # 6. A query with no lexical overlap still runs exactly one fused statement.
        statements.clear()
        semantic = await memory.search("gamma", memories=(head,), mode="hybrid", limit=5)
        assert len([s for s, _ in statements if _is_candidate(s)]) == 1
        assert isinstance(semantic.hits, tuple)

        # 7. Revision, deactivation, and reactivation keep references at the current version.
        alpha = next(entry for entry in await memory.entries(head) if entry.text == "Alpha semantic record.")
        revised = await memory.remember(
            memory=head,
            entries=(MemoryEntryInput(kind="fact", text="Alpha revised record.", entry=alpha),),
            mode="append",
        )
        assert revised is not None
        revised_hit = (await memory.search("alpha", memories=(revised,), mode="hybrid", limit=1)).hits[0]
        assert revised_hit.entry_id == alpha.entry_id
        assert revised_hit.entry_version_id != alpha.entry_version_id
        assert revised_hit.text == "Alpha revised record."
        assert revised_hit.memory_ref == revised.as_ref()
        current_alpha = next(entry for entry in await memory.entries(revised) if entry.entry_id == alpha.entry_id)
        inactive = await memory.forget(revised, entries=(current_alpha,))
        inactive_hits = await memory.search("alpha", memories=(inactive,), mode="hybrid", limit=10)
        assert all(hit.entry_id != alpha.entry_id for hit in inactive_hits.hits)
        restored = await memory.reactivate(inactive, entries=(current_alpha,))
        restored_hit = (await memory.search("alpha", memories=(restored,), mode="hybrid", limit=1)).hits[0]
        assert restored_hit.entry_version_id == current_alpha.entry_version_id
        assert restored_hit.memory_ref == restored.as_ref()

        # 8. Stale heads are rejected through the existing capability contract.
        try:
            await memory.search("alpha", memories=(head,), mode="hybrid", limit=1)
        except CapabilityNotSupportedError:
            pass
        else:
            raise AssertionError("stale head must raise CapabilityNotSupportedError")

        # 9. Scope isolation under one fused statement.
        other = (await contexts.get("other-scope")).artifacts.memory
        other_head = await other.remember(
            memory=None, entries=(MemoryEntryInput(kind="fact", text="Alpha decoy in another scope."),), mode="append"
        )
        assert other_head is not None
        isolated = await memory.search("alpha", memories=(restored,), mode="hybrid", limit=10)
        assert all(hit.memory_ref.artifact_id == restored.artifact_id for hit in isolated.hits)

        # 10. Unicode lexical matching through the fused statement.
        unicode_hits = await memory.search("海洋观测 温度", memories=(restored,), mode="hybrid", limit=5)
        assert any("海洋观测站记录温度。" == hit.text for hit in unicode_hits.hits)

        # 11. The fused plan keeps both native index operators.
        fused = next(s for s, _ in statements if _is_hybrid_candidate(s))
        parameters = next(p for s, p in statements if _is_hybrid_candidate(s))
        async with contexts.database.transaction() as connection:
            plan = "\n".join(
                str(row[0]) for row in (await connection.exec_driver_sql(f"EXPLAIN {fused}", parameters)).all()
            )
        assert "TEXT RETRIEVAL SCAN" in plan
        assert "VECTOR INDEX" in plan
    print("retrieval-02 boundary self-test: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
