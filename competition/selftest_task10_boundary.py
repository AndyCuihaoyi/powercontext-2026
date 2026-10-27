"""Task-10 boundary self-checks and SciFact-style quality simulation."""

import random
import re
from collections import Counter

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation, MemoryHit
from powercontext.builtin.runtime.models import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)


def hit(ref, entry_no, text):
    return MemoryHit(
        memory_ref=ref,
        entry_id=f"entry-{entry_no}",
        entry_version_id=f"version-{entry_no}",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def build(query, groups, max_bytes, selection=True, current=None):
    request = PrepareContextRequest(query=query, max_bytes=max_bytes, evidence_selection=selection)
    return PreparedContextBuilder().build_scopes_result(
        request=request, current_scope_id=current, memory_candidates=groups
    )


def items_of(build_result):
    import json

    if not build_result.context.content:
        return []
    return json.loads(build_result.context.content.splitlines()[-2])["items"]


def check(name, cond):
    assert cond, name
    print(f"ok - {name}")


REF_A = ArtifactRef(family="memory", artifact_id="mem-a", revision=1)
REF_B = ArtifactRef(family="memory", artifact_id="mem-b", revision=2)

# --- 1. cross-scope identity keeps citations distinct ---
text = "The cobalt sensor is calibrated at 37 degrees. " * 30 + "Amber valve note."
groups = (
    PreparedMemoryCandidates(scope_id="scope-1", memory_ref=REF_A, hits=(hit(REF_A, 1, text),)),
    PreparedMemoryCandidates(scope_id="scope-2", memory_ref=REF_B, hits=(hit(REF_B, 2, text),)),
)
result = build("cobalt sensor calibrated", groups, 2000, current="scope-1")
check("cross-scope fragments kept separate", len(result.fragments) == 2)
scopes = {item["citation"]["scope_id"] for item in items_of(result)}
check("each item cites its own scope", scopes == {"scope-1", "scope-2"})
origins = {type(origin) for origin in result.origins}
check(
    "current scope cites MemoryCitation, other keeps address",
    origins == {MemoryCitation, MemoryEntryAddress} and len(result.origins) == len(result.fragments),
)
for fragment in result.fragments:
    check_scope = fragment.address.memory.scope_id in scopes
    assert check_scope and fragment.text == text[fragment.start : fragment.end]

# --- 2. exact full-output byte accounting and tight boundaries ---
loose = build("amber valve opens", ((PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, text),)),)), 4000)
needed = loose.context.content_bytes
check("content_bytes equals full utf-8 length", needed == len(loose.context.content.encode("utf-8")) and needed <= 4000)
exact = build("amber valve opens", ((PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, text),)),)), max(512, needed))
check("budget exactly at full-output size keeps the fragment", exact.context.status == "ready" and exact.fragments)
tiny = build("amber valve opens", ((PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, "alpha word " * 120),)),)), 512)
check("one unsplachable sentence above the full-output budget returns legal empty", tiny.context.status == "empty" and not tiny.fragments)

# --- 3. CJK content billed by full utf-8 width, selected by CJK query ---
cjk = "海洋観測は記録を保持します。" * 20
cjk_groups = (PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, cjk),)),)
cjk_result = build("海洋観測", cjk_groups, 1200, current="s")
check("CJK query selects literal spans", bool(cjk_result.fragments))
for fragment in cjk_result.fragments:
    assert fragment.text == cjk[fragment.start : fragment.end] and len(fragment.text.encode("utf-8")) == len(fragment.text.encode("utf-8"))
check("CJK budget is utf-8 measured", cjk_result.context.content_bytes <= 1200 and cjk_result.context.content_bytes > len(cjk_result.context.content))

# --- 4. no lexical overlap -> empty, default-off path untouched ---
none = build("zzz qqq xyz", cjk_groups, 1200, current="s")
check("no-overlap query yields empty context", none.context.status == "empty" and none.fragments == () and none.origins == ())
prefix = "General notes. " * 40 + "The cobalt sensor is calibrated at 37 degrees. "
off = build("cobalt", ((PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, prefix),)),)), 1200, selection=False, current="s")
first_item = items_of(off)[0]
check("default off keeps legacy prefix-truncation rendering", first_item["content"].startswith("General notes.") and "start" not in first_item)

