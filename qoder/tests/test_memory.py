from __future__ import annotations

from app import memory


def test_kind_normalization_and_tags() -> None:
    assert memory._normalize_kind("PREFERENCE") == "preference"
    assert memory._normalize_kind("progress") == "progress"
    assert memory._normalize_kind("anything-else") == "insight"
    assert memory._kind_tag("preference", "local") == "preference@local"
    assert memory._kind_from_tag("", ["insight@alice"]) == "insight"
    assert memory._kind_from_tag("", ["other@alice"]) == ""


def test_trim_text_collapses_whitespace_and_bounds_utf8() -> None:
    assert memory._trim_text("  one\n\t two  ") == "one two"
    trimmed = memory._trim_text("记忆" * 3000)
    assert len(trimmed.encode("utf-8")) <= 8000
    assert trimmed.startswith("记忆")
