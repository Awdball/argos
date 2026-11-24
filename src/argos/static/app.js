(() => {
  "use strict";

  const state = {
    data: null,
    selectedItemId: null,
  };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = String(v);
      else if (k === "dataset") {
        for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = String(dv);
      } else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else {
        node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child === null || child === undefined) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  function safeHref(url) {
    try {
      const parsed = new URL(String(url || ""), window.location.origin);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.href;
    } catch {}
    return "#";
  }

  function fmtUsd(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
    const num = Number(val);
    return num % 1 === 0 ? `$${num.toLocaleString("en-US")}` : `$${num.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  function fmtInterval(minutes) {
    if (!minutes) return "n/a";
    if (minutes < 60) return `${minutes}m`;
    const hrs = Math.round((minutes / 60) * 10) / 10;
    return `${hrs}h`;
  }

  function fmtRelative(isoStr) {
    if (!isoStr) return "never";
    const dt = new Date(isoStr.endsWith("Z") ? isoStr : `${isoStr}Z`);
    const diffSec = Math.round((Date.now() - dt.getTime()) / 1000);
    if (Number.isNaN(diffSec)) return "unknown";
    const abs = Math.abs(diffSec);
    if (abs < 60) return diffSec >= 0 ? "just now" : "in <1m";
    const mins = Math.round(abs / 60);
    if (mins < 60) return diffSec >= 0 ? `${mins}m ago` : `in ${mins}m`;
    const hrs = Math.round(mins / 60);
    if (hrs < 48) return diffSec >= 0 ? `${hrs}h ago` : `in ${hrs}h`;
    const days = Math.round(hrs / 24);
    return diffSec >= 0 ? `${days}d ago` : `in ${days}d`;
  }

  function toast(message) {
    const region = document.getElementById("toast-region");
    const node = el("div", { class: "toast", text: message });
    region.appendChild(node);
    setTimeout(() => node.remove(), 4500);
  }

  async function apiJson(url, options = {}) {
    const resp = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const body = await resp.json();
        if (body.detail) detail = body.detail;
      } catch {}
      throw new Error(detail);
    }
    return resp.json();
  }

  /* ---- awdash Theme Selector ---- */
  function setupThemeToggle() {
    const meta = document.querySelector('meta[name="color-scheme"]');
    const sel = document.getElementById("theme-select");
    const applyTheme = (theme) => {
      const valid = ["cyber", "gruvbox", "solarized", "e-ink", "e-ink-dark"];
      const next = valid.includes(theme) ? theme : "cyber";
      document.documentElement.setAttribute("data-theme", next);
      if (meta) meta.content = next === "e-ink" ? "light" : "dark";
      if (sel) sel.value = next;
    };

    try {
      applyTheme(localStorage.getItem("argos-theme") || "cyber");
    } catch {
      applyTheme("cyber");
    }

    if (sel) {
      sel.addEventListener("change", () => {
        try {
          localStorage.setItem("argos-theme", sel.value);
        } catch {}
        applyTheme(sel.value);
      });
    }

    window.addEventListener("storage", (e) => {
      if (e.key === "argos-theme") applyTheme(e.newValue || "cyber");
    });
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined) return "—";
    const s = Math.max(0, Math.round(Number(sec)));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    if (m < 60) return `${m}m ${rem}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  function fmtOperationLabel(op) {
    const map = {
      idle: "Idle",
      searching: "Searching",
      researching_prices: "Researching Prices",
      researching_retailers: "Researching Retailers",
      building_scraper: "Building Scraper",
      escalating: "Escalating",
    };
    const key = String(op || "idle").toLowerCase();
    return map[key] || key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function renderHermesStatus(data) {
    const btn = document.getElementById("hermes-status-btn");
    const label = document.getElementById("hermes-status-label");
    if (!btn || !label) return;
    const cur = data?.hermes?.current || { operation: "idle", status: "idle", duration_seconds: 0 };
    const isRunning = cur.status === "running";
    btn.classList.toggle("running", isRunning);
    const opLabel = fmtOperationLabel(cur.operation);
    label.textContent = isRunning
      ? `Hermes: ${opLabel} · ${fmtDuration(cur.duration_seconds)}`
      : `Hermes: ${opLabel}`;
  }

  async function openHermesModal() {
    const dialog = document.getElementById("hermes-dialog");
    const container = document.getElementById("hermes-content");
    if (!dialog || !container) return;
    let payload = state.data?.hermes;
    try {
      payload = await apiJson("/api/hermes/status");
      if (state.data) state.data.hermes = payload;
      renderHermesStatus({ hermes: payload });
    } catch {}

    const cur = payload?.current || { operation: "idle", status: "idle", detail: "Idle", duration_seconds: 0 };
    const history = payload?.history || [];

    const summaryBox = el("div", { class: "mini-box", style: "margin-bottom:12px;" }, [
      el("div", { class: "mini-box-label", text: "Current Hermes State" }),
      el("div", {
        class: `mini-box-val ${cur.status === "running" ? "accent" : ""}`,
        text: `${fmtOperationLabel(cur.operation)}${cur.status === "running" ? ` (${fmtDuration(cur.duration_seconds)})` : ""}`,
      }),
      el("div", { class: "muted", text: cur.detail || "No active task" }),
    ]);

    const rows = history.length
      ? history.map((h) =>
          el("tr", {}, [
            el("td", {}, [el("span", { class: `pill ${h.status === "running" ? "pill-accent" : h.status === "failed" ? "pill-hot" : "pill-good"}`, text: fmtOperationLabel(h.operation) })]),
            el("td", { text: h.detail || h.target_domain || "—" }),
            el("td", { text: h.status }),
            el("td", { text: fmtDuration(h.duration_seconds) }),
            el("td", { text: h.started_at ? new Date(h.started_at + "Z").toLocaleString() : "—" }),
          ])
        )
      : [
          el("tr", {}, [
            el("td", { colspan: "5", class: "muted", text: "No Hermes operations logged yet. Scheduled checks run daily at 09:00 and every 3 days at 10:00." }),
          ]),
        ];

    const table = el("div", { class: "table-wrap" }, [
      el("table", { class: "data-table" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { text: "Operation" }),
            el("th", { text: "Task / Target" }),
            el("th", { text: "Status" }),
            el("th", { text: "Duration" }),
            el("th", { text: "Started" }),
          ]),
        ]),
        el("tbody", {}, rows),
      ]),
    ]);

    container.replaceChildren(summaryBox, table);
    dialog.showModal();
  }

  /* ---- Accessible Tabs ---- */
  function setupTabs() {
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    const panels = Array.from(document.querySelectorAll('[role="tabpanel"]'));

    function activateTab(tab, moveFocus = false) {
      for (const t of tabs) {
        const selected = t === tab;
        t.setAttribute("aria-selected", selected ? "true" : "false");
        t.tabIndex = selected ? 0 : -1;
      }
      for (const p of panels) {
        const show = p.id === tab.dataset.panel;
        p.hidden = !show;
        if (show) p.tabIndex = 0;
        else p.removeAttribute("tabindex");
      }
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (moveFocus) tab.focus();
    }

    tabs.forEach((tab, idx) => {
      tab.addEventListener("click", () => activateTab(tab));
      tab.addEventListener("keydown", (e) => {
        let nextIdx = null;
        if (e.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
        else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") nextIdx = 0;
        else if (e.key === "End") nextIdx = tabs.length - 1;
        if (nextIdx !== null) {
          e.preventDefault();
          activateTab(tabs[nextIdx], true);
        }
      });
    });
  }

  /* ---- Native Modal Dialogs + Safari closedby="any" Fallback ---- */
  function setupDialogs() {
    const dialogs = Array.from(document.querySelectorAll("dialog"));
    for (const dialog of dialogs) {
      dialog.addEventListener("close", () => {
        dialog.returnValue = "";
      });
      dialog.querySelectorAll("[data-close-dialog]").forEach((btn) => {
        btn.addEventListener("click", () => dialog.close("cancel"));
      });
    }

    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e, dialog) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dialog.addEventListener("pointerdown", (e) => {
          pressedOutside = outside(e, dialog);
        });
        dialog.addEventListener("pointerup", (e) => {
          if (pressedOutside && outside(e, dialog)) dialog.close();
        });
      }
    }
  }

  function buildPriceLadder(item) {
    const baseline = Number(item.retail_baseline_usd || item.new_deal_target_usd || item.used_deal_target_usd || 100);
    const maxScale = Math.max(
      baseline * 1.15,
      Number(item.best_retail?.price_usd || 0) * 1.05,
      Number(item.best_secondhand?.landed_cost_usd || 0) * 1.05,
      100
    );
    const pct = (v) => `${Math.min(98, Math.max(2, (Number(v) / maxScale) * 100)).toFixed(1)}%`;

    const track = el("div", { class: "price-track" });
    if (item.used_deal_target_usd) {
      track.appendChild(
        el("span", {
          class: "price-marker marker-used",
          style: `left:${pct(item.used_deal_target_usd)}`,
          title: `Used Target ${fmtUsd(item.used_deal_target_usd)}`,
        })
      );
    }
    if (item.new_deal_target_usd) {
      track.appendChild(
        el("span", {
          class: "price-marker marker-new",
          style: `left:${pct(item.new_deal_target_usd)}`,
          title: `New Target ${fmtUsd(item.new_deal_target_usd)}`,
        })
      );
    }
    if (item.best_retail?.price_usd) {
      track.appendChild(
        el("span", {
          class: "price-marker marker-retail",
          style: `left:${pct(item.best_retail.price_usd)}`,
          title: `Best Retail ${fmtUsd(item.best_retail.price_usd)}`,
        })
      );
    }
    if (item.best_secondhand?.landed_cost_usd) {
      track.appendChild(
        el("span", {
          class: "price-marker marker-secondhand",
          style: `left:${pct(item.best_secondhand.landed_cost_usd)}`,
          title: `Best 2nd-Hand ${fmtUsd(item.best_secondhand.landed_cost_usd)}`,
        })
      );
    }

    return el("div", { class: "price-ladder" }, [
      el("div", { class: "price-row-top" }, [
        el("span", { text: `Used ≤ ${fmtUsd(item.used_deal_target_usd)}` }),
        el("span", { text: `New ≤ ${fmtUsd(item.new_deal_target_usd)}` }),
        el("span", { text: `MSRP ${fmtUsd(item.retail_baseline_usd)}` }),
      ]),
      track,
      el("div", { class: "price-legend" }, [
        el("span", {
          text: item.best_secondhand
            ? `2nd-Hand Pin: ${fmtUsd(item.best_secondhand.landed_cost_usd)}`
            : "2nd-Hand: none yet",
        }),
        el("span", {
          text: item.best_retail
            ? `Retail Pin: ${fmtUsd(item.best_retail.price_usd)}`
            : "Retail: awaiting scan",
        }),
      ]),
    ]);
  }

  function renderSummaryAndFilters(data) {
    const s = data.summary;
    document.getElementById("kpi-active").textContent = s.watches_active;
    document.getElementById("kpi-hot").textContent = `${s.watches_hot} hot · ${s.watches_total} total`;
    document.getElementById("kpi-secondhand").textContent = s.viable_secondhand_count;
    document.getElementById("kpi-below-target").textContent = s.below_target_retail_count;
    document.getElementById("kpi-best-discount").textContent =
      s.best_discount_pct !== null ? `Best discount ${s.best_discount_pct}%` : "Scanning retailers";
    document.getElementById("kpi-providers").textContent = `${s.providers_configured} / ${s.providers_total}`;
    const budgetEl = document.getElementById("kpi-budget");
    if (budgetEl) budgetEl.textContent = "Zero-cost browser & API providers";
    document.getElementById("kpi-gaps").textContent = s.open_gaps_count;
    document.getElementById("tab-deals-count").textContent = s.viable_secondhand_count;
    document.getElementById("tab-gaps-count").textContent = s.open_gaps_count;

    const catSelect = document.getElementById("filter-category");
    const prevCat = catSelect.value;
    const cats = Array.from(new Set(data.items.map((i) => i.category).filter(Boolean))).sort();
    catSelect.replaceChildren(el("option", { value: "all", text: "All Categories" }));
    for (const c of cats) {
      catSelect.appendChild(el("option", { value: c, text: c }));
    }
    if (cats.includes(prevCat)) catSelect.value = prevCat;
  }

  function filteredItems(data) {
    const q = document.getElementById("filter-search").value.trim().toLowerCase();
    const st = document.getElementById("filter-status").value;
    const cat = document.getElementById("filter-category").value;
    const hotOnly = document.getElementById("filter-hot").checked;
    const belowTargetOnly = document.getElementById("filter-below-target")?.checked || false;
    const sortBy = document.getElementById("sort-items").value;

    const rows = data.items.filter((it) => {
      if (it.status === "archived") return false;
      if (st !== "all" && it.status !== st) return false;
      if (cat !== "all" && it.category !== cat) return false;
      if (hotOnly && !it.is_hot) return false;
      if (belowTargetOnly && !it.best_retail?.meets_target) return false;
      if (q) {
        const hay = [
          it.name,
          it.raw_request,
          ...(it.search_queries || []),
          ...(it.trusted_retailers || []).map((r) => `${r.name} ${r.domain}`),
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    rows.sort((a, b) => {
      if (sortBy === "value") {
        return Number(b.retail_baseline_usd || 0) - Number(a.retail_baseline_usd || 0);
      }
      if (sortBy === "newest") return b.id - a.id;
      const aScore = Math.max(
        Number(a.best_secondhand?.deal_score || 0),
        Number(a.best_retail?.pct_vs_baseline || 0)
      );
      const bScore = Math.max(
        Number(b.best_secondhand?.deal_score || 0),
        Number(b.best_retail?.pct_vs_baseline || 0)
      );
      return bScore - aScore || a.id - b.id;
    });
    return rows;
  }

  function renderWatchGrid(data) {
    const grid = document.getElementById("watch-grid");
    const items = filteredItems(data);
    if (!items.length) {
      grid.replaceChildren(el("div", { class: "empty-state", text: "No watched items match the current filters." }));
      return;
    }

    const cards = items.map((item, idx) => {
      const imgAttrs = {
        src: item.image_endpoint,
        alt: item.name,
        width: 480,
        height: 300,
      };
      if (idx < 2) imgAttrs.fetchpriority = "high";
      else imgAttrs.loading = "lazy";

      const hotBtn = el("button", {
        type: "button",
        class: `pill ${item.is_hot ? "pill-hot" : ""}`,
        text: item.is_hot ? "🔥 HOT" : "Standard",
        title: "Toggle hot polling cadence",
        onClick: async () => {
          try {
            await apiJson(`/api/items/${item.id}`, {
              method: "PATCH",
              body: JSON.stringify({ is_hot: !item.is_hot }),
            });
            toast(`Watch #${item.id} set to ${!item.is_hot ? "HOT" : "Standard"} cadence`);
            await refreshDashboard();
          } catch (err) {
            toast(`Failed: ${err.message}`);
          }
        },
      });

      const newDelta = item.target_gap?.new_delta_usd;
      const deltaText =
        newDelta === null || newDelta === undefined
          ? null
          : newDelta <= 0
          ? `${fmtUsd(Math.abs(newDelta))} under target`
          : `+${fmtUsd(newDelta)} above target`;

      const retailBox = item.best_retail
        ? el("div", { class: "mini-box" }, [
            el("div", { class: "mini-box-label", text: item.best_retail.is_live ? "Best Live Retail" : "Retail Ref" }),
            el("div", {
              class: `mini-box-val ${item.best_retail.meets_target ? "good" : ""}`,
              text: fmtUsd(item.best_retail.price_usd),
            }),
            el("a", {
              href: safeHref(item.best_retail.url),
              target: "_blank",
              rel: "noopener",
              text: item.best_retail.name,
            }),
            deltaText
              ? el("div", {
                  class: newDelta <= 0 ? "good" : "muted",
                  style: "font-size:11px;margin-top:2px;",
                  text: deltaText,
                })
              : null,
          ])
        : el("div", { class: "mini-box" }, [
            el("div", { class: "mini-box-label", text: "Best Retail" }),
            el("div", { class: "mini-box-val muted", text: "—" }),
            el("div", { class: "muted", text: `${item.retailer_matrix.length} tracked sites` }),
          ]);

      const shCount = item.viable_secondhand_deals.length;
      const shBox = item.best_secondhand
        ? el("div", { class: "mini-box" }, [
            el("div", { class: "mini-box-label", text: `2nd-Hand (${shCount} viable)` }),
            el("div", {
              class: `mini-box-val ${item.best_secondhand.meets_target ? "good" : "warn"}`,
              text: fmtUsd(item.best_secondhand.landed_cost_usd),
            }),
            el("div", {
              class: "muted",
              text: `${item.best_secondhand.provider}${item.best_secondhand.distance_miles !== null ? ` · ${item.best_secondhand.distance_miles} mi` : ""}`,
            }),
          ])
        : el("div", { class: "mini-box" }, [
            el("div", { class: "mini-box-label", text: "2nd-Hand Deals" }),
            el("div", { class: "mini-box-val muted", text: "0 viable" }),
            el("div", { class: "muted", text: `Within ${item.radius_miles} mi` }),
          ]);

      const cadenceText = [
        `2nd-hand: ${fmtInterval(item.secondhand_interval_minutes)}`,
        `Retail: ${fmtInterval(item.retail_interval_minutes)}`,
        `Radius: ${item.radius_miles} mi`,
        `Checked: ${fmtRelative(item.last_checked_at)}`,
      ].join(" · ");

      const scanBtn = el("button", {
        type: "button",
        class: "btn btn-secondary btn-sm",
        text: "Scan Now",
        onClick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          btn.textContent = "Scanning...";
          try {
            const res = await apiJson(`/api/items/${item.id}/check`, { method: "POST" });
            toast(`Watch #${item.id}: ${res.items_found} found, ${res.new_alerts} new alert(s)`);
            await refreshDashboard();
          } catch (err) {
            toast(`Scan failed: ${err.message}`);
          } finally {
            btn.disabled = false;
            btn.textContent = "Scan Now";
          }
        },
      });

      return el("article", { class: "watch-card" }, [
        el("div", { class: "card-media" }, [
          el("img", imgAttrs),
          el("div", { class: "media-badges" }, [
            el("span", { class: "pill pill-accent", text: item.category }),
            hotBtn,
          ]),
        ]),
        el("div", { class: "card-body" }, [
          el("div", {}, [
            el("h3", { class: "card-title", text: `#${item.id} ${item.name}` }),
            el("div", {
              class: "card-meta",
              text: `Status: ${item.status} · Exclusions: ${(item.negative_keywords || []).slice(0, 4).join(", ") || "none"}`,
            }),
          ]),
          buildPriceLadder(item),
          el("div", { class: "deal-split" }, [retailBox, shBox]),
          el("div", { class: "cadence-strip", text: cadenceText }),
          el("div", { class: "card-actions" }, [
            el("button", {
              type: "button",
              class: "btn btn-primary btn-sm",
              text: "Inspect & Retailers",
              onClick: () => openDetailModal(item.id),
            }),
            scanBtn,
          ]),
        ]),
      ]);
    });

    grid.replaceChildren(...cards);
  }

  function renderQuiesceButtons(listingId) {
    const actions = [
      ["price", "💸 Not Good Enough"],
      ["distance", "📍 Too Far"],
      ["timing", "⏳ Bad Timing"],
      ["wrong", "🚫 Wrong Item"],
      ["bought", "✅ Bought / Archive"],
    ];
    return el(
      "div",
      { class: "quiesce-row" },
      actions.map(([act, label]) =>
        el("button", {
          type: "button",
          class: "btn btn-secondary btn-sm",
          text: label,
          onClick: async () => {
            try {
              const res = await apiJson(`/api/listings/${listingId}/feedback`, {
                method: "POST",
                body: JSON.stringify({ action: act }),
              });
              toast(`Listing #${listingId} -> ${res.state}`);
              await refreshDashboard();
              if (state.selectedItemId) renderDetailBody(state.selectedItemId);
            } catch (err) {
              toast(`Feedback error: ${err.message}`);
            }
          },
        })
      )
    );
  }

  function renderDealCard(deal, includeItemName = true) {
    const titleNode = el("a", {
      href: safeHref(deal.url),
      target: "_blank",
      rel: "noopener",
      text: deal.title,
    });
    const badge = el("span", {
      class: `pill ${deal.meets_target ? "pill-good" : "pill-warn"}`,
      text: deal.meets_target ? "Meets Target" : "Near Miss",
    });
    const shipText = deal.shipping_usd > 0 ? ` (+${fmtUsd(deal.shipping_usd)} ship)` : "";
    const locText = [deal.location_text, deal.distance_miles !== null ? `${deal.distance_miles} mi` : null]
      .filter(Boolean)
      .join(" · ");
    const bundleText = deal.matched_bundle && deal.matched_bundle_names?.length
      ? `Bundle: ${deal.matched_bundle_names.join(" + ")}`
      : null;

    return el("article", { class: "deal-card" }, [
      el("div", { style: "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center;" }, [
        el("strong", {}, [includeItemName ? `#${deal.watch_item_id} ${deal.watch_item_name} — ` : "", titleNode]),
        badge,
      ]),
      el("div", {
        class: "muted",
        text: `${deal.provider} · Landed ${fmtUsd(deal.landed_cost_usd)}${shipText} · Score ${Math.round(deal.deal_score)}/100 · State: ${deal.state}${locText ? ` · ${locText}` : ""}`,
      }),
      bundleText ? el("div", { class: "good", text: bundleText }) : null,
      deal.deal_reason ? el("div", { class: "muted", text: deal.deal_reason }) : null,
      renderQuiesceButtons(deal.id),
    ]);
  }

  function renderAllDealsTab(data) {
    const container = document.getElementById("all-deals-list");
    if (!data.all_secondhand_deals.length) {
      container.replaceChildren(
        el("div", {
          class: "empty-state",
          text: "No viable 2nd-hand deals found yet across configured secondhand providers.",
        })
      );
      return;
    }
    container.replaceChildren(...data.all_secondhand_deals.map((d) => renderDealCard(d, true)));
  }

  function renderGapsTab(data) {
    const tbody = document.getElementById("gaps-tbody");
    const resolvedSection = document.getElementById("resolved-gaps-section");
    const resolvedTbody = document.getElementById("resolved-gaps-tbody");
    const allGaps = data.provider_gaps || [];
    const openGaps = allGaps.filter((g) => g.status === "open");
    const doneGaps = allGaps.filter((g) => g.status !== "open");

    const buildRow = (g, pillClass) =>
      el("tr", {}, [
        el("th", { scope: "row", text: g.retailer_name }),
        el("td", { dataset: { label: "Domain" } }, [el("code", { text: g.domain })]),
        el("td", { dataset: { label: "Source" }, text: g.recommended_by }),
        el("td", { dataset: { label: "Reason" }, text: g.reason }),
        el("td", { dataset: { label: "Sample Item" }, text: g.sample_item }),
        el("td", { dataset: { label: "Hits" }, text: g.occurrences }),
        el("td", { dataset: { label: "Status" } }, [el("span", { class: `pill ${pillClass}`, text: g.status })]),
      ]);

    if (!openGaps.length) {
      tbody.replaceChildren(
        el("tr", {}, [
          el("td", {
            colspan: 7,
            class: "muted",
            text: "No open scraper gaps — all tracked retailers are covered.",
          }),
        ])
      );
    } else {
      tbody.replaceChildren(...openGaps.map((g) => buildRow(g, "pill-warn")));
    }

    if (resolvedSection && resolvedTbody) {
      if (!doneGaps.length) {
        resolvedSection.hidden = true;
        resolvedTbody.replaceChildren();
      } else {
        resolvedSection.hidden = false;
        resolvedTbody.replaceChildren(
          ...doneGaps.map((g) =>
            buildRow(g, g.status === "resolved" ? "pill-good" : "pill-accent")
          )
        );
      }
    }
  }

  function renderProvidersTab(data) {
    const budgetPanel = document.getElementById("budget-panel");
    budgetPanel.replaceChildren(
      el("div", { style: "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;" }, [
        el("strong", { text: "Zero-Cost Human-Paced Browser & Free API Mode" }),
        el("span", { class: "good", text: "$0.00 / month" }),
      ]),
      el("div", { class: "muted", style: "font-size:0.84rem;margin-top:4px;", text: "Facebook Marketplace and Best Buy run via serialized Playwright Chromium sessions; Craigslist, Slickdeals, Woot, Reddit, and Universal Retail run zero-cost." })
    );

    const tbody = document.getElementById("providers-tbody");
    const rows = data.providers.map((p) =>
      el("tr", {}, [
        el("th", { scope: "row" }, [`${p.display_name} `, el("code", { text: p.name })]),
        el("td", { dataset: { label: "Market" }, text: p.market_type }),
        el("td", { dataset: { label: "Status" } }, [
          el("span", {
            class: `pill ${p.configured ? "pill-good" : "pill-warn"}`,
            text: p.configured ? "configured" : "awaiting API key",
          }),
        ]),
        el("td", {
          dataset: { label: "Normal / Hot Cadence" },
          text: `${fmtInterval(p.normal_interval_minutes)} / ${fmtInterval(p.hot_interval_minutes)}`,
        }),
        el("td", {
          dataset: { label: "Capabilities" },
          text: [p.supports_geo ? "geo" : null, p.supports_shipping ? "shipping" : null].filter(Boolean).join(", ") || "national/web",
        }),
        el("td", {
          dataset: { label: "Month Usage" },
          text: `${p.calls_this_month} calls ($${Number(p.spent_this_month_usd || 0).toFixed(2)})`,
        }),
      ])
    );
    tbody.replaceChildren(...rows);
  }

  function openDetailModal(itemId) {
    state.selectedItemId = itemId;
    renderDetailBody(itemId);
    const dialog = document.getElementById("detail-dialog");
    if (!dialog.open) dialog.showModal();
  }

  function renderDetailBody(itemId) {
    const item = state.data?.items.find((i) => i.id === itemId);
    if (!item) return;

    document.getElementById("detail-dialog-title").textContent = `#${item.id} — ${item.name}`;
    const body = document.getElementById("detail-content");

    const refreshPhotoBtn = el("button", {
      type: "button",
      class: "btn btn-secondary btn-sm",
      text: "Refresh Photo",
      onClick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Fetching...";
        try {
          const res = await apiJson(`/api/items/${item.id}/refresh-image`, { method: "POST" });
          toast(res.has_image ? `Updated product photo (${res.image_bytes || "external"} bytes)` : "No new photo found");
          await refreshDashboard();
          renderDetailBody(item.id);
        } catch (err) {
          toast(`Photo refresh failed: ${err.message}`);
        } finally {
          btn.disabled = false;
          btn.textContent = "Refresh Photo";
        }
      },
    });

    const seenExcluded = (state.excludedImages ||= {});
    const altPhotoBtn = el("button", {
      type: "button",
      class: "btn btn-secondary btn-sm",
      text: "Alt Image",
      onClick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Finding Alt...";
        try {
          const hist = (seenExcluded[item.id] ||= []);
          if (item.image_url && !hist.includes(item.image_url)) hist.push(item.image_url);
          const res = await apiJson(`/api/items/${item.id}/refresh-image`, {
            method: "POST",
            body: JSON.stringify({ alternative: true, exclude_urls: hist }),
          });
          if (res.image_url && !hist.includes(res.image_url)) hist.push(res.image_url);
          toast(res.has_image ? "Loaded alternative product image" : "No alternative image found");
          await refreshDashboard();
          renderDetailBody(item.id);
        } catch (err) {
          toast(`Alt image failed: ${err.message}`);
        } finally {
          btn.disabled = false;
          btn.textContent = "Alt Image";
        }
      },
    });

    const customPhotoBtn = el("button", {
      type: "button",
      class: "btn btn-ghost btn-sm",
      text: "Set URL",
      onClick: async () => {
        const url = window.prompt("Paste HTTPS product image URL:", item.image_url || "");
        if (!url || !url.trim().startsWith("https://")) return;
        try {
          await apiJson(`/api/items/${item.id}/refresh-image`, {
            method: "POST",
            body: JSON.stringify({ image_url: url.trim() }),
          });
          toast("Updated product image URL");
          await refreshDashboard();
          renderDetailBody(item.id);
        } catch (err) {
          toast(`Image URL failed: ${err.message}`);
        }
      },
    });

    const scanBtn = el("button", {
      type: "button",
      class: "btn btn-primary btn-sm",
      text: "Scan All Providers Now",
      onClick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Scanning...";
        try {
          const res = await apiJson(`/api/items/${item.id}/check`, { method: "POST" });
          toast(`Scanned: ${res.items_found} found, ${res.new_alerts} new alert(s)`);
          await refreshDashboard();
          renderDetailBody(item.id);
        } catch (err) {
          toast(`Scan failed: ${err.message}`);
        } finally {
          btn.disabled = false;
          btn.textContent = "Scan All Providers Now";
        }
      },
    });

    const hero = el("div", { class: "detail-hero" }, [
      el("div", { class: "detail-photo-box" }, [
        el("img", { src: item.image_endpoint, alt: item.name, width: 480, height: 320 }),
        el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;justify-content:center;" }, [
          refreshPhotoBtn,
          altPhotoBtn,
          customPhotoBtn,
        ]),
      ]),
      el("div", { style: "display:flex;flex-direction:column;gap:10px;" }, [
        el("div", { style: "display:flex;flex-wrap:wrap;gap:6px;align-items:center;" }, [
          el("span", { class: "pill pill-accent", text: item.category }),
          el("span", { class: `pill ${item.is_hot ? "pill-hot" : ""}`, text: item.is_hot ? "🔥 HOT" : "Standard" }),
          el("span", { class: "pill", text: `Status: ${item.status}` }),
          el("span", { class: "pill", text: `Radius: ${item.radius_miles} mi` }),
          scanBtn,
        ]),
        buildPriceLadder(item),
        item.induction_summary ? el("p", { style: "margin:0;", text: item.induction_summary }) : null,
        item.deal_likelihood ? el("div", { class: "muted", text: `Market Odds: ${item.deal_likelihood}` }) : null,
        item.historical_best_retail
          ? el("div", {
              class: "muted",
              text: `Historical Retail Best: ${fmtUsd(item.historical_best_retail.price_usd)} at ${item.historical_best_retail.name || item.historical_best_retail.domain}${
                item.target_gap?.historical_vs_target_usd != null
                  ? item.target_gap.historical_vs_target_usd <= 0
                    ? ` (${fmtUsd(Math.abs(item.target_gap.historical_vs_target_usd))} under target)`
                    : ` (+${fmtUsd(item.target_gap.historical_vs_target_usd)} vs target)`
                  : ""
              }`,
            })
          : null,
        item.bundle_components?.length
          ? el("div", {
              class: "good",
              text: `Bundle Allowances: ${item.bundle_components.map((b) => `${b.name} (+${fmtUsd(b.used_value_usd)} used / +${fmtUsd(b.new_value_usd)} new)`).join(" · ")}`,
            })
          : null,
      ]),
    ]);

    // Retailer Price Matrix Table
    const retailerRows = item.retailer_matrix.map((r) => {
      const cls =
        r.status === "below_target"
          ? "pill-good"
          : r.status === "blocked" || r.status === "unstructured"
            ? "pill-warn"
            : "";
      return el("tr", {}, [
        el("th", { scope: "row" }, [
          el("a", { href: safeHref(r.url), target: "_blank", rel: "noopener", text: r.name }),
        ]),
        el("td", { dataset: { label: "Domain" } }, [el("code", { text: r.domain })]),
        el("td", { dataset: { label: "Price" }, text: r.price_usd !== null ? fmtUsd(r.price_usd) : "—" }),
        el("td", {
          dataset: { label: "vs MSRP" },
          text: r.pct_vs_baseline !== null ? `${r.pct_vs_baseline}%` : "—",
        }),
        el("td", { dataset: { label: "Status" } }, [el("span", { class: `pill ${cls}`, text: r.status_label })]),
        el("td", { dataset: { label: "Notes" }, text: r.notes || r.title || "" }),
      ]);
    });

    const retailerSection = el("section", {}, [
      el("h2", { id: "detail-retailers-title", style: "margin:0 0 8px;font-size:1rem;", text: "Retailers & Current Prices" }),
      el("div", { tabindex: "0", role: "region", "aria-labelledby": "detail-retailers-title", class: "table-wrapper" }, [
        el("table", { class: "data-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { scope: "col", text: "Retailer" }),
              el("th", { scope: "col", text: "Domain" }),
              el("th", { scope: "col", text: "Price" }),
              el("th", { scope: "col", text: "vs MSRP" }),
              el("th", { scope: "col", text: "Status" }),
              el("th", { scope: "col", text: "Notes" }),
            ]),
          ]),
          el("tbody", {}, retailerRows.length ? retailerRows : [el("tr", {}, [el("td", { colspan: 6, class: "muted", text: "No retailers tracked yet." })])]),
        ]),
      ]),
    ]);

    // Viable 2nd-Hand Deals for this Item
    const shSection = el("section", {}, [
      el("h2", { style: "margin:0 0 8px;font-size:1rem;", text: `Viable 2nd-Hand Deals (${item.viable_secondhand_deals.length})` }),
      item.viable_secondhand_deals.length
        ? el("div", { class: "deals-feed" }, item.viable_secondhand_deals.map((d) => renderDealCard(d, false)))
        : el("div", { class: "empty-state", text: `No viable secondhand listings yet within ${item.radius_miles} mi or configured online markets.` }),
    ]);

    // Cadence Table
    const cadenceRows = item.cadence.map((c) =>
      el("tr", {}, [
        el("th", { scope: "row", text: c.display_name }),
        el("td", { dataset: { label: "Interval" }, text: fmtInterval(c.interval_minutes) }),
        el("td", { dataset: { label: "Last Checked" }, text: fmtRelative(c.last_run_at) }),
        el("td", { dataset: { label: "Next Due" }, text: c.configured ? fmtRelative(c.next_due_at) : "awaiting key" }),
        el("td", { dataset: { label: "Health" } }, [
          el("span", {
            class: `pill ${c.last_status === "ok" ? "pill-good" : c.last_status === "error" ? "pill-warn" : ""}`,
            text: c.configured ? c.last_status : "unconfigured",
          }),
        ]),
        el("td", { dataset: { label: "Hits" }, text: `${c.items_found} (${c.new_alerts} alerts)` }),
      ])
    );

    const cadenceSection = el("section", {}, [
      el("h2", { id: "detail-cadence-title", style: "margin:0 0 8px;font-size:1rem;", text: "Search Frequency & Provider Run Health" }),
      el("div", { tabindex: "0", role: "region", "aria-labelledby": "detail-cadence-title", class: "table-wrapper" }, [
        el("table", { class: "data-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { scope: "col", text: "Provider" }),
              el("th", { scope: "col", text: "Interval" }),
              el("th", { scope: "col", text: "Last Run" }),
              el("th", { scope: "col", text: "Next Due" }),
              el("th", { scope: "col", text: "Status" }),
              el("th", { scope: "col", text: "Items Found" }),
            ]),
          ]),
          el("tbody", {}, cadenceRows),
        ]),
      ]),
    ]);

    // Edit Watch Form
    const editForm = el("form", {
      class: "budget-card",
      onSubmit: async (e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        const payload = {
          retail_baseline_usd: fd.get("retail") ? Number(fd.get("retail")) : null,
          new_deal_target_usd: fd.get("new_target") ? Number(fd.get("new_target")) : null,
          used_deal_target_usd: fd.get("used_target") ? Number(fd.get("used_target")) : null,
          radius_miles: fd.get("radius") ? Number(fd.get("radius")) : item.radius_miles,
          status: String(fd.get("status") || item.status),
          is_hot: fd.get("is_hot") === "on",
          search_queries: String(fd.get("queries") || "")
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          negative_keywords: String(fd.get("negatives") || "")
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        };
        try {
          await apiJson(`/api/items/${item.id}`, { method: "PATCH", body: JSON.stringify(payload) });
          toast(`Saved Watch #${item.id}`);
          await refreshDashboard();
          renderDetailBody(item.id);
        } catch (err) {
          toast(`Save failed: ${err.message}`);
        }
      },
    }, [
      el("h2", { style: "margin:0 0 10px;font-size:1rem;", text: "Edit Targets, Queries & Cadence Mode" }),
      el("div", { class: "form-grid" }, [
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "Retail Baseline ($)" }),
          el("input", { class: "input", name: "retail", type: "number", step: "0.01", value: item.retail_baseline_usd ?? "" }),
        ]),
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "New Target ($)" }),
          el("input", { class: "input", name: "new_target", type: "number", step: "0.01", value: item.new_deal_target_usd ?? "" }),
        ]),
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "Used Target ($)" }),
          el("input", { class: "input", name: "used_target", type: "number", step: "0.01", value: item.used_deal_target_usd ?? "" }),
        ]),
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "Radius (miles)" }),
          el("input", { class: "input", name: "radius", type: "number", min: 5, max: 250, value: item.radius_miles }),
        ]),
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "Status" }),
          el("select", { class: "select", name: "status" }, ["active", "pending_approval", "paused", "archived"].map((s) =>
            el("option", { value: s, text: s, selected: item.status === s })
          )),
        ]),
        el("label", { class: "check-row", style: "align-self:end;padding-bottom:6px;" }, [
          el("input", { type: "checkbox", name: "is_hot", checked: item.is_hot }),
          el("span", { text: "🔥 HOT item cadence" }),
        ]),
      ]),
      el("div", { class: "form-grid", style: "margin-top:10px;" }, [
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "Search Queries (comma-separated)" }),
          el("input", { class: "input", name: "queries", value: (item.search_queries || []).join(", ") }),
        ]),
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: "Negative Keywords (comma-separated)" }),
          el("input", { class: "input", name: "negatives", value: (item.negative_keywords || []).join(", ") }),
        ]),
      ]),
      el("div", { class: "dialog-footer" }, [
        el("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Save Changes" }),
      ]),
    ]);

    body.replaceChildren(hero, retailerSection, shSection, cadenceSection, editForm);
  }

  function renderAll(data) {
    state.data = data;
    renderSummaryAndFilters(data);
    renderWatchGrid(data);
    renderAllDealsTab(data);
    renderGapsTab(data);
    renderProvidersTab(data);
    renderHermesStatus(data);
  }

  async function refreshDashboard() {
    const pill = document.getElementById("sync-status");
    pill.textContent = "Syncing...";
    try {
      const data = await apiJson("/api/dashboard");
      renderAll(data);
      pill.textContent = "Live";
    } catch (err) {
      pill.textContent = "Error";
      toast(`Refresh error: ${err.message}`);
    }
  }

  function setupAddDialog() {
    const dialog = document.getElementById("add-dialog");
    const openBtn = document.getElementById("open-add-btn");
    const form = document.getElementById("add-item-form");
    const submitBtn = document.getElementById("add-submit-btn");
    const progress = document.getElementById("add-progress");

    openBtn.addEventListener("click", () => {
      progress.hidden = true;
      form.reset();
      document.getElementById("add-auto-activate").checked = true;
      document.getElementById("add-notify-tg").checked = true;
      dialog.showModal();
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const raw = document.getElementById("add-raw-request").value.trim();
      if (!raw) return;
      submitBtn.disabled = true;
      submitBtn.textContent = "Researching...";
      progress.hidden = false;
      progress.textContent = "Researching new/used pricing, negative guards, trusted retailers, local Craigslist & fetching product photo...";
      try {
        const radiusRaw = document.getElementById("add-radius-miles")?.value;
        const radiusVal = radiusRaw ? Number(radiusRaw) : null;
        const created = await apiJson("/api/items/induct", {
          method: "POST",
          body: JSON.stringify({
            raw_request: raw,
            is_hot: document.getElementById("add-is-hot").checked,
            auto_activate: document.getElementById("add-auto-activate").checked,
            notify_telegram: document.getElementById("add-notify-tg").checked,
            fetch_image: true,
            ...(radiusVal ? { radius_miles: radiusVal } : {}),
          }),
        });
        dialog.close();
        await refreshDashboard();
        toast(`Inducted #${created.id}: ${created.name}`);
        openDetailModal(created.id);
      } catch (err) {
        progress.textContent = `Induction failed: ${err.message}`;
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Research & Induct";
      }
    });
  }

  function setupToolbarAndActions() {
    for (const id of ["filter-search", "filter-status", "filter-category", "filter-hot", "filter-below-target", "sort-items"]) {
      const node = document.getElementById(id);
      if (!node) continue;
      node.addEventListener(id === "filter-search" ? "input" : "change", () => {
        if (state.data) renderWatchGrid(state.data);
      });
    }

    const switchToTab = (tabId) => {
      const tab = document.getElementById(tabId);
      if (tab) tab.click();
    };
    document.getElementById("kpi-card-active")?.addEventListener("click", () => {
      const bt = document.getElementById("filter-below-target");
      if (bt) bt.checked = false;
      document.getElementById("filter-status").value = "active";
      switchToTab("tab-watchlist");
      if (state.data) renderWatchGrid(state.data);
    });
    document.getElementById("kpi-card-secondhand")?.addEventListener("click", () => switchToTab("tab-deals"));
    document.getElementById("kpi-card-below-target")?.addEventListener("click", () => {
      const bt = document.getElementById("filter-below-target");
      if (bt) bt.checked = true;
      document.getElementById("filter-status").value = "all";
      switchToTab("tab-watchlist");
      if (state.data) renderWatchGrid(state.data);
    });
    document.getElementById("kpi-card-providers")?.addEventListener("click", () => switchToTab("tab-providers"));
    document.getElementById("kpi-card-gaps")?.addEventListener("click", () => switchToTab("tab-gaps"));

    document.getElementById("refresh-btn").addEventListener("click", refreshDashboard);
    const hermesBtn = document.getElementById("hermes-status-btn");
    if (hermesBtn) hermesBtn.addEventListener("click", openHermesModal);

    document.getElementById("scan-all-btn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const active = (state.data?.items || []).filter((i) => i.status === "active");
      if (!active.length) {
        toast("No active items to scan.");
        return;
      }
      btn.disabled = true;
      btn.textContent = `Scanning ${active.length}...`;
      try {
        let totalFound = 0;
        let totalAlerts = 0;
        for (const it of active) {
          const res = await apiJson(`/api/items/${it.id}/check`, { method: "POST" });
          totalFound += res.items_found || 0;
          totalAlerts += res.new_alerts || 0;
        }
        await refreshDashboard();
        toast(`Scanned ${active.length} active items: ${totalFound} found, ${totalAlerts} new alert(s).`);
      } catch (err) {
        toast(`Bulk scan failed: ${err.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = "Scan All Active";
      }
    });
  }

  function init() {
    setupThemeToggle();
    setupTabs();
    setupDialogs();
    setupAddDialog();
    setupToolbarAndActions();

    const bootstrapEl = document.getElementById("argos-bootstrap");
    if (bootstrapEl?.textContent) {
      try {
        renderAll(JSON.parse(bootstrapEl.textContent));
      } catch {}
    }

    setInterval(() => {
      if (!document.hidden) refreshDashboard();
    }, 60000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
