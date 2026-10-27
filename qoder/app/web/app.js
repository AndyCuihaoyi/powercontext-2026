(() => {
  "use strict";

  const state = {
    home: null,
    health: null,
    memory: null,
    collection: "",
    activeDocId: null,
    currentQuery: "",
    lastAsk: null,
    citations: {},
    lastHits: [],
    activeCitation: null,
    page: null,
    view: "text",
    busy: false,
    demoBusy: false,
    statsLoaded: false,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(path, options = {}) {
    const opts = { ...options };
    opts.headers = { ...(options.headers || {}) };
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      if (typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, opts);
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.error || payload.message);
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return payload;
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("error", Boolean(isError));
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 5200 : 3200);
  }

  function num(value) {
    return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
  }

  function kindLabel(kind) {
    if (kind === "pdf") return "PDF";
    if (kind === "markdown") return "MD";
    return "TXT";
  }

  function pageLabel(kind, pageNo) {
    return kind === "markdown" ? `§${pageNo}` : `p.${pageNo}`;
  }

  function formatTime(value) {
    if (!value) return "";
    const text = String(value);
    const parsed = new Date(text.includes("T") ? text : text.replace(" ", "T") + "Z");
    if (Number.isNaN(parsed.getTime())) return text.slice(0, 16);
    return parsed.toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  function setChip(selector, ok, value, label) {
    const chip = $(selector);
    chip.classList.toggle("ok", ok === true);
    chip.classList.toggle("bad", ok === false);
    const valueNode = $(".value", chip);
    if (valueNode) valueNode.textContent = value;
    if (label) {
      const labelNode = $(".label", chip);
      if (labelNode) labelNode.textContent = label;
    }
  }

  function chip(content, className = "") {
    return el("span", { class: `chip ${className}`.trim(), text: content });
  }

  function channelChips(matchedBy) {
    const wrap = el("span", { class: "channel-chips" });
    const value = String(matchedBy || "");
    if (value.includes("fts") && value.includes("vector")) {
      wrap.append(chip("融合", "both"));
    } else if (value.includes("fts")) {
      wrap.append(chip("全文", "fts"));
    } else if (value.includes("vector")) {
      wrap.append(chip("向量", "vector"));
    }
    return wrap;
  }

  async function loadHealth() {
    try {
      const health = await api("/api/health");
      state.health = health;
      const seekdb = health.seekdb || {};
      const versionMatch = String(seekdb.version || "").match(/seekdb-v?(\d+\.\d+\.\d+)/i);
      setChip("#chip-seekdb", Boolean(seekdb.ok), seekdb.ok ? (versionMatch ? `v${versionMatch[1]}` : "就绪") : "未连接");
      const embedding = health.embedding || {};
      const embOk = embedding.backend === "fastembed";
      setChip("#chip-embedding", embOk, embOk ? "fastembed" : (embedding.backend || "未知"));
      const memory = health.memory || {};
      setChip("#chip-memory", memory.mode === "powercontext" && memory.available, memory.available ? "在线" : "降级");
      $("#tech-seekdb").textContent = seekdb.version || "未连接";
      $("#tech-embedding").textContent = embedding.backend === "fastembed"
        ? "multilingual · 384d" : `${embedding.backend || "未知"} · 384d`;

      const llm = health.llm || {};
      const checkbox = $("#use-llm");
      checkbox.disabled = !llm.available;
      checkbox.checked = Boolean(llm.available);
      $("#llm-note").textContent = llm.available
        ? `模型 ${llm.model}`
        : (llm.disabled ? "已关闭外部模型" : "未配置模型，使用摘录式回答");
    } catch (error) {
      setChip("#chip-seekdb", false, "离线");
      setChip("#chip-embedding", false, "离线");
      setChip("#chip-memory", false, "离线");
      const empty = $("#empty-result");
      if (empty) empty.hidden = false;
      toast(`无法连接 CiteFlow 服务：${error.message}`, true);
    }
  }

  function renderCollectionOptions() {
    const select = $("#collection-select");
    const previous = state.collection;
    select.replaceChildren(el("option", { value: "", text: "全部资料" }));
    for (const item of state.home.collections || []) {
      select.append(el("option", {
        value: item.collection_id,
        text: `${item.collection_id} · ${item.documents}`,
      }));
    }
    const values = Array.from(select.options).map((option) => option.value);
    state.collection = values.includes(previous) ? previous : "";
    select.value = state.collection;
  }

  function filteredDocuments() {
    if (!state.home) return [];
    const docs = state.home.documents || [];
    if (!state.collection) return docs;
    return docs.filter((doc) => doc.collection_id === state.collection);
  }

  function renderDocuments() {
    const list = $("#doc-list");
    const docs = filteredDocuments();
    list.replaceChildren();
    if (!docs.length) {
      list.append(el("p", { class: "memory-empty", text: state.home && state.home.documents.length
        ? "当前范围没有资料" : "资料库还是空的" }));
    }
    for (const doc of docs) {
      const template = $("#doc-template").content.cloneNode(true);
      const item = $(".doc-item", template);
      item.dataset.id = doc.id;
      item.classList.toggle("active", state.activeDocId === doc.id);
      const kind = $(".doc-kind", item);
      kind.textContent = kindLabel(doc.kind);
      kind.classList.add(doc.kind);
      $(".doc-title", item).textContent = doc.title;
      $(".doc-meta", item).textContent = `${doc.page_count} 页 · ${doc.chunk_count} 段 · ${doc.collection_id}`;
      $(".doc-main", item).addEventListener("click", () => toggleDocFilter(doc));
      $(".doc-delete", item).addEventListener("click", (event) => {
        event.stopPropagation();
        removeDocument(doc);
      });
      list.append(template);
    }

    const counts = state.home.counts || {};
    $("#library-meta").textContent = `${num(counts.documents)} 份 · ${num(counts.pages)} 页`;
    const sampleNote = $("#sample-note");
    const hasDocs = (state.home.documents || []).length > 0;
    sampleNote.hidden = hasDocs;
    const firstDemo = $("#btn-first-demo");
    if (firstDemo) firstDemo.hidden = hasDocs;
    $("#btn-demo").hidden = hasDocs && state.home.demo && state.home.demo.seeded;
    renderFilterRow();
  }

  function renderFilterRow() {
    const row = $("#filter-row");
    const filter = $("#doc-filter");
    if (!state.activeDocId) {
      row.hidden = true;
      return;
    }
    const doc = (state.home.documents || []).find((item) => item.id === state.activeDocId);
    if (!doc) {
      state.activeDocId = null;
      row.hidden = true;
      return;
    }
    row.hidden = false;
    filter.textContent = `仅看《${doc.title}》 ×`;
    filter.title = "点击取消文档过滤";
  }

  function toggleDocFilter(doc) {
    state.activeDocId = state.activeDocId === doc.id ? null : doc.id;
    renderDocuments();
    if (state.activeDocId && state.lastAsk) {
      toast(`下一次提问只检索《${doc.title}》`);
    }
  }

  async function removeDocument(doc) {
    if (!window.confirm(`删除《${doc.title}》及其全部索引片段？`)) return;
    try {
      await api(`/api/documents/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
      if (state.activeDocId === doc.id) state.activeDocId = null;
      toast(`已删除《${doc.title}》`);
      await loadHome();
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  }

  function renderProgress() {
    const pill = $("#progress-pill");
    const progress = state.home && state.home.progress;
    if (!progress || !progress.document_id) {
      pill.hidden = true;
      return;
    }
    pill.hidden = false;
    pill.textContent = `续读《${progress.title || "文档"}》 ${pageLabel(progress.kind, progress.page_no)}`;
    pill.onclick = () => openPage({
      document_id: progress.document_id,
      title: progress.title,
      kind: progress.kind,
      page_no: progress.page_no,
      heading: "",
      channels: {},
      matched_by: "",
      label: pageLabel(progress.kind, progress.page_no),
    });
  }

  function renderMemoryMini() {
    const node = $("#memory-mini-text");
    if (!state.memory) {
      node.textContent = "正在读取长期记忆…";
      return;
    }
    const status = state.memory.status || {};
    const count = (state.memory.memories || []).length;
    if (status.mode === "powercontext" && status.available) {
      node.textContent = `PowerContext 在线，已记住 ${count} 条偏好、结论与进度。`;
    } else if (status.mode === "off") {
      node.textContent = `记忆服务已关闭，本地镜像中有 ${count} 条记录。`;
    } else {
      node.textContent = `PowerContext 未连接，当前使用 seekdb 镜像记忆（${count} 条）。`;
    }
  }

  async function loadHome() {
    const home = await api("/api/home");
    state.home = home;
    renderCollectionOptions();
    renderDocuments();
    renderProgress();
    renderMemoryMini();
    const firstDemo = $("#btn-first-demo");
    if (firstDemo && !(home.documents || []).length) firstDemo.hidden = false;
    return home;
  }

  async function loadMemory() {
    try {
      state.memory = await api("/api/memory");
      renderMemory();
      renderMemoryMini();
    } catch (error) {
      toast(`记忆读取失败：${error.message}`, true);
    }
  }

  function renderMemory() {
    if (!state.memory) return;
    const status = state.memory.status || {};
    const online = status.mode === "powercontext" && status.available;
    $("#memory-mode").textContent = online
      ? "PowerContext 在线"
      : (status.mode === "off" ? "记忆服务已关闭" : "seekdb 镜像记忆");
    $("#memory-mode").style.color = online ? "" : "#8b4b2f";
    const latency = $("#memory-latency");
    latency.textContent = status.latency_ms !== undefined ? `${status.latency_ms} ms` : "—";
    latency.classList.toggle("bad", !online);
    const scope = status.scope_id ? ` · scope ${status.scope_id.slice(0, 12)}…` : "";
    $("#memory-detail").textContent = `${status.detail || "状态未知"}${scope}`;

    const list = $("#memory-list");
    list.replaceChildren();
    const memories = state.memory.memories || [];
    $("#memory-count").textContent = `${memories.length} 条`;
    if (!memories.length) {
      list.append(el("p", { class: "memory-empty", text: "还没有长期记忆。写一条偏好，下次回答会自动带上。" }));
    }
    const kindNames = { preference: "偏好", insight: "结论", progress: "进度" };
    for (const item of memories) {
      const node = el("div", { class: "memory-item" },
        el("p", { text: item.text }),
        el("footer", {},
          el("span", { class: `memory-kind ${item.kind}`, text: kindNames[item.kind] || item.kind }),
          el("span", { text: item.backend === "powercontext" ? "PowerContext" : "seekdb" }),
          el("span", { text: formatTime(item.created_at) }),
        ),
      );
      list.append(node);
    }
  }

  async function addMemory(kind, text) {
    const value = (text || "").trim();
    if (!value) {
      toast("先写一条要记住的内容", true);
      return;
    }
    try {
      const result = await api("/api/memory", { method: "POST", body: { kind, text: value } });
      const backend = result.record.backend === "powercontext" ? "PowerContext" : "seekdb 镜像";
      toast(`已写入${backend}`);
      $("#memory-text").value = "";
      await loadMemory();
    } catch (error) {
      toast(`写入失败：${error.message}`, true);
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    const button = $("#btn-ask");
    button.disabled = busy;
    $(".btn-label", button).textContent = busy ? "检索中…" : "检索并回答";
    $(".spinner", button).hidden = !busy;
  }

  function loadingCard(message) {
    return el("div", { class: "loading-state" },
      el("div", { class: "spinner" }),
      el("p", { text: message }),
    );
  }

  async function ask() {
    if (state.busy) return;
    const query = $("#query").value.trim();
    if (!query) {
      toast("先输入一个具体问题", true);
      $("#query").focus();
      return;
    }
    state.currentQuery = query;
    setBusy(true);
    const result = $("#result");
    result.replaceChildren(loadingCard("正在同一条 SQL 中执行全文与向量召回…"));
    try {
      const body = {
        query,
        collection_id: state.collection || null,
        document_ids: state.activeDocId ? [state.activeDocId] : [],
        top_k: 8,
        language: $("#language").value,
        use_llm: $("#use-llm").checked && !$("#use-llm").disabled,
      };
      const payload = await api("/api/ask", { method: "POST", body });
      state.lastAsk = payload;
      state.lastHits = payload.hits || [];
      state.citations = {};
      for (const citation of (payload.answer && payload.answer.citations) || []) {
        state.citations[citation.index] = citation;
      }
      renderAnswer(payload);
      renderTechTrace(payload);
      if (payload.answer && payload.answer.citations && payload.answer.citations.length) {
        openCitation(payload.answer.citations[0].index);
      } else {
        clearEvidence();
      }
      refreshExplain(true);
      if (state.statsLoaded) loadStats(true);
    } catch (error) {
      result.replaceChildren(el("div", { class: "answer-card" },
        el("div", { class: "answer-kicker" }, el("span", { class: "mode-badge none", text: "检索失败" })),
        el("p", { class: "answer-lead", text: error.message }),
        el("p", { class: "muted", text: "请确认 seekdb 已启动（默认 127.0.0.1:2891），然后重试。" }),
      ));
      toast(`检索失败：${error.message}`, true);
    } finally {
      setBusy(false);
    }
  }

  function renderRichText(container, text, citations) {
    const parts = String(text || "").split(/(\[\d+\])/g);
    for (const part of parts) {
      const match = part.match(/^\[(\d+)\]$/);
      if (!match) {
        if (part) container.append(document.createTextNode(part));
        continue;
      }
      const index = Number(match[1]);
      const citation = citations && citations[index];
      if (!citation) {
        container.append(document.createTextNode(part));
        continue;
      }
      container.append(el("button", {
        class: "citation",
        type: "button",
        title: `${citation.title} ${citation.label}`,
        text: part,
        onclick: () => openCitation(index),
      }));
    }
  }

  function renderAnswer(payload) {
    const answer = payload.answer || {};
    const trace = payload.trace || {};
    const result = $("#result");
    result.replaceChildren();

    const modeNames = { extractive: "摘录式回答", llm: "AI 生成", no_evidence: "证据不足" };
    const mode = answer.mode || "extractive";
    const top = el("div", { class: "answer-top" },
      el("div", { class: "answer-kicker" },
        el("span", { class: `mode-badge ${mode === "llm" ? "llm" : mode === "no_evidence" ? "none" : ""}`, text: modeNames[mode] || mode }),
        el("span", { class: "answer-note", text: mode === "llm"
          ? `模型：${answer.model || "已配置模型"}`
          : mode === "extractive" ? "只摘录原文，不调用外部模型" : "没有足够相关的原文" }),
      ),
      el("span", { class: "answer-note", text: `${trace.total_ms || 0} ms · ${payload.hits.length} 条候选` }),
    );

    const card = el("article", { class: "answer-card" }, top);
    if (answer.lead) card.append(el("p", { class: "answer-lead" }, answer.lead));

    if (mode === "llm" && answer.text) {
      const body = el("div", { class: "answer-body" });
      const paragraphs = String(answer.text).split(/\n{2,}/).filter((item) => item.trim());
      for (const paragraph of paragraphs) {
        const node = el("p", { class: "answer-bullet-text" });
        node.style.margin = "0";
        node.style.fontSize = "14px";
        renderRichText(node, paragraph.trim(), ansCitations(answer));
        body.append(node);
      }
      card.append(body);
    } else if (answer.bullets && answer.bullets.length) {
      const body = el("div", { class: "answer-body" });
      for (const bullet of answer.bullets) {
        const line = el("div", { class: "answer-bullet" },
          el("span", { class: "bullet-index", text: String(bullet.citation) }),
          el("p", {}, bullet.text),
        );
        const ref = ansCitations(answer)[bullet.citation];
        if (ref) {
          line.append(el("button", {
            class: "citation", type: "button", text: ref.label,
            title: `${ref.title} ${ref.label}`,
            onclick: () => openCitation(bullet.citation),
          }));
        }
        body.append(line);
      }
      card.append(body);
    }
    if (answer.note) card.append(el("div", { class: "answer-note-line", text: answer.note }));

    const meta = el("div", { class: "answer-meta" },
      el("span", { class: "pill", text: `RRF 融合 ${trace.fused_hits || 0} 条` }),
      el("span", { class: "pill", text: `全文 ${trace.fts_hits || 0} · 向量 ${trace.vector_hits || 0}` }),
      el("span", { class: "pill", text: `嵌入 ${trace.embed_ms || 0} ms · SQL ${trace.query_ms || 0} ms` }),
    );
    const memory = payload.memory || {};
    if (memory.hits && memory.hits.length) {
      meta.append(el("span", {
        class: "pill memory",
        text: `${memory.backend === "powercontext" ? "PowerContext" : "seekdb"} 记忆 ${memory.hits.length} 条`,
      }));
    }
    card.append(meta);

    if (answer.citations && answer.citations.length) {
      const block = el("div", { class: "citations-block" },
        el("p", { class: "eyebrow", text: "Citations" }),
      );
      const grid = el("div", { class: "citation-grid" });
      for (const citation of answer.citations) {
        grid.append(el("button", {
          class: "citation-card", type: "button",
          onclick: () => openCitation(citation.index),
        },
          el("span", { class: "ref", text: `[${citation.index}] ${citation.label}` }),
          el("strong", { text: citation.title }),
          el("span", { text: citation.heading || (citation.kind === "pdf" ? "PDF 原文" : "笔记片段") }),
        ));
      }
      block.append(grid);
      card.append(block);
    }
    result.append(card);

    if (payload.hits && payload.hits.length) {
      const hitsBlock = el("div", { class: "hits-block" },
        el("div", { class: "hits-head" },
          el("h3", { text: "候选证据" }),
          el("span", { class: "meta", text: "两路召回经 RRF 融合后的顺序" }),
        ),
      );
      payload.hits.forEach((hit, index) => {
        hitsBlock.append(el("button", {
          class: "hit-row", type: "button",
          onclick: () => openPage(hit, index + 1),
        },
          el("span", { class: "hit-rank", text: String(index + 1).padStart(2, "0") }),
          el("span", { class: "hit-copy" },
            el("strong", { class: "hit-title", text: hit.doc_title }),
            el("span", { class: "hit-snippet", text: hit.snippet }),
          ),
          el("span", { class: "hit-side" },
            el("span", { class: "hit-page", text: pageLabel(hit.doc_kind, hit.page_no) }),
            channelChips(hit.matched_by),
          ),
        ));
      });
      result.append(hitsBlock);
    }
  }

  function ansCitations(answer) {
    const map = {};
    for (const citation of (answer && answer.citations) || []) map[citation.index] = citation;
    return map;
  }

  function renderTechTrace(payload) {
    const trace = payload.trace || {};
    $("#tech-latency").textContent = `${trace.total_ms || 0} ms`;
    if (trace.sql) $("#tech-sql").textContent = trace.sql;
    if (trace.embedding) {
      $("#tech-embedding").textContent = `${trace.embedding.backend} · ${trace.embedding.dim}d`;
    }
  }

  function clearEvidence() {
    state.activeCitation = null;
    $("#evidence-empty").hidden = false;
    $("#evidence-detail").hidden = true;
  }

  function openCitation(index) {
    let citation = state.citations[index];
    if (!citation) {
      const hit = state.lastHits[index - 1];
      if (!hit) return;
      citation = {
        index,
        document_id: hit.document_id,
        title: hit.doc_title,
        kind: hit.doc_kind,
        page_no: hit.page_no,
        heading: hit.heading,
        label: pageLabel(hit.doc_kind, hit.page_no),
        snippet: hit.snippet,
        matched_by: hit.matched_by,
        channels: hit.channels || {},
        rrf_score: hit.rrf_score,
      };
      state.citations[index] = citation;
    }
    openPage(citation, index);
  }

  async function openPage(citation, index = null) {
    state.activeCitation = citation;
    state.page = { ...citation };
    $("#evidence-empty").hidden = true;
    $("#evidence-detail").hidden = false;
    $("#evidence-title").textContent = citation.title || "未知资料";
    $("#evidence-kind").textContent = kindLabel(citation.kind);
    const label = citation.label || pageLabel(citation.kind, citation.page_no);
    $("#evidence-page").textContent = label;
    $("#evidence-heading").textContent = citation.heading || "";
    renderChannels(citation);

    const imageButton = $('#view-toggle button[data-view="image"]');
    imageButton.disabled = citation.kind !== "pdf";
    if (citation.kind !== "pdf" && state.view === "image") state.view = "text";
    syncView();

    $("#page-prev").disabled = citation.page_no <= 1;
    const image = $("#evidence-image");
    image.hidden = state.view !== "image" || citation.kind !== "pdf";
    $("#evidence-text").hidden = state.view === "image" && citation.kind === "pdf";

    try {
      const page = await api(`/api/documents/${encodeURIComponent(citation.document_id)}/page/${citation.page_no}`);
      if (state.activeCitation !== citation) return;
      $("#evidence-text").textContent = page.text || "本页没有可提取的文本。";
      $("#page-next").disabled = citation.page_no >= page.page_count;
      if (page.progress) {
        state.home = state.home || {};
        state.home.progress = page.progress;
        renderProgress();
      }
      if (state.view === "image" && citation.kind === "pdf") {
        image.src = `/api/documents/${encodeURIComponent(citation.document_id)}/page/${citation.page_no}/image`;
      }
      if (state.tab !== "tech") switchTab("evidence", false);
    } catch (error) {
      $("#evidence-text").textContent = `无法读取原文：${error.message}`;
      toast(`读取原文失败：${error.message}`, true);
    }
  }

  function renderChannels(citation) {
    const box = $("#evidence-channels");
    box.replaceChildren();
    const channels = citation.channels || {};
    const names = { fts: "全文检索", vector: "向量检索" };
    for (const [key, meta] of Object.entries(channels)) {
      const row = el("div", {},
        el("div", { class: "channel-row" },
          el("span", { class: "name" }, channelChips(key === "fts" ? "fts" : "vector"), names[key] || key),
          el("span", { class: "score", text: `rank ${meta.rank} · ${Number(meta.score).toFixed(3)}` }),
        ),
        el("div", { class: `channel-bar ${key}` },
          el("span", { style: `width:${Math.max(6, 100 - Math.min(100, meta.rank * 4))}%` }),
        ),
      );
      box.append(row);
    }
    if (!Object.keys(channels).length) {
      box.append(el("p", { class: "muted small", text: "该页由阅读进度打开，没有检索通道信息。" }));
    }
    if (citation.rrf_score) {
      box.append(el("p", { class: "muted small", text: `RRF 分数 ${Number(citation.rrf_score).toFixed(6)} · ${citation.matched_by || ""}` }));
    }
  }

  function syncView() {
    $$("#view-toggle button").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === state.view);
    });
    const useImage = state.view === "image" && state.page && state.page.kind === "pdf";
    $("#evidence-text").hidden = useImage;
    const image = $("#evidence-image");
    image.hidden = !useImage;
    if (useImage && state.page) {
      image.src = `/api/documents/${encodeURIComponent(state.page.document_id)}/page/${state.page.page_no}/image`;
    }
  }

  async function changePage(delta) {
    if (!state.page) return;
    const next = Number(state.page.page_no) + delta;
    if (next < 1) return;
    if (state.home) {
      const doc = (state.home.documents || []).find((item) => item.id === state.page.document_id);
      if (doc && next > doc.page_count) return;
    }
    const citation = {
      ...state.page,
      page_no: next,
      label: pageLabel(state.page.kind, next),
      heading: "",
      channel_note: "翻页查看同一文档",
    };
    await openPage(citation, state.activeCitation && state.activeCitation.index);
  }

  async function saveInsight() {
    if (!state.page) return;
    const text = `《${state.page.title}》${state.page.label || pageLabel(state.page.kind, state.page.page_no)}：${(state.page.snippet || "").slice(0, 180)}`;
    await addMemory("insight", text);
  }

  async function copyCitation() {
    if (!state.page) return;
    const text = `《${state.page.title}》${state.page.label || pageLabel(state.page.kind, state.page.page_no)}`;
    try {
      await navigator.clipboard.writeText(text);
      toast(`已复制：${text}`);
    } catch (_) {
      toast(text);
    }
  }

  function switchTab(tab, load = true) {
    state.tab = tab;
    $$(".tab").forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    for (const name of ["evidence", "memory", "tech"]) {
      $(`#tab-${name}`).hidden = name !== tab;
    }
    if (tab === "memory" && load) loadMemory();
    if (tab === "tech" && load) loadStats();
  }

  async function refreshExplain(silent = false) {
    try {
      const payload = await api("/api/retrieval/explain");
      $("#tech-plan").textContent = payload.plan || "计划为空。";
      if (payload.trace && payload.trace.sql) $("#tech-sql").textContent = payload.trace.sql;
      if (!silent) toast("执行计划已刷新");
    } catch (error) {
      if (!silent) toast(`EXPLAIN 失败：${error.message}`, true);
    }
  }

  async function loadStats(silent = false) {
    try {
      const payload = await api("/api/stats");
      state.statsLoaded = true;
      const totals = payload.totals || {};
      const latency = payload.latency_ms || {};
      const grid = $("#stats-grid");
      grid.replaceChildren(
        stat("文档", num(totals.documents)),
        stat("片段", num(totals.chunks)),
        stat("查询", num(totals.queries)),
        stat("P50", `${Math.round(latency.p50 || 0)} ms`),
        stat("P95", `${Math.round(latency.p95 || 0)} ms`),
        stat("平均", `${latency.avg || 0} ms`),
      );
      const recent = $("#recent-queries");
      recent.replaceChildren();
      for (const row of payload.recent_queries || []) {
        recent.append(el("div", { class: "recent-row" },
          el("span", { class: "q", text: row.query, title: row.query }),
          el("span", { class: "lat", text: `${row.latency_ms} ms` }),
        ));
      }
      if (!(payload.recent_queries || []).length) {
        recent.append(el("p", { class: "memory-empty", text: "还没有查询记录。" }));
      }
    } catch (error) {
      if (!silent) toast(`统计读取失败：${error.message}`, true);
    }
  }

  function stat(label, value) {
    return el("div", { class: "stat-card" },
      el("span", { text: label }),
      el("strong", { text: value }),
    );
  }

  async function seedDemo() {
    if (state.demoBusy) return;
    state.demoBusy = true;
    const buttons = ["#btn-demo", "#btn-demo-inline", "#btn-first-demo"]
      .map((selector) => $(selector))
      .filter(Boolean);
    for (const button of buttons) {
      button.disabled = true;
      button.textContent = "导入中…";
    }
    toast("正在解析年报并写入向量索引，首次运行需要一点时间…");
    try {
      const result = await api("/api/demo/seed", { method: "POST" });
      const ok = (result.files || []).filter((file) => !file.error);
      const failed = (result.files || []).filter((file) => file.error);
      toast(`示例资料已就绪：${ok.length} 份导入，用时 ${Math.round((result.elapsed_ms || 0) / 1000)} 秒`);
      if (failed.length) toast(`${failed.length} 份失败：${failed[0].error}`, true);
      state.collection = "";
      await loadHome();
      await loadMemory();
      if (!(state.lastAsk)) {
        $("#query").value = "3M 在 2022 年面临的主要供应链和原材料风险是什么？";
        $("#query").focus();
      }
    } catch (error) {
      toast(`导入失败：${error.message}`, true);
    } finally {
      state.demoBusy = false;
      for (const button of buttons) {
        button.disabled = false;
        button.textContent = button.id === "btn-first-demo" ? "先导入示例资料" : "导入示例";
      }
    }
  }

  async function uploadFile(file) {
    if (!file) return;
    const suffix = file.name.toLowerCase().slice(file.name.lastIndexOf("."));
    if (![".pdf", ".md", ".markdown", ".txt"].includes(suffix)) {
      toast("仅支持 PDF、Markdown 或 TXT", true);
      return;
    }
    if (file.size > 30 * 1024 * 1024) {
      toast("文件超过 30 MB 限制", true);
      return;
    }
    const form = new FormData();
    form.append("file", file);
    const collection = state.collection || (state.home && state.home.collections[0] && state.home.collections[0].collection_id) || "我的书架";
    form.append("collection_id", collection);
    toast(`正在解析《${file.name}》…`);
    try {
      const result = await api("/api/upload", { method: "POST", body: form });
      if (result.duplicate) {
        toast(`《${result.document.title}》已存在，未重复导入`);
      } else {
        toast(`已导入《${result.document.title}》：${result.document.chunk_count} 段，${Math.round((result.elapsed_ms || 0) / 1000)} 秒`);
      }
      state.collection = result.document.collection_id;
      await loadHome();
    } catch (error) {
      toast(`导入失败：${error.message}`, true);
    } finally {
      $("#file-input").value = "";
    }
  }

  function bindEvents() {
    $("#btn-ask").addEventListener("click", ask);
    $("#query").addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        ask();
      }
    });
    $$("#suggestions button").forEach((button) => {
      button.addEventListener("click", () => {
        $("#query").value = button.dataset.q || button.textContent;
        ask();
      });
    });
    $("#collection-select").addEventListener("change", (event) => {
      state.collection = event.target.value;
      state.activeDocId = null;
      renderDocuments();
    });
    $("#doc-filter").addEventListener("click", () => {
      state.activeDocId = null;
      renderDocuments();
    });
    $$("[data-tab]").forEach((button) => {
      if (button.classList.contains("tab") || button.classList.contains("status-chip") || button.classList.contains("link-btn")) {
        button.addEventListener("click", () => switchTab(button.dataset.tab));
      }
    });
    $("#btn-upload").addEventListener("click", () => $("#file-input").click());
    $("#file-input").addEventListener("change", (event) => uploadFile(event.target.files[0]));
    const dropzone = $("#dropzone");
    dropzone.addEventListener("click", () => $("#file-input").click());
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        $("#file-input").click();
      }
    });
    for (const type of ["dragenter", "dragover"]) {
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    }
    for (const type of ["dragleave", "drop"]) {
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    }
    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer && event.dataTransfer.files[0];
      uploadFile(file);
    });
    $("#btn-demo").addEventListener("click", seedDemo);
    $("#btn-demo-inline").addEventListener("click", seedDemo);
    $("#btn-first-demo").addEventListener("click", seedDemo);
    $("#view-toggle").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-view]");
      if (!button || button.disabled) return;
      state.view = button.dataset.view;
      syncView();
    });
    $("#page-prev").addEventListener("click", () => changePage(-1));
    $("#page-next").addEventListener("click", () => changePage(1));
    $("#btn-save-insight").addEventListener("click", saveInsight);
    $("#btn-copy-citation").addEventListener("click", copyCitation);
    $("#btn-copy-sql").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("#tech-sql").textContent);
        toast("SQL 已复制");
      } catch (_) {
        toast("复制失败，请手动选择", true);
      }
    });
    $("#btn-explain").addEventListener("click", () => refreshExplain(false));
    $("#btn-stats").addEventListener("click", () => loadStats(false));
    $("#memory-form").addEventListener("submit", (event) => {
      event.preventDefault();
      addMemory($("#memory-kind").value, $("#memory-text").value);
    });
    $$(".quick-memories button").forEach((button) => {
      button.addEventListener("click", () => addMemory("preference", button.dataset.memory));
    });
    $("#btn-context").addEventListener("click", async () => {
      const query = state.currentQuery || "阅读偏好";
      const node = $("#context-preview");
      node.textContent = "正在组装上下文…";
      try {
        const payload = await api(`/api/memory/context?q=${encodeURIComponent(query)}`);
        node.textContent = payload.available
          ? (payload.content || "（空）")
          : (payload.detail || "PowerContext 不可用");
      } catch (error) {
        node.textContent = `上下文生成失败：${error.message}`;
      }
    });
  }

  async function init() {
    bindEvents();
    switchTab("evidence", false);
    await Promise.allSettled([loadHealth(), loadHome(), loadMemory()]);
    if (state.health && !state.health.seekdb.ok) {
      toast("seekdb 未连接：请先启动本地实例（127.0.0.1:2891）", true);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
