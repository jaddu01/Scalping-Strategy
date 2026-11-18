/* Live UI: WebSocket client, dashboard rendering, dialogs, toggles, kill switches, log stream. */
(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const csrf = () => ($('meta[name="csrf-token"]') || {}).content || "";
  const isAdmin = () => document.body.dataset.admin === "1";
  // admins control everything; a partner controls only what they own (the server checks this too)
  // the admin's "Partner" filter (?owner=all / id) also applies to the live feed and the JSON calls
  const owner = new URLSearchParams(location.search).get("owner");
  const ownerQs = owner ? "owner=" + encodeURIComponent(owner) : "";
  const canControl = (ownerId) => isAdmin() || (document.body.dataset.trader === "1" && ownerId != null && String(ownerId) === document.body.dataset.uid);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2) => (v === null || v === undefined || v === "" || isNaN(v)) ? "–" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pc = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const hms = (iso) => { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" }); };
  const ago = (iso) => { if (!iso) return "never"; const s = Math.round((Date.now() - new Date(iso)) / 1000); return s < 60 ? s + "s ago" : s < 3600 ? Math.round(s / 60) + "m ago" : Math.round(s / 3600) + "h ago"; };
  const mmss = (s) => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m + ":" + String(s % 60).padStart(2, "0"); };

  // loading: the bar at the top of the page shows while any request is running
  let busyCount = 0;
  const busy = (d) => { busyCount = Math.max(0, busyCount + d); document.body.classList.toggle("busy", busyCount > 0); };
  async function withBusy(fn, btn) {
    busy(1); btn?.classList.add("busy");
    try { return await fn(); } finally { busy(-1); btn?.classList.remove("busy"); }
  }
  async function api(path, body) {
    const el = document.activeElement;
    const btn = el && el.tagName === "BUTTON" ? el : null;  // the clicked button shows a spinner too
    return withBusy(async () => {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() }, body: JSON.stringify(body || {}), credentials: "same-origin" });
      let data = {}; try { data = await r.json(); } catch (e) { /* empty */ }
      return { status: r.status, ok: r.ok, data };
    }, btn);
  }
  window.algoApi = api;
  // HTMX requests show the bar too, except background refreshes ("every 3s" polling)
  document.addEventListener("htmx:beforeRequest", (ev) => {
    const trig = ev.detail.elt?.getAttribute("hx-trigger") || "";
    if (/\bevery\b/.test(trig) && ev.detail.requestConfig?.triggeringEvent?.type !== "load") return;
    ev.detail.xhr.algoBusy = true; busy(1);
  });
  document.addEventListener("htmx:afterRequest", (ev) => { if (ev.detail.xhr?.algoBusy) { ev.detail.xhr.algoBusy = false; busy(-1); } });
  // a form that reloads the page: show the bar and a spinner on its button until the next page arrives
  document.addEventListener("submit", (ev) => {
    if (ev.target.hasAttribute("hx-post") || ev.target.hasAttribute("hx-get")) return;
    setTimeout(() => {
      if (ev.defaultPrevented) return;
      const btn = ev.submitter; busy(1); btn?.classList.add("busy");
      setTimeout(() => { busy(-1); btn?.classList.remove("busy"); }, 20000);  // never stuck if no new page comes
    }, 0);
  });
  window.addEventListener("pageshow", () => { busyCount = 0; busy(0); $$("button.busy").forEach((b) => b.classList.remove("busy")); });

  // dashboard day: null = today (live); otherwise the P&L figures come from /api/day for that date
  let viewDay = null, dayData = null;

  // ------------------------------------------------------------------ responsive helpers
  // the menu button (narrow screens)
  const setNav = (open) => {
    document.body.classList.toggle("nav-open", open);
    $(".nav-toggle")?.setAttribute("aria-expanded", open ? "true" : "false");
  };
  document.addEventListener("click", (ev) => {
    if (ev.target.closest(".nav-toggle")) { setNav(!document.body.classList.contains("nav-open")); return; }
    if (document.body.classList.contains("nav-open") && !ev.target.closest(".nav")) setNav(false);
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") setNav(false); });
  // "cards-sm" tables turn into stacked cards on phones: every cell gets its column's name as a label,
  // and the column marked th.c-title / th.c-actions / th.c-wide becomes the card's title / buttons / full row
  function labelTables(root = document) {
    $$("table.cards-sm", root).forEach((tbl) => {
      const ths = $$("thead th", tbl);
      if (!ths.length) return;
      $$("tbody tr", tbl).forEach((tr) => {
        let i = 0;
        Array.from(tr.children).forEach((td) => {
          const th = ths[i];
          i += td.colSpan || 1;
          if (!th || td.colSpan > 1) return;
          td.dataset.label = th.textContent.trim();
          ["c-title", "c-actions", "c-wide"].forEach((c) => { if (th.classList.contains(c)) td.classList.add(c); });
        });
      });
    });
  }
  window.algoLabelTables = labelTables;
  document.addEventListener("DOMContentLoaded", () => labelTables());
  document.addEventListener("htmx:afterSwap", (ev) => labelTables(ev.target));

  // ------------------------------------------------------------------ dialogs (no browser alert/confirm)
  function dialog({ title, body, buttons }) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.innerHTML = `<h3>${esc(title)}</h3><div>${body}</div><div class="actions"></div>`;
      const actions = $(".actions", d);
      buttons.forEach((b) => {
        const btn = document.createElement("button");
        btn.textContent = b.label; if (b.cls) btn.className = b.cls; btn.type = "button";
        btn.addEventListener("click", () => { d.close(); d.remove(); resolve(b.value); });
        actions.appendChild(btn);
      });
      d.addEventListener("cancel", () => { d.remove(); resolve(null); });
      document.body.appendChild(d); d.showModal();
    });
  }
  window.algoDialog = dialog;
  const confirmDialog = (title, body, okLabel = "Confirm", danger = false) =>
    dialog({ title, body, buttons: [{ label: "Cancel", value: false }, { label: okLabel, value: true, cls: danger ? "danger solid" : "primary" }] });

  function toast(text, level) {
    let box = $(".toast"); if (!box) { box = document.createElement("div"); box.className = "toast"; document.body.appendChild(box); }
    const t = document.createElement("div"); t.className = level || ""; t.textContent = text; box.appendChild(t);
    setTimeout(() => t.remove(), 8000);
  }

  // forms with data-confirm="message" ask first
  document.addEventListener("submit", async (ev) => {
    const f = ev.target;
    if (!(f instanceof HTMLFormElement) || !f.dataset.confirm || f.dataset.confirmed === "1") return;
    ev.preventDefault();
    const ok = await confirmDialog(f.dataset.confirmTitle || "Please confirm", esc(f.dataset.confirm), f.dataset.confirmOk || "Confirm", f.dataset.danger === "1");
    if (ok) { f.dataset.confirmed = "1"; f.requestSubmit ? f.requestSubmit() : f.submit(); }
  });

  // strategy form: live mode needs explicit confirmation
  const modeSel = $("#mode-select");
  if (modeSel) {
    const form = modeSel.form, hidden = $("#confirm-live");
    form.addEventListener("submit", async (ev) => {
      if (modeSel.value !== "live" || hidden.value === "yes") return;
      ev.preventDefault();
      const ok = await confirmDialog("Switch to LIVE trading?", "This instance will place <b>real orders with real money</b> through the selected broker account. Make sure the parameters, lots and risk limits are correct.", "Yes, go live", true);
      if (ok) { hidden.value = "yes"; form.requestSubmit ? form.requestSubmit() : form.submit(); }
    });
  }

  // ------------------------------------------------------------------ instance toggles & kill switches
  async function toggleInstance(id, enable, mode, name) {
    if (enable) {
      let body = {};
      if (mode === "live") {
        const ok = await confirmDialog("Enable LIVE instance?", `<b>${esc(name)}</b> will trade with real money.`, "Enable live", true);
        if (!ok) return false; body.confirm_live = true;
      }
      const r = await api(`/api/instances/${id}/enable`, body);
      if (!r.ok) toast(r.data.detail || "enable failed", "ERROR");
      return r.ok;
    }
    let r = await api(`/api/instances/${id}/disable`, {});
    if (r.status === 409 && r.data.needs_choice) {
      const choice = await dialog({ title: `${name} has an open position`, body: "What should happen to the open position?",
        buttons: [{ label: "Cancel", value: null }, { label: "Let the current trade finish, then stop", value: "finish_trade" }, { label: "Exit position now", value: "exit_now", cls: "danger solid" }] });
      if (!choice) return false;
      r = await api(`/api/instances/${id}/disable`, { on_open_position: choice });
    }
    if (!r.ok) toast(r.data.detail || "disable failed", "ERROR");
    return r.ok;
  }

  document.addEventListener("change", async (ev) => {
    const el = ev.target;
    if (!(el instanceof HTMLInputElement) || !el.classList.contains("inst-toggle")) return;
    const want = el.checked; el.disabled = true;
    const ok = await toggleInstance(el.dataset.id, want, el.dataset.mode, el.dataset.name);
    if (!ok) el.checked = !want;
    el.disabled = false;
  });

  document.addEventListener("click", async (ev) => {
    const el = ev.target.closest("[data-kill]");
    if (!el) return;
    ev.preventDefault();
    const scope = el.dataset.kill, id = el.dataset.id, name = el.dataset.name || "";
    const texts = {
      global: ["GLOBAL KILL SWITCH", "Cancel <b>all</b> open orders and exit <b>all</b> positions in <b>every</b> broker account, and disable every strategy instance?"],
      account: [`Kill switch: ${esc(name)}`, "Cancel all open orders and exit all positions in this account (including manual ones), and disable its instances?"],
      instance: [`Kill switch: ${esc(name)}`, "Exit this instance's position at market now and disable it?"],
      reset: ["Reset kill switch", "Allow new entries again?"],
      "reset-account": ["Reset account kill switch", "Allow new entries on this account again?"],
    };
    const [t, b] = texts[scope];
    if (!(await confirmDialog(t, b, scope.startsWith("reset") ? "Reset" : "Kill now", !scope.startsWith("reset")))) return;
    const url = { global: "/api/kill/global", account: `/api/accounts/${id}/kill`, instance: `/api/instances/${id}/kill`, reset: "/api/kill/reset", "reset-account": `/api/accounts/${id}/kill/reset` }[scope];
    el.disabled = true;
    const r = await api(url, {});
    el.disabled = false;
    toast(r.ok ? `${t}: done ${r.data.exited !== undefined ? `(exited ${r.data.exited}, cancelled ${r.data.cancelled}, flattened ${r.data.flattened})` : ""}` : (r.data.detail || "failed"), r.ok ? "" : "ERROR");
  });

  // ------------------------------------------------------------------ dashboard rendering
  let lastState = null, stateAt = 0;
  function renderBanners(s) {
    const box = $("#banners"); if (!box) return;
    box.innerHTML = (s.banners || []).map((b) => `<div class="banner ${esc(b.level)}">${esc(b.text)}</div>`).join("");
  }
  function renderConn(s) {
    const el = $("#conn"); if (!el) return;
    const accs = (s.accounts || []).map((a) => `<span title="${esc(a.label)}: ${esc(a.status)}${a.is_feed ? " (data feed)" : ""}&#10;last tick: ${esc(a.last_tick_at ? ago(a.last_tick_at) : "–")}&#10;order sync: ${esc(ago(a.last_order_sync_at))}"><span class="dot ${esc(a.indicator)}"></span> ${esc(a.label)}${a.is_feed ? " ⦿" : ""}</span>`).join("");
    el.innerHTML = accs + `<span class="muted">${s.market_open ? "market open" : s.trading_day ? "market closed" : "no trading today"} · ${hms(s.now)}</span>`;
  }
  function renderDashboard(s) {
    const root = $("#dashboard"); if (!root) return;
    const past = viewDay !== null;
    const t = past ? (dayData ? dayData.total : {}) : (s.totals || {});
    const instPnl = (i) => past ? ((dayData && dayData.instances[String(i.id)]) || { trades: 0, gross: 0, net: 0 }) : i;
    const accPnl = (a) => past ? ((dayData && dayData.accounts[String(a.id)]) || {}) : (a.pnl || {});
    $("#tot-gross").innerHTML = `<span class="${pc(t.gross)}">${num(t.gross)}</span>`;
    $("#tot-net").innerHTML = `<span class="${pc(t.net)}">${num(t.net)}</span>`;
    $("#tot-trades").textContent = t.trades ?? 0;
    $("#tot-open").textContent = (s.positions || []).length;
    $("#gk-state").innerHTML = s.global_kill ? `<span class="badge red">ACTIVE</span> ${isAdmin() ? '<button class="small" data-kill="reset">Reset</button>' : ""}` : `<span class="badge green">off</span>`;
    // instances
    $("#inst-body").innerHTML = (s.instances || []).map((i) => `<tr>
      <td>${canControl(i.owner_id) ? `<label class="switch"><input type="checkbox" class="inst-toggle" data-id="${i.id}" data-mode="${esc(i.mode)}" data-name="${esc(i.name)}" ${i.enabled ? "checked" : ""}><span></span></label>` : (i.enabled ? "on" : "off")}</td>
      <td><a href="/strategies/${i.id}">${esc(i.name)}</a>${i.pending_settings ? ' <span class="badge amber" title="new parameters apply from the next slot">pending params</span>' : ""}</td>
      <td><span class="badge ${esc(i.status)}">${esc(i.status === "RUNNING" ? "RUNNING" : i.status)}</span> <span class="muted small">${esc(i.message || "")}</span></td>
      <td><span class="badge ${esc(i.mode)}">${esc(i.mode.toUpperCase())}</span></td>
      <td>${esc(i.owner || "-")}</td><td>${esc(i.account)}</td><td class="num">${instPnl(i).trades ?? 0}</td>
      <td class="num ${pc(instPnl(i).gross)}">${num(instPnl(i).gross)}</td><td class="num ${pc(instPnl(i).net)}">${num(instPnl(i).net)}</td>
      <td>${canControl(i.owner_id) && i.enabled ? `<button class="small danger" data-kill="instance" data-id="${i.id}" data-name="${esc(i.name)}">Kill</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="10" class="muted">No strategy instances yet — <a href="/strategies/new">create one</a>.</td></tr>`;
    // positions
    $("#pos-body").innerHTML = (s.positions || []).map((p) => `<tr data-exit="${esc(p.exit_at)}">
      <td><a href="/trades/${esc(p.trade_ref)}">${esc(p.trade_ref)}</a><div class="muted small">${esc(p.instance_name)}</div></td>
      <td>${esc(p.symbol)}<div class="small"><span class="badge ${esc(p.mode)}">${esc(p.mode)}</span> ${esc(p.account_label)}</div></td>
      <td class="num">${p.qty}<div class="muted small">${p.lots} lots</div></td>
      <td class="num">${num(p.entry_price)}</td><td class="num">${num(p.ltp)}</td>
      <td class="num ${pc(p.unrealized)}"><b>${num(p.unrealized)}</b><div class="muted small">net≈ ${num(p.net_estimate)}</div></td>
      <td class="num">${num(p.broker_pnl)}${p.mismatch ? `<div class="mismatch" title="platform vs broker differ">⚠ diff ${num(p.pnl_diff)}</div>` : ""}</td>
      <td class="num">${num(p.spot)}<div class="muted small">SL ${num(p.spot_sl)} (${esc(p.spot_sl_side === "below" ? "≤" : "≥")})</div></td>
      <td class="num ${p.sl_distance !== null && p.sl_distance < 10 ? "neg" : ""}">${num(p.sl_distance)}</td>
      <td class="small">${p.protective_sl_order_id ? `${esc(p.protective_sl_order_id)}<br>trg ${num(p.protective_sl_trigger)} <span class="badge ${esc(p.protective_sl_status)}">${esc(p.protective_sl_status)}</span>` : '<span class="neg">none</span>'}</td>
      <td class="num small">${p.tp_level !== null && p.tp_level !== undefined ? `${num(p.tp_level)} (${esc(p.tp_kind)})<br>dist ${num(p.tp_distance)}` : "–"}${p.runner ? `<div class="muted small">${esc(p.runner)}</div>` : ""}</td>
      <td class="num countdown" data-secs="${p.seconds_to_exit}">${mmss(p.seconds_to_exit)}</td>
      <td><span class="badge ${esc(p.status)}">${esc(p.status)}</span></td></tr>`).join("") || `<tr><td colspan="13" class="muted">No open positions.</td></tr>`;
    const ext = s.external_positions || [];
    $("#ext-wrap").hidden = !ext.length;
    $("#ext-body").innerHTML = ext.map((p) => `<tr><td><span class="badge ext">External / manual</span></td><td>${esc(p.account)}${p.paper ? " (paper)" : ""}</td><td>${esc(p.symbol)}</td><td class="num">${p.qty}</td><td class="num">${num(p.avg)}</td><td class="num">${num(p.ltp)}</td><td class="num ${pc(p.pnl)}">${num(p.pnl)}</td></tr>`).join("");
    $("#acc-body").innerHTML = (s.accounts || []).map((a) => `<tr><td><span class="dot ${esc(a.indicator)}"></span></td>
      <td><a href="/accounts">${esc(a.label)}</a>${a.is_feed ? ' <span class="badge paper">data feed</span>' : ""}${a.kill_switch ? ' <span class="badge red">KILLED</span>' : ""}</td>
      <td>${esc(a.owner || "-")}</td><td>${esc(a.status)}</td><td class="num">${num(a.funds)}</td>
      <td class="num ${pc(accPnl(a).gross)}">${num(accPnl(a).gross)}</td><td class="num ${pc(accPnl(a).net)}">${num(accPnl(a).net)}</td>
      <td class="small">${a.is_feed ? "tick " + esc(ago(a.last_tick_at)) + "<br>" : ""}sync ${esc(ago(a.last_order_sync_at))}</td></tr>`).join("") || `<tr><td colspan="8" class="muted">No broker accounts — <a href="/accounts">add one</a>.</td></tr>`;
  }
  function applyState(s) {
    lastState = s; stateAt = Date.now();
    renderBanners(s); renderConn(s); renderDashboard(s); labelTables($("#dashboard") || document);
    document.dispatchEvent(new CustomEvent("algo:state", { detail: s }));
  }
  // countdowns tick locally between server updates
  setInterval(() => {
    const dt = (Date.now() - stateAt) / 1000;
    $$(".countdown").forEach((el) => { el.textContent = mmss(Number(el.dataset.secs) - dt); });
  }, 1000);

  const initial = $("#initial-state");
  if (initial) { try { const s = JSON.parse(initial.textContent); if (s) applyState(s); } catch (e) { /* ignore */ } }

  // ------------------------------------------------------------------ dashboard day filter
  const dayInput = $("#dash-day");
  if (dayInput) {
    const today = dayInput.dataset.today;
    const fmtDay = (d) => { const [y, m, dd] = d.split("-").map(Number); return new Date(y, m - 1, dd).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }); };
    const shift = (d, n) => {  // the next / previous weekday
      const [y, m, dd] = d.split("-").map(Number); const x = new Date(Date.UTC(y, m - 1, dd));
      do { x.setUTCDate(x.getUTCDate() + n); } while (x.getUTCDay() === 0 || x.getUTCDay() === 6);
      return x.toISOString().slice(0, 10);
    };
    const setLoading = (on) => $$(".day-data").forEach((el) => el.classList.toggle("loading", on));
    function dayUi() {
      const d = viewDay || today, label = viewDay ? fmtDay(viewDay) : null;
      dayInput.value = d;
      $$(".day-label").forEach((el) => { el.textContent = label || "Today's"; });
      $$(".day-label-lc").forEach((el) => { el.textContent = label || "today"; });
      const link = $("#day-trades-link"); if (link) link.href = `/trades?day_from=${d}&day_to=${d}`;
      $("#day-next").disabled = !viewDay; $("#day-today").disabled = !viewDay;
      const note = $("#day-note");
      note.hidden = !viewDay;
      if (viewDay) note.textContent = `Showing P&L and trades for ${label}. Status, on/off switches and open positions are live (now).`;
      const url = new URL(location.href);
      if (viewDay) url.searchParams.set("date", viewDay); else url.searchParams.delete("date");
      history.replaceState(null, "", url);
    }
    async function loadDay(d) {
      if (!d || d >= today) {
        viewDay = null; dayData = null; setLoading(false); dayUi(); if (lastState) applyState(lastState); return;
      }
      viewDay = d; dayData = null; dayUi(); setLoading(true);
      try {
        const r = await withBusy(() => fetch("/api/day?date=" + encodeURIComponent(d) + (ownerQs ? "&" + ownerQs : ""), { credentials: "same-origin" }));
        const j = await r.json().catch(() => ({}));
        if (viewDay !== d) return;  // another day was picked meanwhile
        if (!r.ok) throw new Error(j.detail || r.status);
        dayData = j.summary;
      } catch (e) {
        if (viewDay === d) toast(`Could not load ${fmtDay(d)}: ${e.message}`, "ERROR");
      } finally {
        if (viewDay === d) { setLoading(false); if (lastState) applyState(lastState); }
      }
    }
    dayInput.addEventListener("change", () => loadDay(dayInput.value));
    $("#day-prev").addEventListener("click", () => loadDay(shift(viewDay || today, -1)));
    $("#day-next").addEventListener("click", () => loadDay(shift(viewDay, 1)));
    $("#day-today").addEventListener("click", () => loadDay(null));
    const start = new URLSearchParams(location.search).get("date");
    if (start && start < today) loadDay(start); else dayUi();
  }

  // ------------------------------------------------------------------ log stream
  const logbox = $("#logbox");
  const logFilter = () => {
    const f = {}; $$("[data-logf]").forEach((el) => { if (el.value) f[el.dataset.logf] = el.value; }); return f;
  };
  const levelRank = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
  function matches(e, f) {
    if (f.level && levelRank[e.level] < levelRank[f.level]) return false;
    if (f.category && e.category !== f.category) return false;
    if (f.event_type && e.event_type !== f.event_type.toUpperCase()) return false;
    if (f.instance_id && String(e.instance_id) !== f.instance_id) return false;
    if (f.account_id && String(e.account_id) !== f.account_id) return false;
    if (f.trade_id && e.trade_id !== f.trade_id) return false;
    if (f.q && !(e.message + " " + e.event_type).toLowerCase().includes(f.q.toLowerCase())) return false;
    return true;
  }
  function logLine(e) {
    const div = document.createElement("div");
    div.className = "logline " + e.level;
    const tr = e.trade_id ? ` <a href="/trades/${esc(e.trade_id)}">${esc(e.trade_id)}</a>` : "";
    div.innerHTML = `[${esc(e.time)}] ${esc(e.category.padEnd(8))} ${esc(e.event_type)}${e.instance_name ? " inst=" + esc(e.instance_name) : ""}${tr} ${esc(e.message)}`;
    div.title = JSON.stringify(e.payload || {}, null, 1);
    return div;
  }
  function appendLog(e, prepend) {
    if (!logbox || $("#log-paused")?.checked || !matches(e, logFilter())) return;
    const atBottom = logbox.scrollTop + logbox.clientHeight >= logbox.scrollHeight - 30;
    const el = logLine(e);
    prepend ? logbox.prepend(el) : logbox.appendChild(el);
    while (logbox.children.length > 3000) logbox.firstChild.remove();
    if (atBottom && !prepend) logbox.scrollTop = logbox.scrollHeight;
  }
  async function loadHistory() {
    if (!logbox) return;
    const f = logFilter(); const qs = new URLSearchParams(f); qs.set("limit", "1000"); if (owner) qs.set("owner", owner);
    const df = $("#log-day")?.value; if (df) { qs.set("day_from", df); qs.set("day_to", df); }
    const r = await withBusy(() => fetch("/api/events?" + qs.toString(), { credentials: "same-origin" }));
    if (!r.ok) return;
    const data = await r.json();
    logbox.innerHTML = "";
    data.events.reverse().forEach((e) => appendLog(e));
    logbox.scrollTop = logbox.scrollHeight;
    const ex = $("#log-export-csv"), ej = $("#log-export-json");
    const eqs = new URLSearchParams(f); if (owner) eqs.set("owner", owner); if (df) { eqs.set("day_from", df); eqs.set("day_to", df); }
    if (ex) ex.href = "/api/events/export?fmt=csv&" + eqs.toString();
    if (ej) ej.href = "/api/events/export?fmt=json&" + eqs.toString();
  }
  if (logbox) {
    $$("[data-logf], #log-day").forEach((el) => el.addEventListener("change", loadHistory));
    $("#log-q")?.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); loadHistory(); } });
    loadHistory();
  }

  // ------------------------------------------------------------------ websocket
  let ws, backoff = 1000;
  function connect() {
    if (!document.body.dataset.user) return;
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws" + (ownerQs ? "?" + ownerQs : ""));
    ws.onopen = () => { backoff = 1000; $("#ws-dot")?.classList.replace("red", "green"); };
    ws.onmessage = (m) => {
      let msg; try { msg = JSON.parse(m.data); } catch (e) { return; }
      if (msg.type === "state") applyState(msg.data);
      else if (msg.type === "event") {
        appendLog(msg.data);
        if (levelRank[msg.data.level] >= 40 || ["SIGNAL", "OPENED", "CLOSED", "KILL_SWITCH"].includes(msg.data.event_type)) toast(msg.data.line, msg.data.level);
        document.dispatchEvent(new CustomEvent("algo:event", { detail: msg.data }));
      }
    };
    ws.onclose = (ev) => {
      $("#ws-dot")?.classList.replace("green", "red");
      if (ev.code === 4401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }
      setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 15000);
    };
  }
  connect();

  // inactivity: the server expires idle sessions; reload so the user lands on the login page
  const idleMin = Number(document.body.dataset.idle || 0);
  if (idleMin) {
    let last = Date.now();
    ["click", "keydown", "mousemove", "scroll"].forEach((e) => document.addEventListener(e, () => { last = Date.now(); }, { passive: true }));
    setInterval(() => { if (Date.now() - last > idleMin * 60000) location.href = "/login?next=" + encodeURIComponent(location.pathname); }, 30000);
  }
  // HTMX: send the CSRF header on every request
  document.addEventListener("htmx:configRequest", (ev) => { ev.detail.headers["X-CSRF-Token"] = csrf(); });
})();
