// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
// ponytail: native controls and installed Plotly; no second rasterizer or dependency.
(() => {
  "use strict";
  const states = new WeakMap();
  const initialized = new WeakSet();
  const observed = new WeakSet();
  const scope = window.location.pathname.match(/^(.*\/project\/\d+)(?:\/|$)/)?.[1] || window.location.pathname;
  const scrollKey = "lsrf-chart-scroll:" + scope;
  const presets = {
    access: { fontFamily: "Times New Roman, serif", fontSize: 8, titleFontFamily: "Times New Roman, serif", titleFontSize: 10, widthMm: 180, heightMm: 120, dpi: 300 },
    robotics: { fontFamily: "Times New Roman, serif", fontSize: 8, titleFontFamily: "Times New Roman, serif", titleFontSize: 10, widthMm: 88, heightMm: 100, dpi: 600 },
  };
  const timeoutNote = "Chart action is taking too long. Save any draft and reload this page before retrying; no paper records changed.";

  function stored(kind, key) {
    try { return JSON.parse(window[kind].getItem(key)); } catch (_) { return null; }
  }
  function store(kind, key, value) {
    try { window[kind].setItem(key, JSON.stringify(value)); } catch (_) { /* Browser-local storage may be unavailable. */ }
  }
  function forget(kind, key) {
    try { window[kind].removeItem(key); } catch (_) {}
  }
  function note(element, text) {
    if (element) { element.textContent = text; element.hidden = !text; }
  }
  function sourcePlot(card) {
    return card.querySelector(".js-plotly-plot:not([data-chart-export-clone])");
  }
  function connected(card, state) {
    return card.isConnected && states.get(card) === state && sourcePlot(card) === state.source && card.querySelector("[data-chart-export]") === state.controls;
  }
  function settingsKey(controls) {
    return "lsrf-chart-export:" + scope + ":" + controls.dataset.chartKey;
  }
  function fields(controls) {
    return controls.querySelectorAll("[data-export-setting]");
  }
  function settings(controls) {
    return Object.fromEntries(Array.from(fields(controls), field => [field.dataset.exportSetting, field.value]));
  }
  function titleText(title) {
    return typeof title === "string" ? title : title?.text || "";
  }
  function stateFor(card) {
    const source = sourcePlot(card);
    const controls = card.querySelector("[data-chart-export]");
    let state = states.get(card);
    if (state && (state.source !== source || state.controls !== controls)) {
      state.previewVersion++;
      state.actionVersion++;
      window.clearTimeout(state.timer);
    }
    if (!state || state.source !== source) {
      state = { source, controls, previewVersion: 0, actionVersion: 0, busy: false, blocked: false };
      states.set(card, state);
    }
    state.controls = controls;
    if (source?._fullLayout && controls && !initialized.has(controls)) {
      const saved = stored("localStorage", settingsKey(controls));
      const labels = { title: titleText(source.layout?.title ?? source._fullLayout.title), xTitle: titleText(source.layout?.xaxis?.title ?? source._fullLayout.xaxis?.title), yTitle: titleText(source.layout?.yaxis?.title ?? source._fullLayout.yaxis?.title) };
      fields(controls).forEach(field => {
        const key = field.dataset.exportSetting;
        if (key in labels) field.value = labels[key];
        if (saved && (typeof saved[key] === "string" || typeof saved[key] === "number")) field.value = saved[key];
      });
      if (!["custom", "access", "robotics"].includes(settings(controls).preset)) controls.querySelector('[data-export-setting="preset"]').value = "custom";
      initialized.add(controls);
    }
    if (source?._fullLayout && source.on && !observed.has(source)) {
      observed.add(source);
      const changed = () => {
        if (sourcePlot(card) !== source) return;
        stateFor(card).actionVersion++;
        queuePreview(card);
      };
      source.on("plotly_relayout", changed);
      source.on("plotly_restyle", changed);
    }
    return state;
  }

  function bounded(value, label, minimum, maximum) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < minimum || number > maximum) throw new Error(`${label} must be a finite number between ${minimum} and ${maximum}.`);
    return number;
  }
  function dimensions(values) {
    const widthMm = bounded(values.widthMm, "Width (mm)", 1, 1000);
    const heightMm = bounded(values.heightMm, "Height (mm)", 1, 1000);
    const dpi = bounded(values.dpi, "PNG resolution (DPI)", 1, 2400);
    const widthPx = Math.round(widthMm * dpi / 25.4);
    const heightPx = Math.round(heightMm * dpi / 25.4);
    if (widthPx < 1 || heightPx < 1 || widthPx > 8192 || heightPx > 8192 || widthPx * heightPx > 16000000) throw new Error("PNG must be at least 1 pixel, at most 8192 pixels per side and at most 16 megapixels. Reduce size or DPI.");
    return { widthPx, heightPx, dpi, width: widthMm / 25.4 * 96, height: heightMm / 25.4 * 96 };
  }
  function validate(values) {
    const size = dimensions(values);
    bounded(values.fontSize, "General font size (pt)", 1, 200);
    bounded(values.titleFontSize, "Title font size (pt)", 1, 200);
    for (const key of ["fontFamily", "titleFontFamily"]) {
      if (!values[key]?.trim() || values[key].length > 200) throw new Error("Font families must contain 1–200 characters.");
    }
    for (const key of ["title", "xTitle", "yTitle"]) {
      if (values[key]?.length > 500) throw new Error("Chart and axis titles must be at most 500 characters.");
    }
    return size;
  }
  function overrideFonts(object, font) {
    if (!object || typeof object !== "object" || ArrayBuffer.isView(object)) return;
    for (const [key, value] of Object.entries(object)) {
      // Metadata is research content, not a style tree.
      if (key === "meta" || key === "customdata") continue;
      if (key.endsWith("font") && value && typeof value === "object") Object.assign(value, font);
      else if (value && typeof value === "object") overrideFonts(value, font);
    }
  }
  function snapshot(source, values, axes, size) {
    const copy = structuredClone({ data: source.data || [], layout: source.layout || {} });
    const { data, layout } = copy;
    const font = { family: values.fontFamily, size: Number(values.fontSize) * 96 / 72 };
    overrideFonts(layout, font);
    layout.font = { ...source._fullLayout.font, ...layout.font, ...font };
    for (const trace of data) {
      overrideFonts(trace, font);
      trace.textfont = { ...trace.textfont, ...font };
    }
    for (const annotation of layout.annotations || []) annotation.font = { ...annotation.font, ...font };
    for (const [key, axis] of Object.entries(source._fullLayout)) {
      if (!/^[xy]axis\d*$/.test(key)) continue;
      layout[key] ||= {};
      if (axis.range) { layout[key].range = structuredClone(axis.range); layout[key].autorange = false; }
      layout[key].tickfont = { ...axis.tickfont, ...layout[key].tickfont, ...font };
    }
    if (axes) {
      for (const [key, text] of [["xaxis", values.xTitle], ["yaxis", values.yTitle]]) {
        layout[key] ||= {};
        const title = layout[key].title;
        layout[key].title = { ...(typeof title === "object" ? title : {}), text, font: { ...source._fullLayout[key]?.title?.font, ...(typeof title === "object" ? title?.font : {}), ...font } };
      }
    }
    const title = layout.title;
    layout.title = { ...(typeof title === "object" ? title : {}), text: values.title, font: { ...source._fullLayout.title?.font, ...(typeof title === "object" ? title?.font : {}), family: values.titleFontFamily, size: Number(values.titleFontSize) * 96 / 72 } };
    Object.assign(layout, { autosize: false, width: size.width, height: size.height });
    return copy;
  }

  async function render(source, values, axes, preview, current) {
    const size = validate(values);
    const { data, layout } = snapshot(source, values, axes, size);
    const clone = document.createElement("div");
    clone.dataset.chartExportClone = "true";
    clone.setAttribute("aria-hidden", "true");
    clone.style.cssText = "position:fixed;left:-100000px;top:0;pointer-events:none;";
    document.body.appendChild(clone);
    try {
      await window.Plotly.newPlot(clone, data, layout, { staticPlot: true, displayModeBar: false, responsive: false });
      if (!current()) return null;
      const scale = preview ? Math.min(1, 900 / Math.max(size.width, size.height)) : size.dpi / 96;
      const width = preview ? Math.round(size.width * scale) : size.widthPx;
      const height = preview ? Math.round(size.height * scale) : size.heightPx;
      // Integer raster dimensions, with text/layout still sized at 96 logical DPI.
      const image = await window.Plotly.toImage(clone, { format: "png", width: width / scale, height: height / scale, scale });
      return current() ? image : null;
    } finally {
      try { window.Plotly.purge(clone); } finally { clone.remove(); }
    }
  }

  function pngWithDpi(image, dpi) {
    if (!image.startsWith("data:image/png;base64,")) throw new Error("Plotly did not return a PNG.");
    const bytes = Uint8Array.from(atob(image.split(",")[1]), character => character.charCodeAt(0));
    const signature = [137, 80, 78, 71, 13, 10, 26, 10];
    if (!signature.every((byte, index) => bytes[index] === byte)) throw new Error("Invalid PNG signature.");
    const chunk = new Uint8Array(21);
    const view = new DataView(chunk.buffer);
    view.setUint32(0, 9);
    chunk.set([112, 72, 89, 115], 4); // pHYs: pixels/metre, not pixels/inch.
    view.setUint32(8, Math.round(dpi / 0.0254));
    view.setUint32(12, Math.round(dpi / 0.0254));
    chunk[16] = 1;
    let crc = 0xffffffff;
    for (const byte of chunk.subarray(4, 17)) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
    view.setUint32(17, (crc ^ 0xffffffff) >>> 0);
    const parts = [bytes.subarray(0, 8)];
    const original = new DataView(bytes.buffer);
    let offset = 8;
    let ended = false;
    while (offset < bytes.length) {
      if (offset + 12 > bytes.length) throw new Error("Truncated PNG chunk.");
      const end = offset + 12 + original.getUint32(offset);
      if (end > bytes.length) throw new Error("Truncated PNG data.");
      const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
      if (offset === 8 && (type !== "IHDR" || end !== 33)) throw new Error("Invalid PNG header.");
      if (type !== "pHYs") parts.push(bytes.subarray(offset, end));
      if (type === "IHDR") parts.push(chunk);
      offset = end;
      if (type === "IEND") { ended = true; break; }
    }
    if (!ended || offset !== bytes.length) throw new Error("Invalid PNG end.");
    return new Blob(parts, { type: "image/png" });
  }
  function download(blob, card) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "survey-" + card.id + ".png";
    document.body.appendChild(link);
    try { link.click(); } finally { link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }

  function queuePreview(card) {
    const state = stateFor(card);
    const controls = state.controls;
    if (!controls) return;
    const version = ++state.previewVersion;
    window.clearTimeout(state.timer);
    const status = controls.querySelector("[data-export-status]");
    const image = controls.querySelector("[data-export-preview]");
    image.hidden = true;
    if (state.blocked) { note(status, timeoutNote); return; }
    let values;
    try {
      values = settings(controls);
      const size = validate(values);
      note(controls.querySelector("[data-export-dimensions]"), `${size.widthPx} × ${size.heightPx} pixels at ${size.dpi} DPI`);
    } catch (error) {
      note(controls.querySelector("[data-export-dimensions]"), "No valid PNG dimensions.");
      note(status, error.message);
      return;
    }
    if (!state.source?._fullLayout || !window.Plotly) { note(status, "The chart is still loading. Retry once it is drawn."); return; }
    store("localStorage", settingsKey(controls), values);
    note(status, controls.open ? "Updating low-resolution preview…" : "Open settings to preview. Download PNG renders fresh at the configured resolution.");
    if (!controls.open) return;
    state.timer = window.setTimeout(async () => {
      if (state.previewBusy) { state.previewPending = true; return; }
      const current = () => connected(card, state) && state.previewVersion === version && !state.blocked;
      if (!current()) return;
      state.previewBusy = true;
      const watchdog = window.setTimeout(() => {
        if (!connected(card, state)) return;
        state.blocked = true;
        state.previewVersion++;
        state.actionVersion++;
        note(status, timeoutNote);
      }, 10000);
      try {
        const png = await render(state.source, values, controls.dataset.chartAxes === "true", true, current);
        if (png && current()) {
          image.src = png;
          image.hidden = false;
          note(status, "Low-resolution preview updated. Download PNG renders fresh at the configured resolution.");
        }
      } catch (_) {
        if (current()) note(status, "PNG preview could not finish. Retry by adjusting a setting; no research data changed.");
      } finally {
        window.clearTimeout(watchdog);
        state.previewBusy = false;
        if (state.previewPending && connected(card, state) && !state.blocked) { state.previewPending = false; queuePreview(card); }
      }
    }, 250);
  }

  async function action(button, card) {
    const state = stateFor(card);
    if (state.busy || state.blocked) return;
    const status = card.querySelector("[data-chart-action-status]");
    if (!state.source?._fullLayout || !window.Plotly) { note(status, "The chart is still loading. Retry once it is drawn."); return; }
    let values;
    if (button.dataset.chartAction === "download") {
      try {
        if (!state.controls) throw new Error("PNG export settings are unavailable; reload this page.");
        values = settings(state.controls);
        validate(values);
      } catch (error) {
        note(status, error.message);
        if (state.controls) {
          state.controls.open = true;
          Array.from(fields(state.controls)).find(field => field.validity && !field.validity.valid)?.focus();
        }
        return;
      }
    }
    state.busy = true;
    const version = ++state.actionVersion;
    const current = () => connected(card, state) && state.actionVersion === version && !state.blocked;
    note(status, button.dataset.chartAction === "download" ? "Rendering configured PNG…" : "Resetting view…");
    const watchdog = window.setTimeout(() => {
      if (!connected(card, state)) return;
      state.blocked = true;
      state.actionVersion++;
      note(status, timeoutNote);
    }, 10000);
    try {
      if (button.dataset.chartAction === "download") {
        const png = await render(state.source, values, state.controls.dataset.chartAxes === "true", false, current);
        if (png && current()) {
          download(pngWithDpi(png, Number(values.dpi)), card);
          note(status, "PNG downloaded with configured dimensions and DPI metadata.");
        } else if (connected(card, state) && !state.blocked) note(status, "PNG discarded because the chart or settings changed. Download again for the current view.");
      } else if (button.dataset.chartAction === "reset") {
        const viewport = state.source.layout?.meta?.citation_viewport;
        // Plotly mutates range arrays while navigating; keep the stored fit independent.
        const fit = viewport && Object.fromEntries(["xaxis", "yaxis"].map(axis => [axis + ".range", (viewport[axis].fit || viewport[axis].initial).slice()]));
        await window.Plotly.relayout(state.source, fit || { "xaxis.autorange": true, "yaxis.autorange": true });
        if (connected(card, state) && !state.blocked) note(status, "");
      }
    } catch (_) {
      if (current()) note(status, "Chart action could not finish. Retry or use the full charts/download link; no research data changed.");
    } finally {
      window.clearTimeout(watchdog);
      // After a timeout, late completions are discarded; a reload avoids stacking work.
      if (!state.blocked) state.busy = false;
    }
  }

  // One-shot GET navigation only. POST handle admission remains in project_handle.js.
  let pendingScroll = stored("sessionStorage", scrollKey);
  forget("sessionStorage", scrollKey);
  let loaded = document.readyState === "complete";
  let restoring = false;
  function restoreScroll() {
    if (!pendingScroll || !loaded || restoring) return;
    const navigation = window.performance?.getEntriesByType("navigation")[0];
    if (pendingScroll.destination !== window.location.pathname + window.location.search || pendingScroll.from !== document.referrer || (navigation && navigation.type !== "navigate") || Date.now() - pendingScroll.time > 30000 || !Number.isFinite(pendingScroll.top) || !Number.isFinite(pendingScroll.left)) { pendingScroll = null; return; }
    const card = document.getElementById(pendingScroll.card);
    if (!card || !sourcePlot(card)?._fullLayout) return;
    restoring = true;
    window.requestAnimationFrame(() => {
      const saved = pendingScroll;
      pendingScroll = null;
      const form = card.querySelector(".chart-controls");
      const focus = saved.focusId ? document.getElementById(saved.focusId) : form?.elements[saved.focusIndex];
      if (focus && card.contains(focus)) focus.focus({ preventScroll: true });
      window.scrollTo({ left: saved.left, top: saved.top, behavior: "instant" });
    });
  }
  document.addEventListener("submit", event => {
    const form = event.target;
    if (event.defaultPrevented || !form?.matches("form.chart-controls") || (form.method || "get").toLowerCase() !== "get") return;
    const destination = new URL(form.action || window.location.href, window.location.href);
    if (destination.origin !== window.location.origin) return;
    const query = new URLSearchParams(new FormData(form));
    if (event.submitter?.name) query.append(event.submitter.name, event.submitter.value);
    destination.search = query.toString();
    const card = form.closest(".chart-card");
    if (!card) return;
    const focus = document.activeElement;
    store("sessionStorage", scrollKey, { destination: destination.pathname + destination.search, from: window.location.href, time: Date.now(), card: card.id, top: window.scrollY, left: window.scrollX, focusId: focus?.id || "", focusIndex: Array.from(form.elements).indexOf(focus) });
    window.setTimeout(() => { if (event.defaultPrevented) forget("sessionStorage", scrollKey); }, 0);
  });
  document.addEventListener("click", event => {
    if (event.target.closest?.("a[href]")) forget("sessionStorage", scrollKey);
    const button = event.target.closest?.("button[data-chart-action]");
    const card = button?.closest(".chart-card");
    if (card) return action(button, card);
  });
  const edit = event => {
    const field = event.target;
    const controls = field.closest?.("[data-chart-export]");
    if (!controls || !field.dataset.exportSetting) return;
    // A newly inserted or still-loading panel must not overwrite its first edit.
    initialized.add(controls);
    const preset = controls.querySelector('[data-export-setting="preset"]');
    if (field === preset) fields(controls).forEach(input => { if (input.dataset.exportSetting in (presets[field.value] || {})) input.value = presets[field.value][input.dataset.exportSetting]; });
    else preset.value = "custom";
    const card = controls.closest(".chart-card");
    stateFor(card).actionVersion++;
    queuePreview(card);
  };
  document.addEventListener("input", edit);
  document.addEventListener("change", edit);
  document.addEventListener("toggle", event => {
    if (event.target.matches?.("[data-chart-export]")) queuePreview(event.target.closest(".chart-card"));
  }, true);
  const ready = event => {
    const card = document.getElementById(event.detail)?.closest(".chart-card");
    if (card) queuePreview(card);
    restoreScroll();
  };
  window.addEventListener("graph-ready", ready);
  window.addEventListener("survey-chart-ready", ready);
  window.addEventListener("load", () => {
    loaded = true;
    document.querySelectorAll(".chart-card").forEach(queuePreview);
    restoreScroll();
  });
  if (loaded) { document.querySelectorAll(".chart-card").forEach(queuePreview); restoreScroll(); }
})();
