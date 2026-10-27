from __future__ import annotations

from app import ingest


def test_markdown_sections_and_title() -> None:
    text = "# Risk Factors\n\nRaw materials are important.\n\n## Liquidity\n\nCash flow remains strong.\n"
    title, sections, pages = ingest.parse_markdown(text, "sample.md")
    assert title == "Risk Factors"
    assert pages == 2
    assert [section.heading for section in sections] == ["Risk Factors", "Liquidity"]
    assert "Raw materials" in sections[0].text


def test_normalize_text_rejoins_hyphenation_and_drops_zero_width() -> None:
    text = "multi-\nline  text\u200b\r\n\r\n\r\nnext"
    normalized = ingest._normalize_text(text)
    assert "multiline text" in normalized
    assert "\u200b" not in normalized
    assert "\n\n\n" not in normalized


def test_chunk_sections_respects_limits_and_order() -> None:
    body = "\n\n".join(
        f"Paragraph {index}. " + ("evidence sentence. " * 12) for index in range(12)
    )
    chunks = ingest.chunk_sections([ingest.Section(page_no=3, heading="Body", text=body)], target=220, overlap=40)
    assert chunks
    assert all(chunk.page_no == 3 for chunk in chunks)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.text.strip() for chunk in chunks)
    assert all(len(chunk.text) <= 400 for chunk in chunks)


def test_plain_text_uses_filename_as_title() -> None:
    title, sections, pages = ingest.parse_text("A short note.", "reading-log.txt")
    assert title == "reading log"
    assert pages == 1
    assert sections[0].text == "A short note."
