/* WITALCO Guest Concierge widget (ES2022, no framework). Opened by loader.js.
   Shadow DOM, keyboard operable, role="log" live region, Esc closes and returns focus,
   persistent AI label, privacy link, phone and e-mail always visible. Session id in
   sessionStorage only; no cookies, no other browser storage. */
(() => {
  "use strict";
  const state = { open: false, cfg: null, session: null, lang: "de", root: null, els: {}, busy: false, opts: null, lastId: "", poll: null };
  const POLL_MS = 6000;

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const linkify = (text) => esc(text).replace(/https?:\/\/[^\s<]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
  const storageKey = () => `gc_session_${state.opts.key}`;
  const labels = () => (state.cfg.labels[state.lang] || state.cfg.labels.en);

  async function api(path, body) {
    const response = await fetch(`${state.opts.base}/w/api/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: state.opts.key, ...body }),
    });
    if (!response.ok) throw new Error(`gc-widget: ${path} ${response.status}`);
    return response.json();
  }

  function build() {
    const { host, position, color } = state.opts;
    const root = host.attachShadow({ mode: "open" });
    state.root = root;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = `${state.opts.base}/w/gc-widget.css`;
    root.appendChild(link);
    const panel = document.createElement("section");
    panel.className = "gc";
    panel.dataset.position = position;
    panel.style.setProperty("--gc-accent", color);
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "false");
    panel.setAttribute("aria-labelledby", "gc-title");
    panel.hidden = true;
    panel.innerHTML = `
      <header class="gc-header">
        <span class="gc-title" id="gc-title"></span>
        <span class="gc-ai" data-role="ai"></span>
        <select class="gc-lang" data-role="lang"></select>
        <button type="button" class="gc-close" data-role="close">&times;</button>
      </header>
      <div class="gc-log" role="log" aria-live="polite" aria-relevant="additions" data-role="log" tabindex="0"></div>
      <div class="gc-typing" data-role="typing" aria-live="polite"></div>
      <div class="gc-quick" data-role="quick"></div>
      <form class="gc-form" data-role="form">
        <label class="gc-visually-hidden" for="gc-input" data-role="input-label"></label>
        <textarea class="gc-input" id="gc-input" rows="1" maxlength="2000" required data-role="input"></textarea>
        <button type="submit" class="gc-send" data-role="send"></button>
      </form>
      <footer class="gc-footer" data-role="footer"></footer>`;
    root.appendChild(panel);
    const q = (role) => panel.querySelector(`[data-role="${role}"]`);
    state.els = { panel, title: panel.querySelector("#gc-title"), ai: q("ai"), lang: q("lang"), close: q("close"), log: q("log"),
      typing: q("typing"), quick: q("quick"), form: q("form"), input: q("input"), inputLabel: q("input-label"), send: q("send"), footer: q("footer") };
    state.els.close.addEventListener("click", close);
    panel.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); close(); } });
    state.els.form.addEventListener("submit", (event) => { event.preventDefault(); send(state.els.input.value, null); });
    state.els.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(state.els.input.value, null); }
    });
    state.els.lang.addEventListener("change", () => { state.lang = state.els.lang.value; applyLabels(); });
  }

  function applyLabels() {
    const l = labels();
    const { els, cfg } = state;
    els.title.textContent = cfg.logo_text || cfg.hotel;
    els.ai.textContent = l.ai;
    els.close.setAttribute("aria-label", l.close);
    els.lang.setAttribute("aria-label", l.language);
    els.inputLabel.textContent = l.input;
    els.input.setAttribute("placeholder", l.input);
    els.send.textContent = l.send;
    els.panel.setAttribute("lang", state.lang);
    const parts = [];
    if (cfg.privacy_url) parts.push(`<a href="${esc(cfg.privacy_url)}" target="_blank" rel="noopener noreferrer">${esc(l.privacy)}</a>`);
    if (cfg.phone) parts.push(`<span>${esc(l.phone)}: <a href="tel:${esc(cfg.phone.replace(/\s+/g, ""))}">${esc(cfg.phone)}</a></span>`);
    if (cfg.email) parts.push(`<span>${esc(l.email)}: <a href="mailto:${esc(cfg.email)}">${esc(cfg.email)}</a></span>`);
    els.footer.innerHTML = parts.join("");
  }

  function fillLanguages() {
    const names = { de: "Deutsch", en: "English", ru: "Русский", uk: "Українська", tr: "Türkçe", it: "Italiano" };
    state.els.lang.innerHTML = state.cfg.languages.map((code) => `<option value="${code}" lang="${code}">${names[code] || code}</option>`).join("");
    state.els.lang.value = state.lang;
    state.els.lang.hidden = !state.cfg.language_menu || state.cfg.languages.length < 2;
  }

  function append(role, text, lang) {
    const el = document.createElement("div");
    el.className = `gc-msg ${role}`;
    if (lang) el.setAttribute("lang", lang);
    el.innerHTML = role === "user" ? esc(text) : linkify(text);
    state.els.log.appendChild(el);
    state.els.log.scrollTop = state.els.log.scrollHeight;
  }

  function showQuick(buttons) {
    const box = state.els.quick;
    box.innerHTML = "";
    for (const button of buttons || []) {
      const el = document.createElement("button");
      el.type = "button";
      el.textContent = button.title;
      el.dataset.id = button.id;
      el.addEventListener("click", () => send(button.title, button.id));
      box.appendChild(el);
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    state.els.send.disabled = busy;
    state.els.input.disabled = busy;
    for (const b of state.els.quick.querySelectorAll("button")) b.disabled = busy;
    state.els.typing.textContent = busy ? "…" : "";
  }

  async function ensureSession() {
    if (state.session) return;
    let stored = null;
    try { stored = sessionStorage.getItem(storageKey()); } catch { stored = null; }
    if (stored) { state.session = stored; return; }
    const data = await api("session", { lang: state.lang });
    state.session = data.session;
    state.lang = data.lang;
    state.lastId = data.last_id || "";
    try { sessionStorage.setItem(storageKey(), data.session); } catch { /* storage may be blocked; the session lives in memory */ }
    for (const m of data.messages) append("bot", m.text, m.lang);
  }

  async function send(text, buttonId) {
    const value = (text || "").trim();
    if (!value || state.busy) return;
    append("user", value, state.lang);
    state.els.input.value = "";
    showQuick([]);
    setBusy(true);
    try {
      await ensureSession();
      const data = await api("message", { session: state.session, text: value, lang: state.lang, button_id: buttonId });
      state.lang = data.lang || state.lang;
      state.els.lang.value = state.lang;
      let quick = [];
      for (const m of data.messages) { append("bot", m.text, m.lang); if (m.buttons && m.buttons.length) quick = m.buttons; }
      if (data.last_id) state.lastId = data.last_id;
      if (data.handoff) { append("system", labels().staff, state.lang); startPolling(); }
      showQuick(quick);
    } catch (error) {
      append("system", labels().error, state.lang);
      if (String(error).includes("404") || String(error).includes("400")) { state.session = null; try { sessionStorage.removeItem(storageKey()); } catch { /* ignore */ } }
    } finally {
      setBusy(false);
      state.els.input.focus();
    }
  }

  // Staff replies arrive while a person handles the conversation; the widget polls
  // the updates endpoint (no push channel, no service worker) and stops when the
  // assistant is back or the panel closes.
  async function poll() {
    if (!state.open || !state.session) { stopPolling(); return; }
    try {
      const params = new URLSearchParams({ key: state.opts.key, session: state.session, after: state.lastId });
      const response = await fetch(`${state.opts.base}/w/api/updates?${params}`);
      if (!response.ok) throw new Error(`gc-widget: updates ${response.status}`);
      const data = await response.json();
      for (const m of data.messages) append(m.role === "staff" ? "staff" : "bot", m.text, m.lang);
      if (data.last_id) state.lastId = data.last_id;
      if (!data.handoff) stopPolling();
    } catch {
      stopPolling();
    }
  }
  function startPolling() { if (!state.poll) state.poll = setInterval(poll, POLL_MS); }
  function stopPolling() { if (state.poll) { clearInterval(state.poll); state.poll = null; } }

  function close() {
    state.open = false;
    stopPolling();
    state.els.panel.hidden = true;
    state.opts.launcher.hidden = false;
    state.opts.launcher.setAttribute("aria-expanded", "false");
    state.opts.launcher.focus();
  }

  async function open(opts) {
    if (!state.root) { state.opts = opts; state.lang = opts.lang; build(); }
    if (!state.cfg) {
      try {
        const response = await fetch(`${opts.base}/w/api/config?key=${encodeURIComponent(opts.key)}`);
        if (!response.ok) throw new Error(`config ${response.status}`);
        state.cfg = await response.json();
      } catch (error) {
        console.warn("gc-widget: config failed", error);
        opts.launcher.disabled = false;
        return;
      }
      if (!state.cfg.languages.includes(state.lang)) state.lang = state.cfg.default_language;
      fillLanguages();
      applyLabels();
    }
    state.open = true;
    state.els.panel.hidden = false;
    opts.launcher.hidden = true;
    opts.launcher.disabled = false;
    opts.launcher.setAttribute("aria-expanded", "true");
    try { await ensureSession(); } catch { append("system", labels().error, state.lang); }
    if (state.lastId) poll();
    state.els.input.focus();
  }

  window.GCWidget = { open, close, version: "2026.09.29" };
})();
