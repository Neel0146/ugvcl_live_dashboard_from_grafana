(function () {
  "use strict";
  const POLL_LIVE = 15000, POLL_PENDING = 2000, POLL_HISTORY = 60000;
  const fmt = (n) => (n == null ? "—" : new Intl.NumberFormat("en-IN").format(n));
  const pctTxt = (p) => (p == null ? "—" : p.toFixed(1) + "%");
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (id) => document.getElementById(id);

  const PROFILES = [
    ["midnight", "Midnight"], ["load_profile", "Load profile"], ["instant", "Instant"], ["billing", "Billing"],
  ];
  const SECTION_LABELS = {
    devices: "Device counts (RF / GPRS)", expected: "Expected meters per profile",
    midnight: "Midnight data", load_profile: "Load profile data", instant: "Instant data", billing: "Billing data",
    trend: "Midnight trend", metering_mode: "Metering mode", lp_hourly: "Load profile by hour",
  };

  let ccLast = null;
  let view = null, viewOk = false, attrData = null, attrName = null;
  let selDate = null, trendDays = 7, reqToken = 0, pollTimer = null, loadError = "";
  const charts = {};

  // ---------- dates ----------
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  const parseYmd = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
  const addDays = (s, n) => { const d = parseYmd(s); d.setDate(d.getDate() + n); return ymd(d); };
  const todayStr = () => ymd(new Date());
  const dLabel = (s) => parseYmd(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  const dShort = (s) => parseYmd(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  const dLong = (s) => parseYmd(s).toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
  const isToday = () => !view || view.view.is_today;

  // ---------- theme ----------
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function palette() {
    return { ink: css("--ink"), ink2: css("--ink2"), muted: css("--muted"), grid: css("--grid"), axis: css("--axis"),
             surface: css("--surface"), series: [css("--s1"), css("--s2"), css("--s3"), css("--s4")] };
  }
  function effectiveTheme() {
    const t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("ugvcl-theme", t); } catch (e) {}
    Object.keys(charts).forEach((k) => { charts[k].destroy(); delete charts[k]; });
    renderAll();
    if (ccLast) ccShow(ccLast);
  }
  try { const t = localStorage.getItem("ugvcl-theme"); if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t); } catch (e) {}
  $("themeBtn").addEventListener("click", () => setTheme(effectiveTheme() === "dark" ? "light" : "dark"));

  // ---------- chart helpers ----------
  function chartOptions(p, o) {
    o = o || {};
    const valueAxis = { beginAtZero: true, min: o.min, max: o.max, grid: { color: p.grid }, border: { display: false },
      ticks: { color: p.muted, callback: o.tick || ((v) => fmt(v)) } };
    const catAxis = { grid: { display: false }, border: { color: p.axis }, ticks: { color: p.muted, maxRotation: 0, autoSkip: true } };
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      indexAxis: o.horizontal ? "y" : "x",
      interaction: { mode: "index", intersect: false },
      scales: o.horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis },
      plugins: {
        legend: { display: o.legend !== false, position: "top", align: "start",
                  labels: { color: p.ink2, usePointStyle: true, pointStyle: "rectRounded", boxWidth: 8, boxHeight: 8 } },
        tooltip: { callbacks: o.tooltip || {} },
      },
    };
  }
  function setEmpty(id, empty, text) {
    const box = $(id).parentElement;
    let el = box.querySelector(".empty");
    if (!empty) { if (el) el.hidden = true; return; }
    if (!el) { el = document.createElement("div"); el.className = "empty"; box.appendChild(el); }
    el.textContent = text || "No data to show"; el.hidden = false;
  }
  function draw(id, type, labels, datasets, opts) {
    const p = palette();
    if (charts[id]) {
      const c = charts[id];
      c.data.labels = labels; c.data.datasets = datasets;
      const hs = opts && opts.min !== undefined; // keep axis bounds current
      if (hs) { const ax = (opts.horizontal ? c.options.scales.x : c.options.scales.y); ax.min = opts.min; ax.max = opts.max; }
      c.update("none");
    } else {
      charts[id] = new Chart($(id), { type, data: { labels, datasets }, options: chartOptions(p, opts) });
    }
    const none = datasets.every((d) => d.data.every((v) => v == null || v === 0));
    setEmpty(id, none, (opts && opts.emptyText) || "No data yet for this chart");
  }
  const clrColor = (i) => palette().series[i % 4];
  function barSet(name, i, data, extra) {
    return Object.assign({ label: name, data, backgroundColor: clrColor(i), borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 34 }, extra || {});
  }
  function totalBar(label, data, extra) {
    return Object.assign({ label, data, backgroundColor: palette().ink, borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 34, _total: true }, extra || {});
  }
  function totalLine(label, data, extra) {
    const c = palette().ink;
    return Object.assign({ label, data, borderColor: c, backgroundColor: c, borderWidth: 2.5, borderDash: [6, 3], pointRadius: 2.5,
      pointHoverRadius: 5, tension: 0.2, spanGaps: false, _total: true }, extra || {});
  }
  const totalName = (n) => (n != null && view && n < view.clr_order.length ? "Total (" + n + " of " + view.clr_order.length + " CLRs)" : "Total (all CLRs)");

  // ---------- derived ----------
  function clrs() { return view ? view.clr_order : []; }
  function get(clr) { return (view && view.clrs[clr]) || {}; }
  function T() { return (view && view.totals) || {}; }
  function periodWord(m) {
    if (!view) return "";
    if (m === "billing") return view.view.is_today ? "this month" : "for " + view.view.month;
    return view.view.is_today ? "today" : "on " + dShort(view.view.date);
  }

  // ---------- header + toolbar ----------
  function renderHeader() {
    if (!view) return;
    $("appTitle").textContent = view.app.title; document.title = view.app.title;
    $("appSub").textContent = view.app.subtitle || "";
    const v = view.view, t = todayStr();
    $("dDate").value = v.date; $("dDate").max = t; $("dDate").min = addDays(t, -365);
    $("dNext").disabled = v.date >= t;
    $("dToday").classList.toggle("on", v.date === t);
    $("dYest").classList.toggle("on", v.date === addDays(t, -1));
    $("dTrend").value = String(v.trend_days);
    $("dLabel").textContent = v.is_today ? dLong(v.date) + " · live, refreshing automatically" : dLong(v.date) + " · history";
    $("expNote").hidden = v.is_today;
    $("tCoverage").textContent = (v.is_today ? "today" : "on " + dShort(v.date)) + ", % of expected meters";
    $("tTrend").textContent = "last " + v.trend_days + " days" + (v.is_today ? "" : " up to " + dShort(v.date)) + ", % of expected meters";
    $("tHourly").textContent = "meters with load-profile data in each hour, " + (v.is_today ? "today" : dShort(v.date));
    $("tNonComm").textContent = "expected minus received, " + (v.is_today ? "today" : dShort(v.date));
    $("trendNote").textContent = (v.is_today ? "Today (dashed) is still filling up. " : "") + "Axis starts above zero to make day-to-day changes visible.";
  }
  function syncUrl() {
    try {
      const u = new URL(location.href);
      if (selDate && selDate !== todayStr()) u.searchParams.set("date", selDate); else u.searchParams.delete("date");
      if (trendDays !== 7) u.searchParams.set("days", trendDays); else u.searchParams.delete("days");
      history.replaceState(null, "", u);
    } catch (e) {}
  }
  function setDate(s) {
    const t = todayStr();
    if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return;
    if (s > t) s = t;
    selDate = s === t ? null : s;
    syncUrl(); load(false);
  }
  $("dPrev").addEventListener("click", () => setDate(addDays(selDate || todayStr(), -1)));
  $("dNext").addEventListener("click", () => setDate(addDays(selDate || todayStr(), 1)));
  $("dToday").addEventListener("click", () => setDate(todayStr()));
  $("dYest").addEventListener("click", () => setDate(addDays(todayStr(), -1)));
  $("dDate").addEventListener("change", (e) => setDate(e.target.value));
  $("dTrend").addEventListener("change", (e) => { trendDays = Number(e.target.value) || 7; syncUrl(); load(false); });
  $("dRefresh").addEventListener("click", () => load(true));

  // ---------- KPI tiles ----------
  function renderKpis() {
    const nClr = clrs().length, tiles = [], tot = T();
    const partial = (n) => (n && n < nClr ? ` &middot; ${n} of ${nClr} CLRs` : "");
    const d = tot.devices;
    tiles.push(`<div class="kpi"><div class="lbl">Total meters</div><div class="val">${fmt(d && d.total)}</div>
      <div class="sub2">all 4 CLRs, templates excluded${d ? partial(d.n) : ""}</div></div>`);
    PROFILES.forEach(([m, label]) => {
      const c = tot.coverage && tot.coverage[m];
      if (c) {
        tiles.push(`<div class="kpi"><div class="lbl">${esc(label)} ${esc(periodWord(m))}</div>
          <div class="val">${c.pct == null ? "—" : c.pct.toFixed(1)}<small>%</small></div>
          <div class="sub2">${fmt(c.received)} of ${fmt(c.expected)} meters${partial(c.n)}</div>
          <div class="bar"><i style="width:${c.pct || 0}%"></i></div>
          ${c.over_expected ? '<div class="flag">▲ received is above expected on at least one CLR</div>' : ""}</div>`);
      } else {
        tiles.push(`<div class="kpi"><div class="lbl">${esc(label)} ${esc(periodWord(m))}</div><div class="val">—</div><div class="sub2">${view && view.pending ? "loading…" : "no data"}</div></div>`);
      }
    });
    const nc = tot.non_comm_midnight;
    tiles.push(`<div class="kpi"><div class="lbl">Midnight not received ${esc(periodWord("midnight"))}</div><div class="val">${fmt(nc && nc.value)}</div>
      <div class="sub2">expected minus received${nc ? partial(nc.n) : ""}</div></div>`);
    $("kpis").innerHTML = tiles.join("");
  }

  // ---------- charts ----------
  function trendPct(clr) {
    const tr = get(clr).trend, exp = ((get(clr).coverage || {}).midnight || {}).expected;
    return { tr, exp };
  }
  function renderCharts() {
    if (!view) return;
    const C = clrs(), tot = T(), v = view.view;

    // 1) received by profile, % of expected (CLRs + total)
    const cov = tot.coverage || {};
    const covTotN = Math.min.apply(null, PROFILES.map(([m]) => (cov[m] ? cov[m].n : 99)));
    draw("chCoverage", "bar", PROFILES.map(([m, l]) => (m === "billing" ? "Billing (month)" : l)),
      C.map((c, i) => barSet(c, i, PROFILES.map(([m]) => { const x = (get(c).coverage || {})[m]; return x ? x.pct : null; }), { _c: c }))
        .concat([totalBar(totalName(covTotN === 99 ? null : covTotN), PROFILES.map(([m]) => (cov[m] ? cov[m].pct : null)), { _c: "TOTAL" })]),
      { min: 0, max: 100, tick: (x) => x + "%", emptyText: view.pending ? "Loading…" : "No data for this date",
        tooltip: { label: (ctx) => {
          const m = PROFILES[ctx.dataIndex][0];
          const x = ctx.dataset._c === "TOTAL" ? cov[m] : ((get(ctx.dataset._c).coverage) || {})[m];
          return x && x.pct != null ? `${ctx.dataset.label}: ${pctTxt(x.pct)}  (${fmt(x.received)} of ${fmt(x.expected)})` : `${ctx.dataset.label}: no data`; } } });

    // 2) midnight trend, % of expected
    const trT = tot.trend;
    const allDates = Array.from(new Set([].concat(...C.map((c) => (get(c).trend || []).map((r) => r.date))))).sort();
    const lineSet = (label, color, raw, exp, isTotal) => {
      const o = { label, _raw: raw, _exp: exp, borderColor: color, backgroundColor: color, borderWidth: isTotal ? 2.5 : 2, pointRadius: 3, pointHoverRadius: 5,
        tension: 0.2, spanGaps: false, data: raw.map((x) => (x == null || !exp ? null : Math.min(100, (100 * x) / exp))),
        segment: { borderDash: (ctx) => (v.is_today && ctx.p1DataIndex === allDates.length - 1 ? [5, 4] : (isTotal ? [6, 3] : undefined)) } };
      return o;
    };
    const trendSets = C.map((c, i) => {
      const { tr, exp } = trendPct(c);
      const by = {}; (tr || []).forEach((r) => (by[r.date] = r.meters));
      return lineSet(c, clrColor(i), allDates.map((d) => (by[d] == null ? null : by[d])), exp, false);
    });
    if (trT) {
      const withTrend = C.filter((c) => get(c).trend);
      const expSum = withTrend.reduce((a, c) => a + (((get(c).coverage || {}).midnight || {}).expected || 0), 0);
      const by = {}; trT.points.forEach((r) => (by[r.date] = r.meters));
      trendSets.push(lineSet(totalName(trT.n), palette().ink, allDates.map((d) => (by[d] == null ? null : by[d])), expSum, true));
    }
    let lo = 100;
    trendSets.forEach((s) => s.data.forEach((x, i) => { if (x != null && !(v.is_today && i === allDates.length - 1)) lo = Math.min(lo, x); }));
    draw("chMid7", "line", allDates.map(dShort), trendSets,
      { min: Math.max(0, Math.floor((lo - 5) / 10) * 10), max: 100, tick: (x) => x + "%", emptyText: view.pending ? "Loading…" : "No data for these dates",
        tooltip: { label: (ctx) => { const ds = ctx.dataset, raw = ds._raw[ctx.dataIndex];
          return `${ds.label}: ${pctTxt(ctx.parsed.y)}  (${fmt(raw)} of ${fmt(ds._exp)})`; } } });

    // 3) load profile by hour (counts)
    const hours = Array.from({ length: 24 }, (_, h) => pad(h) + ":00");
    const mask = (arr) => { if (!v.is_today) return arr; let last = -1; arr.forEach((x, h) => { if (x > 0) last = h; }); return arr.map((x, h) => (h > last ? null : x)); };
    const hourSets = C.map((c, i) => ({ label: c, borderColor: clrColor(i), backgroundColor: clrColor(i), borderWidth: 2, pointRadius: 2.5,
      pointHoverRadius: 5, tension: 0.2, spanGaps: false, data: get(c).lp_hourly ? mask(get(c).lp_hourly.slice()) : hours.map(() => null) }));
    if (tot.lp_hourly) hourSets.push(totalLine(totalName(tot.lp_hourly.n), mask(tot.lp_hourly.values.slice())));
    draw("chHourly", "line", hours, hourSets,
      { emptyText: view.pending ? "Loading…" : "No hourly data for this date", tooltip: { label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)} meters` } });

    // 4) communication type (current)
    const d = tot.devices;
    draw("chComm", "bar", ["RF", "GPRS", "Unclassified"],
      C.map((c, i) => { const x = get(c).devices; return barSet(c, i, x ? [x.rf, x.gprs, x.unclassified] : [null, null, null]); })
        .concat(d ? [totalBar(totalName(d.n), [d.rf, d.gprs, d.unclassified])] : []),
      { tooltip: { label: (ctx) => {
        const x = ctx.dataset._total ? d : (get(C[ctx.datasetIndex]).devices); const pct = x && x.total ? (100 * ctx.parsed.y) / x.total : null;
        return `${ctx.dataset.label}: ${fmt(ctx.parsed.y)} meters (${pctTxt(pct)} of ${ctx.dataset._total ? "all CLRs" : "CLR"})`; } } });

    // 5) midnight not received
    const nc = tot.non_comm_midnight;
    draw("chNonComm", "bar", C.concat(["Total"]),
      [{ label: "Midnight not received", data: C.map((c) => (get(c).non_comm_midnight != null ? get(c).non_comm_midnight : null)).concat([nc ? nc.value : null]),
         backgroundColor: C.map((_, i) => clrColor(i)).concat([palette().ink]), borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 48 }],
      { legend: false, emptyText: view.pending ? "Loading…" : "No data for this date", tooltip: { label: (ctx) => `${fmt(ctx.parsed.y)} meters` } });

    // 6) metering mode (current)
    const mm = tot.metering_mode, modes = mm ? mm.values.slice(0, 12).map((x) => x.value) : [];
    draw("chMode", "bar", modes,
      C.map((c, i) => { const m = {}; (get(c).metering_mode || []).forEach((r) => (m[r.value] = r.count)); return barSet(c, i, modes.map((x) => (m[x] == null ? 0 : m[x]))); })
        .concat(mm ? [totalBar(totalName(mm.n), mm.values.slice(0, 12).map((x) => x.count))] : []),
      { emptyText: "The database returned no metering-mode values (attribute key from the Metering_Mode dashboard). Run: python diagnose.py CLR1 metering_mode",
        tooltip: { label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)} meters` } });

    renderAttrChart();
  }

  function renderAttrChart() {
    if (!attrData) return;
    const C = clrs();
    const totals = {};
    C.forEach((c) => (attrData.clrs[c] || []).forEach((r) => (totals[r.value] = (totals[r.value] || 0) + r.count)));
    const vals = Object.keys(totals).sort((a, b) => totals[b] - totals[a]).slice(0, 10);
    const nAttr = C.filter((c) => attrData.clrs[c]).length;
    draw("chAttr", "bar", vals,
      C.map((c, i) => { const m = {}; (attrData.clrs[c] || []).forEach((r) => (m[r.value] = r.count)); return barSet(c, i, vals.map((x) => (m[x] == null ? 0 : m[x]))); })
        .concat([totalBar(totalName(nAttr), vals.map((x) => totals[x]))]),
      { horizontal: true, emptyText: "No meters have this attribute", tooltip: { label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.x)} meters` } });
    const notes = [];
    const miss = Object.keys(attrData.errors || {});
    if (miss.length) notes.push("No data from " + miss.join(", ") + " (" + esc(attrData.errors[miss[0]]) + ").");
    if ((attrData.truncated || []).length) notes.push("Only the 200 most common values were read for " + attrData.truncated.join(", ") + ".");
    const all = Object.keys(totals).length;
    if (all > 10) notes.push("Showing the top 10 of " + all + " values.");
    $("attrNote").innerHTML = notes.length ? notes.join(" ") : "Pick any device attribute to see how meters split across its values. Current values, not date-based.";
  }

  // ---------- tables ----------
  let detailRows = [];
  function renderDetail() {
    if (!view) return;
    const C = clrs(), tot = T(), hdr = view.view.is_today ? "today" : dShort(view.view.date);
    const rows = [
      ["Total meters", (d) => d.devices && d.devices.total, tot.devices && tot.devices.total],
      ["RF meters", (d) => d.devices && d.devices.rf, tot.devices && tot.devices.rf],
      ["GPRS meters", (d) => d.devices && d.devices.gprs, tot.devices && tot.devices.gprs],
      ["Unclassified", (d) => d.devices && d.devices.unclassified, tot.devices && tot.devices.unclassified],
    ];
    PROFILES.forEach(([m, label]) => {
      const t = tot.coverage && tot.coverage[m];
      const cv = (k) => (d) => (d.coverage && d.coverage[m] ? d.coverage[m][k] : null);
      rows.push([label + " received", cv("received"), t && t.received]);
      rows.push([label + " expected", cv("expected"), t && t.expected]);
      rows.push([label + " %", cv("pct"), t && t.pct, true]);
    });
    rows.push(["Midnight not received", (d) => d.non_comm_midnight, tot.non_comm_midnight && tot.non_comm_midnight.value]);
    let html = "<thead><tr><th>" + esc(hdr) + "</th>" + C.map((c, i) => `<th><span class="swatch" style="background:${clrColor(i)}"></span>${esc(c)}</th>`).join("") +
      `<th><span class="swatch" style="background:${palette().ink}"></span>Total (all CLRs)</th></tr></thead><tbody>`;
    detailRows = [["Metric (" + view.view.date + ")"].concat(C, ["Total (all CLRs)"])];
    rows.forEach(([label, fn, total, isPct]) => {
      const vals = C.map((c) => { const x = fn(get(c)); return x == null ? null : x; });
      const f = (x) => (isPct ? pctTxt(x) : fmt(x));
      const partial = vals.some((x) => x == null) && vals.some((x) => x != null);
      html += `<tr><td>${esc(label)}</td>` + vals.map((x) => `<td>${f(x)}</td>`).join("") + `<td><b>${total == null ? "—" : f(total)}${partial ? " *" : ""}</b></td></tr>`;
      detailRows.push([label].concat(vals.map((x) => (x == null ? "" : x)), [total == null ? "" : total]));
    });
    $("detailTable").innerHTML = html + "</tbody>";
  }
  $("detailCsv").addEventListener("click", () => {
    const cell = (x) => '"' + String(x == null ? "" : x).replace(/"/g, '""') + '"';
    const blob = new Blob(["﻿" + detailRows.map((r) => r.map(cell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = "meter-data-" + (view ? view.view.date : "") + ".csv"; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });

  function ago(ts) { if (!ts) return "never"; const s = Math.max(0, Math.round(Date.now() / 1000 - ts)); return s < 90 ? s + " s ago" : s < 5400 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago"; }
  function renderHealth() {
    if (!view) return;
    let html = "<thead><tr><th>Section</th><th>Shows</th><th>Last update</th><th>Refreshes</th><th>Slowest query</th><th>Status</th></tr></thead><tbody>";
    Object.keys(SECTION_LABELS).forEach((k) => {
      const s = view.sections[k]; if (!s) return;
      const errs = Object.entries(s.errors || {});
      const status = errs.length ? `<td class="err">${errs.map(([c, m]) => esc(c) + ": " + esc(m)).join("<br>")}</td>` : `<td>${s.loaded ? "OK" : "waiting"}</td>`;
      const el = Object.values(s.elapsed || {}); const slow = el.length ? Math.max.apply(null, el) + " s" : "—";
      html += `<tr><td>${esc(SECTION_LABELS[k])}</td><td>${s.scope === "date" ? "selected date" : "current"}</td><td>${ago(s.newest_update)}</td><td>${s.ttl ? "every " + s.ttl + " s" : "cached"}</td><td>${slow}</td>${status}</tr>`;
    });
    $("healthTable").innerHTML = html + "</tbody>";
  }

  // ---------- status + alerts ----------
  function renderStatus() {
    const pill = $("statusPill"), txt = $("statusText"), alerts = $("alerts");
    let cls = "pill-wait", text = "Connecting…", msgs = [], bad = false;
    if (loadError) msgs.push("<b>" + esc(loadError) + "</b>");
    if (!viewOk && !view) { cls = "pill-bad"; text = "Dashboard server unreachable"; }
    else if (!viewOk) { cls = "pill-bad"; text = "Connection lost – showing last data"; }
    else {
      const secs = Object.entries(view.sections).filter(([k]) => k !== "trend" || true);
      const loaded = secs.filter(([, s]) => s.loaded).length;
      const errs = []; secs.forEach(([k, s]) => Object.entries(s.errors || {}).forEach(([c, m]) => errs.push([k, c, m])));
      const nowS = Date.now() / 1000;
      const stale = secs.filter(([, s]) => s.ttl && s.newest_update && nowS - s.newest_update > 3 * s.ttl + 30).map(([k]) => SECTION_LABELS[k]);
      if (view.pending > 0 && !view.view.is_today) { cls = "pill-wait"; text = "Loading " + dShort(view.view.date) + "…"; }
      else if (loaded === 0 && !errs.length) { cls = "pill-wait"; text = "Loading first results…"; }
      else if (loaded === 0) { cls = "pill-bad"; text = "No data from Grafana"; bad = true; }
      else if (errs.length) { cls = "pill-warn"; text = "Partial data"; }
      else if (stale.length) { cls = "pill-warn"; text = "Stale data"; }
      else { cls = "pill-ok"; text = view.view.is_today ? "Live" : "History loaded"; }
      if (errs.length) {
        const byMsg = {};
        errs.forEach(([k, c, m]) => { (byMsg[SECTION_LABELS[k] + " – " + c] = m); });
        msgs.push("<b>Some queries are failing. The tiles and charts below may be incomplete.</b><ul>" +
          Object.entries(byMsg).slice(0, 8).map(([k, m]) => `<li>${esc(k)}: ${esc(m)}</li>`).join("") + "</ul>");
      }
      if (stale.length) msgs.push("<b>Not refreshed recently:</b> " + esc(stale.join(", ")));
      const t = Math.max.apply(null, secs.map(([, s]) => s.newest_update || 0));
      $("updated").textContent = t ? "Data from " + new Date(t * 1000).toLocaleTimeString() + " (" + ago(t) + ")" : "";
    }
    pill.className = "pill " + cls; txt.textContent = text;
    alerts.hidden = !msgs.length; alerts.className = "alerts" + (bad || loadError ? " bad" : ""); alerts.innerHTML = msgs.join("");
  }

  function renderAll() { renderHeader(); renderStatus(); renderKpis(); renderCharts(); renderDetail(); renderHealth(); }

  // ---------- data loading ----------
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }
  async function load(force) {
    const my = ++reqToken;
    clearTimeout(pollTimer);
    let delay = POLL_LIVE;
    try {
      const url = "/api/view?days=" + trendDays + (selDate ? "&date=" + selDate : "") + (force ? "&refresh=1" : "");
      const j = await getJSON(url);
      if (my !== reqToken) return;
      view = j; viewOk = true; loadError = "";
      delay = j.pending > 0 ? POLL_PENDING : (j.view.is_today ? POLL_LIVE : POLL_HISTORY);
    } catch (e) {
      if (my !== reqToken) return;
      if (/^HTTP|Failed to fetch|NetworkError/.test(e.message)) viewOk = false; else loadError = e.message;
    }
    renderAll();
    pollTimer = setTimeout(() => load(false), delay);
  }
  async function loadAttr(name) {
    attrName = name;
    try { attrData = await getJSON("/api/attr?name=" + encodeURIComponent(name)); renderAttrChart(); }
    catch (e) { $("attrNote").textContent = "Could not load this attribute: " + e.message; }
  }
  async function initAttrs() {
    const sel = $("attrSelect");
    try {
      const { names } = await getJSON("/api/attr-names");
      sel.innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
      sel.disabled = !names.length;
      if (names.length) { const def = names.includes("ConnectUsing") ? "ConnectUsing" : names[0]; sel.value = def; loadAttr(def); }
    } catch (e) {
      sel.innerHTML = "<option>Unavailable</option>";
      $("attrNote").textContent = "Attribute list unavailable: " + e.message + ". Retrying…";
      setTimeout(initAttrs, 20000); return;
    }
    sel.addEventListener("change", () => loadAttr(sel.value));
    setInterval(() => { if (attrName) loadAttr(attrName); }, 60000);
  }

  // ---------- communication check ----------
  const SERIAL_OK = /^[A-Za-z0-9_\-]{1,40}$/;
  const ST = { "Communication": "\u25CF ", "Non-communication": "\u25B2 ", "Unknown": "\u25C6 " };
  let ccRows = [], ccPoll = null;
  function ccTokens(t) { return t.split(/[\s,;|]+/).map((x) => x.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean); }
  function ccUpdateCount() {
    const toks = ccTokens($("ccText").value), uniq = new Set(toks.map((x) => x.toUpperCase()));
    $("ccCount").textContent = toks.length ? uniq.size + " unique serial" + (uniq.size === 1 ? "" : "s") : "";
  }
  function ccMsg(html, bad) { const m = $("ccMsg"); m.hidden = !html; m.className = "cc-msg" + (bad ? " bad" : ""); m.innerHTML = html || ""; }
  $("ccText").addEventListener("input", ccUpdateCount);
  $("ccClear").addEventListener("click", () => { $("ccText").value = ""; ccUpdateCount(); ccMsg(""); $("ccResult").hidden = true; });
  $("ccFile").addEventListener("change", (e) => {
    const f = e.target.files[0]; if (!f) return;
    const rd = new FileReader();
    rd.onload = () => { $("ccText").value = String(rd.result); ccUpdateCount(); };
    rd.readAsText(f); e.target.value = "";
  });
  $("ccRun").addEventListener("click", async () => {
    const text = $("ccText").value;
    if (!ccTokens(text).length) { ccMsg("Paste at least one meter serial number.", true); return; }
    $("ccRun").disabled = true; ccMsg(""); $("ccResult").hidden = true;
    $("ccProgress").hidden = false; $("ccBar").style.width = "0%"; $("ccProgressTxt").textContent = "Starting\u2026";
    try {
      const r = await fetch("/api/comm-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ serials: text }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
      ccWatch(j.job_id);
    } catch (e) { ccDone(); ccMsg("Could not start the check: " + esc(e.message), true); }
  });
  function ccDone() { $("ccRun").disabled = false; $("ccProgress").hidden = true; if (ccPoll) { clearTimeout(ccPoll); ccPoll = null; } }
  async function ccWatch(id) {
    try {
      const j = await getJSON("/api/comm-job?id=" + encodeURIComponent(id));
      const pct = j.total ? Math.round((100 * j.done) / j.total) : 0;
      $("ccBar").style.width = pct + "%"; $("ccProgressTxt").textContent = "Checking\u2026 " + pct + "%";
      if (j.status === "running") { ccPoll = setTimeout(() => ccWatch(id), 700); return; }
      ccDone();
      if (j.status === "error") { ccMsg("The check failed: " + esc(j.error), true); return; }
      ccShow(j.result);
    } catch (e) { ccDone(); ccMsg("Lost contact with the dashboard server: " + esc(e.message), true); }
  }
  function ccShow(r) {
    ccLast = r; ccRows = r.rows;
    const notes = [];
    if (r.invalid_count) notes.push(`${r.invalid_count} entr${r.invalid_count === 1 ? "y was" : "ies were"} skipped because they are not valid serials (letters, digits, - and _ only): ${esc(r.invalid.slice(0, 5).join(", "))}${r.invalid_count > 5 ? "\u2026" : ""}`);
    if (r.duplicates) notes.push(`${r.duplicates} duplicate serial${r.duplicates === 1 ? "" : "s"} removed.`);
    const errs = Object.entries(r.errors || {});
    if (errs.length) notes.push("<b>Incomplete result.</b> The query failed for " + errs.map(([c, m]) => esc(c) + " (" + esc(m) + ")").join("; ") + ". Meters not found elsewhere are shown as Unknown, not as non-communication.");
    ccMsg(notes.length ? "<ul>" + notes.map((n) => "<li>" + n + "</li>").join("") + "</ul>" : "", errs.length > 0);
    const checked = r.communication + r.non_communication;
    $("ccKpis").innerHTML =
      `<div class="kpi"><div class="lbl">Meters checked</div><div class="val">${fmt(r.total)}</div><div class="sub2">unique serials</div></div>` +
      `<div class="kpi"><div class="lbl">\u25CF Communication</div><div class="val">${fmt(r.communication)}</div><div class="sub2">found in Last Communication</div></div>` +
      `<div class="kpi"><div class="lbl">\u25B2 Non-communication</div><div class="val">${fmt(r.non_communication)}</div><div class="sub2">not found</div></div>` +
      `<div class="kpi"><div class="lbl">Communication rate</div><div class="val">${r.comm_pct == null ? "\u2014" : r.comm_pct.toFixed(1)}<small>%</small></div><div class="bar"><i style="width:${r.comm_pct || 0}%"></i></div>${r.unknown ? `<div class="flag">\u25B2 incomplete: ${fmt(r.unknown)} meters not checked</div>` : ""}</div>` +
      (r.unknown ? `<div class="kpi"><div class="lbl">\u25C6 Unknown</div><div class="val">${fmt(r.unknown)}</div><div class="sub2">could not be checked</div></div>` : "");
    $("ccResult").hidden = false;
    const p = palette(), labels = Object.keys(r.by_clr);
    ["chCcClr", "chCcAge"].forEach((k) => { if (charts[k]) { charts[k].destroy(); delete charts[k]; } });
    const seg = (name, key, color) => ({ label: name, data: labels.map((l) => r.by_clr[l][key]), backgroundColor: color, borderColor: p.surface, borderWidth: 2, borderRadius: 3, maxBarThickness: 30 });
    const o1 = chartOptions(p, { horizontal: true, tooltip: { label: (c) => `${c.dataset.label}: ${fmt(c.parsed.x)} meters` } });
    o1.scales.x.stacked = true; o1.scales.y.stacked = true;
    charts.chCcClr = new Chart($("chCcClr"), { type: "bar", data: { labels, datasets: [seg("Communication", "communication", p.series[0]), seg("Non-communication", "non_communication", p.series[1])].concat(r.unknown ? [seg("Unknown", "unknown", p.muted)] : []) }, options: o1 });
    const bl = Object.keys(r.age_buckets);
    charts.chCcAge = new Chart($("chCcAge"), { type: "bar", data: { labels: bl, datasets: [{ label: "Found meters", data: bl.map((b) => r.age_buckets[b]), backgroundColor: p.series[0], borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 40 }] },
      options: chartOptions(p, { legend: false, tooltip: { label: (c) => `${fmt(c.parsed.y)} meters` } }) });
    ccTable();
  }
  function ccFiltered() {
    const f = $("ccFilter").value, q = $("ccSearch").value.trim().toUpperCase();
    return ccRows.filter((r) => (f === "all" || r.status === f) && (!q || r.serial.toUpperCase().includes(q)));
  }
  function ccTable() {
    const rows = ccFiltered(), MAX = 500;
    let h = "<thead><tr><th>Serial</th><th>Result</th><th>CLR</th><th>Last communication</th><th>Why</th></tr></thead><tbody>";
    rows.slice(0, MAX).forEach((r) => { h += `<tr><td>${esc(r.serial)}</td><td class="st"><i aria-hidden="true">${ST[r.status].trim()}</i>${esc(r.status)}</td><td>${esc(r.clr || "\u2014")}</td><td>${esc(r.last_comm || "\u2014")}</td><td>${esc(r.reason)}</td></tr>`; });
    $("ccTable").innerHTML = h + "</tbody>";
    $("ccTableNote").textContent = rows.length > MAX ? `Showing the first ${MAX} of ${fmt(rows.length)} rows. The CSV has all of them.` : `${fmt(rows.length)} row${rows.length === 1 ? "" : "s"}.`;
  }
  $("ccFilter").addEventListener("change", ccTable);
  $("ccSearch").addEventListener("input", ccTable);
  $("ccCsv").addEventListener("click", () => {
    const cell = (v) => { let s = v == null ? "" : String(v); if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; return '"' + s.replace(/"/g, '""') + '"'; };
    const lines = [["Serial", "Result", "CLR", "Last communication", "Why"].map(cell).join(",")];
    ccFiltered().forEach((r) => lines.push([r.serial, r.status, r.clr, r.last_comm, r.reason].map(cell).join(",")));
    const blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = "communication-check-" + new Date().toISOString().slice(0, 16).replace(/[:T]/g, "") + ".csv";
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });

  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 12;
  try {
    const q = new URLSearchParams(location.search);
    if (/^\d{4}-\d{2}-\d{2}$/.test(q.get("date") || "") && q.get("date") < todayStr()) selDate = q.get("date");
    if ([7, 14, 30].includes(Number(q.get("days")))) trendDays = Number(q.get("days"));
  } catch (e) {}
  load(false);
  initAttrs();
})();