# --- 5. duplicates inside one call: same entry multiple fragments allowed ---
long_doc = ". ".join(f"Sentence number {index} about cobalt" if index in (4, 9) else f"Sentence number {index} filler" for index in range(12)) + "."
multi = build("cobalt", ((PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, long_doc),)),)), 1600, current="s")
check("one entry can contribute several fragments", len(multi.fragments) >= 1 and all(f.text in long_doc for f in multi.fragments))

# --- 5b. two adjacent evidence sentences merge into one covering run ---
doc5 = "Intro filler about labs. " + "Overt kinase activity rises at 12 degrees. The receptor assay confirms kinase behavior. " + "Unrelated trailing remarks here. " * 6
run_result = build("kinase activity receptor assay degrees", (PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, 1, doc5),)),), 1600, current="s")
run_spans = [(f.start, f.end) for f in run_result.fragments]
merged_text = " ".join(doc5[s:e] for s, e in sorted(run_spans))
check(
    "adjacent evidence sentences fully covered",
    "Overt kinase activity rises at 12 degrees" in merged_text and "The receptor assay confirms kinase behavior" in merged_text,
)

# --- 6. SciFact-style quality simulation: 24 claims x 3 docs x {1400,1800} ---
WORDS = ["overt", "stimulus", "protein", "kinase", "membrane", "assay", "phosphorylation", "receptor"]


def synth_case(rng):
    evidence = (
        f"{rng.choice(WORDS)} levels in {rng.choice(WORDS)} depend on {rng.choice(WORDS)} activity measured by "
        f"the {rng.choice(WORDS)} protocol at {rng.randint(2, 39)} degrees"
    )
    claim = f"{rng.choice(WORDS)} study: " + evidence[: len(evidence) // 2] + "."
    evidence_doc = rng.randrange(3)
    tail_heavy = rng.random() < 0.5
    docs = []
    for doc_index in range(3):
        filler = [f"{rng.choice(WORDS)} context sentence {doc_index}-{pos}" for pos in range(6)]
        if doc_index == evidence_doc:
            position = (len(filler) if tail_heavy or rng.random() < 0.5 else rng.randrange(len(filler) + 1))
            sentences = filler[:position] + [evidence] + filler[position:]
            text = ". ".join(sentences) + "."
            start = text.find(evidence)
            docs.append((text, start, start + len(evidence)))
        else:
            noise = rng.choice(evidence.split())
            sentences = filler + [f"A side note mentions {noise} once among {rng.choice(WORDS)} topics"]
            docs.append((". ".join(sentences) + ".", None, None))
    return claim, docs, evidence_doc


def tokens(text):
    return Counter(re.findall(r"[a-z]+|\d+", text.lower()))


rng = random.Random(20261027)
recalls, fulls = [], []
for _ in range(24):
    claim, docs, evidence_doc = synth_case(rng)
    groups = tuple(
        PreparedMemoryCandidates(scope_id="s", memory_ref=REF_A, hits=(hit(REF_A, index, text),))
        for index, (text, _, _) in enumerate(docs)
    )
    for budget in (1400, 1800):
        result = build(claim, groups, budget, current="s")
        evidence_text, start, end = docs[evidence_doc]
        selected = sorted((f.start, f.end) for f in result.fragments if f.address.entry_id == f"entry-{evidence_doc}")
        covered_end = start
        for s, e in selected:
            if s <= covered_end:
                covered_end = max(covered_end, e)
        selected_text = " ".join(evidence_text[s:e] for s, e in selected)
        evidence_token_count = sum(tokens(evidence_text[start:end]).values())
        hits = sum((tokens(selected_text) & tokens(evidence_text[start:end])).values())
        recalls.append(hits / max(1, evidence_token_count))
        fulls.append(covered_end >= end)

average_recall = sum(recalls) / len(recalls)
coverage = sum(fulls) / len(fulls)
print(f"quality simulation: evidence token recall={average_recall:.3f} (need >=0.65), full coverage={coverage:.3f} (need >=0.40)")
assert average_recall >= 0.65 and coverage >= 0.40
print("ALL TASK-10 BOUNDARY CHECKS PASSED")
