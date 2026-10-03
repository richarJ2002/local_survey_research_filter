// ponytail: one selectable HTML popup and native controls; Plotly's hover labels remain the fallback.
(() => {
  if (window.__graphNodePopupInstalled) return;
  window.__graphNodePopupInstalled = true;
  const HIDE_DELAY = 350;
  let popup, fields, closeButton, opener, hideTimer;
  let pinned = false;
  let hovered = false;
  let position = [100, 100];

  function cancelHide() { clearTimeout(hideTimer); }
  function hide(returnFocus = false) {
    cancelHide();
    pinned = hovered = false;
    if (popup) popup.hidden = true;
    if (returnFocus) opener?.focus();
    opener = null;
  }
  function scheduleHide() {
    if (!popup || popup.hidden || pinned || hovered || popup.contains(document.activeElement)) return;
    cancelHide();
    hideTimer = setTimeout(() => {
      if (!pinned && !hovered && !popup.contains(document.activeElement)) hide();
    }, HIDE_DELAY);
  }
  function createPopup() {
    if (popup) return;
    popup = document.createElement("div");
    popup.id = "graph-node-popup";
    popup.setAttribute("role", "dialog");
    popup.setAttribute("aria-modal", "false");
    popup.setAttribute("aria-labelledby", "graph-node-popup-title");
    popup.hidden = true;
    popup.style.cssText = "position:fixed;z-index:60;box-sizing:border-box;max-width:min(24rem,calc(100vw - 1rem));"
      + "max-height:calc(100vh - 1rem);overflow:auto;user-select:text;overflow-wrap:anywhere;"
      + "background:var(--surface,#fff);color:var(--ink,#1f2937);border:1px solid var(--line,#e1e5e9);"
      + "border-radius:0.6rem;padding:0.8rem;box-shadow:0 8px 28px #0003;";
    const heading = document.createElement("h2");
    heading.id = "graph-node-popup-title";
    heading.textContent = "Paper details";
    heading.style.cssText = "margin:0 0 .5rem;font-size:1rem;";
    const list = document.createElement("dl");
    list.style.cssText = "margin:0;font-size:.85rem;";
    fields = {};
    for (const [key, label] of [["title", "Title"], ["year", "Year"], ["venue", "Venue"], ["citations", "Cited by"], ["ref", "Ref ID"]]) {
      const term = document.createElement("dt");
      term.textContent = label;
      const value = document.createElement("dd");
      value.style.cssText = "margin:0 0 .4rem;user-select:text;";
      list.append(term, value);
      fields[key] = value;
    }
    const hint = document.createElement("p");
    hint.textContent = "Select text to copy. Click a node or this popup to keep it open; Close or Escape dismisses it.";
    hint.style.cssText = "font-size:.75rem;";
    closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.textContent = "Close";
    closeButton.addEventListener("click", () => hide(true));
    popup.append(heading, list, hint, closeButton);
    popup.addEventListener("pointerenter", () => { hovered = true; cancelHide(); });
    popup.addEventListener("pointerleave", () => { hovered = false; scheduleHide(); });
    popup.addEventListener("focusin", cancelHide);
    popup.addEventListener("focusout", scheduleHide);
    popup.addEventListener("pointerdown", () => { pinned = true; cancelHide(); });
    popup.addEventListener("selectstart", () => { pinned = true; cancelHide(); });
    document.body.append(popup);
  }
  function place() {
    const rect = popup.getBoundingClientRect();
    popup.style.left = Math.max(8, Math.min(position[0] + 14, window.innerWidth - rect.width - 8)) + "px";
    popup.style.top = Math.max(8, Math.min(position[1] + 14, window.innerHeight - rect.height - 8)) + "px";
  }
  function show(details, x, y, pin = false, trigger = null) {
    createPopup();
    fields.title.textContent = details.title || "Untitled paper";
    fields.year.textContent = details.year ?? "unknown";
    fields.venue.textContent = details.venue || "unknown";
    fields.citations.textContent = details.citations ?? 0;
    fields.ref.textContent = details.ref ? `[${details.ref}]` : "—";
    pinned = pin;
    opener = trigger;
    cancelHide();
    popup.hidden = false;
    position = [x, y];
    place();
    if (trigger) closeButton.focus();
  }
  function detailsFor(event, plot) {
    const point = event.points?.[0];
    if (!point || !Number.isInteger(point.pointNumber)) return null;
    return (point.data || plot.data?.[point.curveNumber])?.meta?.paper_details?.[point.pointNumber];
  }
  function bind(plot) {
    if (!plot || typeof plot.on !== "function" || plot.dataset.graphNodePopupBound) return;
    const entries = (plot.data || []).flatMap(trace => trace.meta?.paper_details || []);
    if (!entries.length) return;
    plot.dataset.graphNodePopupBound = "true";
    createPopup();
    plot.on("plotly_hover", event => {
      if (pinned || hovered || popup?.contains(document.activeElement)) return;
      const details = detailsFor(event, plot);
      if (details) show(details, event.event?.clientX ?? 100, event.event?.clientY ?? 100);
    });
    plot.on("plotly_unhover", scheduleHide);
    plot.on("plotly_click", event => {
      const details = detailsFor(event, plot);
      if (details) show(details, event.event?.clientX ?? 100, event.event?.clientY ?? 100, true);
    });
    // ponytail: native select/button provides keyboard and touch access to otherwise SVG-only nodes.
    const controls = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Show a graph node's paper details (keyboard or touch)";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Graph node paper");
    entries.forEach((entry, index) => {
      const option = document.createElement("option");
      option.value = index;
      option.textContent = `${entry.title || "Untitled paper"} — ${entry.year ?? "unknown"}${entry.ref ? ` [${entry.ref}]` : ""}`;
      select.append(option);
    });
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Show details";
    button.addEventListener("click", () => {
      const rect = button.getBoundingClientRect();
      show(entries[select.value], rect.left, rect.bottom, true, button);
    });
    controls.append(summary, select, button);
    plot.after(controls);
  }
  const scan = () => document.querySelectorAll(".js-plotly-plot").forEach(bind);
  window.addEventListener("graph-ready", event => bind(document.getElementById(event.detail)));
  scan();
  window.addEventListener("load", scan, { once: true });
  window.addEventListener("resize", () => { if (popup && !popup.hidden) place(); });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && popup && !popup.hidden) hide(true);
  });
})();
