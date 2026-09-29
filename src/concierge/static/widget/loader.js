/* WITALCO Guest Concierge widget loader. Inert until the launcher is clicked:
   no cookie, no storage, no request beyond this script until then. ES2022, no build. */
(() => {
  "use strict";
  const script = document.currentScript;
  if (!script || !script.dataset.key) return;
  const key = script.dataset.key;
  const base = new URL(script.src, location.href).origin;
  const lang = (script.dataset.lang || document.documentElement.lang || "de").slice(0, 2).toLowerCase();
  const position = script.dataset.position === "left" ? "left" : "right";
  const color = /^#[0-9a-fA-F]{6}$/.test(script.dataset.color || "") ? script.dataset.color : "#C22113";
  const labels = { de: "Chat öffnen", en: "Open chat", ru: "Открыть чат", uk: "Відкрити чат", tr: "Sohbeti aç", it: "Apri la chat" };
  const host = document.createElement("div");
  host.id = "gc-widget-host";
  host.style.cssText = `position:fixed;bottom:16px;${position}:16px;z-index:2147483000;`;
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("aria-label", labels[lang] || labels.en);
  button.setAttribute("aria-haspopup", "dialog");
  button.style.cssText = `min-width:56px;min-height:56px;border:0;border-radius:28px;background:${color};color:#fff;font:600 15px/1 system-ui,sans-serif;padding:0 20px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.25);`;
  button.textContent = "Chat";
  host.appendChild(button);
  document.body.appendChild(host);
  let loading = false;
  button.addEventListener("click", async () => {
    if (window.GCWidget) { window.GCWidget.open({ key, base, host, launcher: button, lang, position, color }); return; }
    if (loading) return;
    loading = true;
    button.disabled = true;
    try {
      await new Promise((resolve, reject) => {
        const el = document.createElement("script");
        el.src = `${base}/w/gc-widget.js`;
        el.async = true;
        el.onload = resolve;
        el.onerror = () => reject(new Error("widget script failed"));
        document.head.appendChild(el);
      });
      window.GCWidget.open({ key, base, host, launcher: button, lang, position, color });
    } catch (error) {
      button.disabled = false;
      loading = false;
      console.warn("gc-widget: could not load", error);
    }
  });
})();
