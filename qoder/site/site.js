(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const header = $("#site-header");
  const navToggle = $("#nav-toggle");
  const nav = $("#site-nav");

  const setNavOpen = (open) => {
    header.classList.toggle("nav-open", open);
    navToggle.setAttribute("aria-expanded", String(open));
  };

  navToggle.addEventListener("click", () => {
    setNavOpen(!header.classList.contains("nav-open"));
  });

  $$("a", nav).forEach((link) => {
    link.addEventListener("click", () => setNavOpen(false));
  });

  document.addEventListener("click", (event) => {
    if (!header.classList.contains("nav-open")) return;
    if (!header.contains(event.target)) setNavOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && header.classList.contains("nav-open")) {
      setNavOpen(false);
      navToggle.focus();
    }
  });

  const navLinks = $$("a", nav);
  const navTargets = navLinks
    .map((link) => ({ link, section: $(link.getAttribute("href")) }))
    .filter((item) => item.section);

  if ("IntersectionObserver" in window) {
    const navObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          navLinks.forEach((link) => link.classList.remove("is-current"));
          const current = navTargets.find((item) => item.section === entry.target);
          if (current) current.link.classList.add("is-current");
        });
      },
      { rootMargin: "-38% 0px -55% 0px", threshold: 0 }
    );
    navTargets.forEach((item) => navObserver.observe(item.section));

    const revealItems = $$(".reveal");
    const revealCheck = () => {
      revealItems.forEach((item) => {
        if (item.classList.contains("is-visible")) return;
        if (item.getBoundingClientRect().top < window.innerHeight * 0.94) {
          item.classList.add("is-visible");
        }
      });
    };
    document.addEventListener("scroll", () => requestAnimationFrame(revealCheck), { passive: true });
    window.addEventListener("resize", revealCheck, { passive: true });
    revealCheck();
  } else {
    $$(".reveal").forEach((item) => item.classList.add("is-visible"));
  }

  const tabs = $$(".gallery-tab");
  const panels = $$(".gallery-item");

  const activateTab = (tab, focus = false) => {
    if (!tab) return;
    tabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
    });
    panels.forEach((panel) => {
      const selected = panel.id === tab.getAttribute("aria-controls");
      panel.hidden = !selected;
      panel.classList.toggle("is-active", selected);
    });
    if (focus) tab.focus();
  };

  tabs.forEach((tab, index) => {
    tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1;
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      activateTab(next, true);
    });
  });

  const dialog = $("#lightbox");
  const dialogImage = $("#lightbox-image");
  const dialogCaption = $("#lightbox-caption");
  const dialogClose = $("#lightbox-close");

  const closeLightbox = () => {
    if (dialog.open) dialog.close();
  };

  if (dialog && typeof dialog.showModal === "function") {
    $$("[data-lightbox]").forEach((button) => {
      button.addEventListener("click", () => {
        const caption = button.getAttribute("data-caption") || "CiteFlow";
        dialogImage.src = button.getAttribute("data-lightbox");
        dialogImage.alt = caption;
        dialogCaption.textContent = caption;
        dialog.showModal();
      });
    });
    dialogClose.addEventListener("click", closeLightbox);
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeLightbox();
    });
    dialog.addEventListener("close", () => {
      dialogImage.removeAttribute("src");
    });
  }

  const copyText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("copy failed");
  };

  $$("[data-copy-target]").forEach((button) => {
    const original = button.textContent;
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.getAttribute("data-copy-target"));
      if (!target) return;
      try {
        await copyText(target.textContent.trim());
        button.textContent = "已复制 ✓";
      } catch (error) {
        button.textContent = "复制失败";
      }
      window.setTimeout(() => {
        button.textContent = original;
      }, 1700);
    });
  });

  const usageUrl = "data/usage.json";
  const formatNumber = (value) => new Intl.NumberFormat("en-US").format(Number(value) || 0);

  const usageNodes = {};
  $$("[data-usage]").forEach((node) => {
    usageNodes[node.dataset.usage] = node;
  });

  fetch(usageUrl, { cache: "no-store" })
    .then((response) => {
      if (!response.ok) throw new Error("usage snapshot unavailable");
      return response.json();
    })
    .then((data) => {
      const total = Number(data.queries?.total) || 0;
      const set = (key, value) => {
        if (usageNodes[key] && value !== undefined && value !== null && value !== "") {
          usageNodes[key].textContent = value;
        }
      };

      set("total", formatNumber(total));
      set("p50", formatNumber(data.queries?.p50_ms));
      set("p95", formatNumber(data.queries?.p95_ms));
      set("documents", formatNumber(data.dataset?.documents));
      set("chunks", formatNumber(data.dataset?.chunks));

      const modes = data.queries?.modes || {};
      set("mode-extractive", formatNumber(modes.extractive));
      set("mode-search", formatNumber(modes.search));

      $$("[data-mode-bar]").forEach((bar) => {
        const value = Number(modes[bar.dataset.modeBar]) || 0;
        const percent = total ? Math.max(3, (value / total) * 100) : 0;
        bar.style.width = `${percent.toFixed(1)}%`;
      });

      if (usageNodes["generated"]) {
        usageNodes["generated"].textContent =
          `快照时间：${data.generated_at_label || "—"} · 数据源：${data.source || "/api/stats"}`;
      }
      if (usageNodes["source-label"]) {
        usageNodes["source-label"].textContent =
          `来源：${data.source || "GET /api/stats"} · ${data.generated_at_label || ""} · 开发期本机数据`;
      }

      const list = $("[data-usage-list='recent']");
      const queries = Array.isArray(data.recent_queries) ? data.recent_queries.slice(0, 4) : [];
      if (list && queries.length) {
        list.replaceChildren();
        queries.forEach((item) => {
          const row = document.createElement("li");
          const label = document.createElement("span");
          const latency = document.createElement("b");
          label.textContent = item.query || "—";
          label.title = item.query || "";
          latency.textContent = `${Math.round(Number(item.latency_ms) || 0)} ms`;
          row.append(label, latency);
          list.appendChild(row);
        });
      }
    })
    .catch(() => {
      const label = usageNodes["generated"];
      if (label) label.textContent = "快照时间：2026-10-27 12:18 UTC+8 · 静态回退数据，来源与仓库内导出文件一致";
    });
})();
