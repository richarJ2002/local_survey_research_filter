// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");
const test = require("node:test");

const script = readFileSync(join(__dirname, "../static/literature/chart_navigation.js"), "utf8");
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
const plain = value => JSON.parse(JSON.stringify(value));
const storage = () => ({ values: new Map(), getItem(key) { return this.values.get(key) || null; }, setItem(key, value) { this.values.set(key, value); }, removeItem(key) { this.values.delete(key); } });
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };

// Small DOM/Plotly boundary double; no npm, server, or real project data.
function fixture(options = {}) {
  const documentListeners = {}, windowListeners = {}, timers = new Map(), frames = [];
  const calls = { plots: [], images: [], resets: [], purged: [], downloads: [], blobs: [], scrolls: [], revoked: [] };
  const status = { hidden: true, textContent: "" }, previewStatus = { hidden: false }, image = { hidden: true }, output = {};
  const card = { id: options.id || "chart-yearly", isConnected: true, querySelector(selector) {
    if (selector.startsWith(".js-plotly-plot")) return this.plot;
    if (selector === "[data-chart-export]") return this.controls;
    if (selector === ".chart-controls") return form;
    if (selector === "[data-chart-action-status]") return status;
    return null;
  }, contains(element) { return element === form || form.elements.includes(element); } };
  const controls = { open: false, dataset: { chartKey: card.id, chartAxes: options.axes === false ? "false" : "true" }, closest() { return card; }, matches(selector) { return selector === "[data-chart-export]"; }, querySelectorAll() { return Object.values(inputs); }, querySelector(selector) {
    if (selector === "[data-export-status]") return previewStatus;
    if (selector === "[data-export-preview]") return image;
    if (selector === "[data-export-dimensions]") return output;
    return inputs[selector.match(/data-export-setting="([^"]+)"/)?.[1]] || null;
  } };
  const defaults = { preset: "custom", title: "Fallback heading", fontFamily: "Arial, sans-serif", fontSize: "10", titleFontFamily: "Arial, sans-serif", titleFontSize: "12", widthMm: "180", heightMm: "120", dpi: "300" };
  if (options.axes !== false) Object.assign(defaults, { xTitle: "", yTitle: "" });
  const inputs = Object.fromEntries(Object.entries(defaults).map(([key, value]) => [key, { value, id: `${card.id}-${key}`, dataset: { exportSetting: key }, closest(selector) { return selector === "[data-chart-export]" ? controls : card; } }]));
  const plot = { id: "plot-id", data: [{ type: "scatter", x: [0, 10], y: [1, 2], ids: ["node-a", "node-b"], visible: "legendonly", marker: { color: "#3f7e3f" }, textfont: { color: "white", size: 10 }, customdata: [{ font: { size: 99 } }], meta: { paper_details: [{ title: "Stored" }], font: { size: 99 } } }], layout: { title: "Original title", xaxis: { title: "Year" }, yaxis: { title: { text: "Count" } }, annotations: [{ text: "Empty-state / footer", font: { color: "#555555", size: 11 } }], meta: { edges: [["node-a", "node-b"]], font: { size: 99 } } }, _fullLayout: { xaxis: { range: [2, 8] }, yaxis: { range: [3, 15] }, title: { font: { color: "red" } }, font: { color: "#1f2937" } }, handlers: {}, on(name, listener) { this.handlers[name] = listener; }, closest() { return card; } };
  card.plot = plot;
  card.controls = controls;
  const location = new URL(options.href || "http://localhost/p/uuid/epoch/project/1/");
  const form = { method: "get", action: location.href, entries: [["year_from", "2021"], ["prune", "on"]], matches(selector) { return selector === "form.chart-controls"; }, closest() { return card; }, elements: [{ id: "yearly-year_from", name: "year_from", focus(value) { this.focused = value; } }, { focus(value) { this.focused = value; } }] };
  let nextTimer = 0;
  class BrowserURL extends URL {
    static createObjectURL(blob) { calls.blobs.push(blob); return "blob:synthetic"; }
    static revokeObjectURL(url) { calls.revoked.push(url); }
  }
  class FormData {
    constructor(target) { this.entries = target.entries; }
    [Symbol.iterator]() { return this.entries[Symbol.iterator](); }
  }
  const document = { readyState: "interactive", referrer: options.referrer || "", activeElement: form.elements[0], body: { appendChild() {} }, addEventListener(name, listener) { (documentListeners[name] ||= []).push(listener); }, querySelectorAll() { return card.isConnected ? [card] : []; }, getElementById(id) { return [card, card.plot, ...form.elements].find(element => element?.id === id) || null; }, createElement(tag) { return { dataset: {}, style: {}, setAttribute() {}, remove() { this.removed = true; }, click() { calls.downloads.push(this.download); } }; } };
  const window = { location, localStorage: options.localStorage || storage(), sessionStorage: options.sessionStorage || storage(), performance: { getEntriesByType() { return [{ type: options.navigation || "navigate" }]; } }, scrollY: 900, scrollX: 30, addEventListener(name, listener) { (windowListeners[name] ||= []).push(listener); }, setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; }, clearTimeout(id) { timers.delete(id); }, requestAnimationFrame(callback) { frames.push(callback); }, scrollTo(value) { calls.scrolls.push(value); }, Plotly: {
    newPlot(clone, data, layout, config) { calls.plots.push({ clone, data, layout, config }); },
    toImage(clone, config) { calls.images.push({ clone, config }); return png; },
    purge(clone) { calls.purged.push(clone); },
    relayout(source, values) { calls.resets.push({ source, values }); },
  } };
  runInNewContext(script, { document, window, URL: BrowserURL, URLSearchParams, FormData, Blob, atob, structuredClone, ArrayBuffer });
  const dispatch = async (name, target, extra = {}) => {
    const event = { target, defaultPrevented: false, ...extra };
    for (const listener of documentListeners[name] || []) await listener(event);
    return event;
  };
  const ready = (name = "graph-ready") => (windowListeners[name] || []).forEach(listener => listener({ detail: card.plot?.id }));
  return { card, controls, inputs, plot, window, document, form, calls, status, previewStatus, image, output, ready,
    click(action) { return dispatch("click", { dataset: { chartAction: action }, closest(selector) { return selector === "button[data-chart-action]" ? this : card; } }); },
    edit(key, value) { inputs[key].value = String(value); return dispatch("input", inputs[key]); },
    dispatch,
    load() { (windowListeners.load || []).forEach(listener => listener()); },
    flushFrames() { frames.splice(0).forEach(callback => callback()); },
    async flush(delay) { const selected = Array.from(timers).filter(([, timer]) => timer.delay === delay); for (const [id] of selected) timers.delete(id); await Promise.all(selected.map(([, timer]) => timer.callback())); },
  };
}

test("download and reset retain source data/layout and handle failures/loading", async () => {
  const env = fixture();
  const initial = JSON.stringify(env.plot);
  await env.click("reset");
  assert.equal(env.calls.resets.at(-1).values["xaxis.autorange"], true);
  assert.equal(env.calls.resets.at(-1).values["yaxis.autorange"], true);
  await env.click("download");
  assert.equal(env.calls.images.at(-1).config.format, "png");
  assert.deepEqual(env.calls.downloads, ["survey-chart-yearly.png"]);
  assert.equal(JSON.stringify(env.plot), initial);
  env.window.Plotly.toImage = () => Promise.reject(new Error("synthetic"));
  await env.click("download");
  assert.equal(env.status.hidden, false);
  assert.match(env.status.textContent, /could not finish/);
  env.plot._fullLayout = null;
  await env.click("download");
  assert.match(env.status.textContent, /still loading/);
  assert.ok(env.calls.purged.every(clone => clone.removed));
});

test("citation Reset fits the whole graph using independent arrays", async () => {
  const env = fixture({ axes: false, id: "chart-citation" });
  const fit = { xaxis: [-1, 5], yaxis: [-2, 4] };
  env.plot.layout.meta.citation_viewport = Object.fromEntries(["xaxis", "yaxis"].map(axis => [axis, { initial: [1, 3], fit: fit[axis] }]));
  await env.click("reset");
  const update = env.calls.resets.at(-1).values;
  assert.deepEqual(Array.from(update["xaxis.range"]), fit.xaxis);
  assert.deepEqual(Array.from(update["yaxis.range"]), fit.yaxis);
  assert.notEqual(update["xaxis.range"], fit.xaxis);
  update["xaxis.range"][0] = 100;
  update["yaxis.range"][1] = 200;
  await env.click("reset");
  assert.deepEqual(Array.from(env.calls.resets.at(-1).values["xaxis.range"]), fit.xaxis);
  assert.deepEqual(Array.from(env.calls.resets.at(-1).values["yaxis.range"]), fit.yaxis);
});

test("overlapping reset clicks never queue another Plotly redraw", async () => {
  const env = fixture();
  let resolve, count = 0;
  env.window.Plotly.relayout = () => { count++; return new Promise(done => { resolve = done; }); };
  const first = env.click("reset");
  await env.click("reset");
  assert.equal(count, 1);
  resolve();
  await first;
  const next = env.click("reset");
  assert.equal(count, 2);
  resolve();
  await next;
});

test("watchdogs discard late reset/download outcomes and require reload", async () => {
  for (const action of ["reset", "download"]) for (const outcome of ["resolve", "reject"]) {
    const env = fixture();
    let finish, fail, count = 0;
    env.window.Plotly[action === "reset" ? "relayout" : "toImage"] = () => { count++; return new Promise((resolve, reject) => { finish = resolve; fail = reject; }); };
    const first = env.click(action);
    await tick();
    await env.flush(10000);
    assert.match(env.status.textContent, /reload this page/);
    await env.click(action);
    assert.equal(count, 1);
    if (outcome === "reject") fail(new Error("late")); else finish(png);
    await first;
    await env.click(action);
    assert.equal(count, 1);
    assert.equal(env.calls.downloads.length, 0);
    assert.match(env.status.textContent, /reload this page/);
  }
});

test("one copied renderer preserves traces, ranges, metadata and colours while styling pt fonts", async () => {
  const env = fixture();
  env.plot.layout.title = { text: "Dict title", font: { color: "red", size: 22 }, x: 0.2 };
  env.controls.open = true;
  env.load();
  assert.equal(env.inputs.title.value, "Dict title");
  assert.equal(env.inputs.xTitle.value, "Year");
  await env.edit("fontFamily", "Times New Roman");
  await env.edit("fontSize", 8);
  await env.edit("titleFontFamily", "Georgia");
  await env.edit("titleFontSize", 10);
  await env.edit("title", "Edited title");
  await env.edit("xTitle", "Edited X");
  const initial = JSON.stringify(env.plot);
  await env.flush(250);
  const rendered = env.calls.plots.at(-1);
  assert.notEqual(rendered.data[0], env.plot.data[0]);
  assert.equal(rendered.layout.font.size, 8 * 96 / 72);
  assert.equal(rendered.layout.title.font.size, 10 * 96 / 72);
  assert.equal(rendered.layout.title.font.family, "Georgia");
  assert.equal(rendered.layout.title.font.color, "red");
  assert.equal(rendered.layout.title.text, "Edited title");
  assert.equal(rendered.layout.xaxis.title.text, "Edited X");
  assert.deepEqual(plain(rendered.layout.xaxis.range), [2, 8]);
  assert.deepEqual(plain(rendered.layout.yaxis.range), [3, 15]);
  assert.equal(rendered.layout.xaxis.autorange, false);
  assert.equal(rendered.data[0].textfont.color, "white");
  assert.equal(rendered.data[0].textfont.size, 8 * 96 / 72);
  assert.equal(rendered.layout.annotations[0].font.color, "#555555");
  assert.equal(rendered.layout.annotations[0].font.size, 8 * 96 / 72);
  assert.deepEqual(plain(rendered.data[0].meta), env.plot.data[0].meta);
  assert.deepEqual(plain(rendered.data[0].customdata), env.plot.data[0].customdata);
  assert.deepEqual(plain(rendered.layout.meta), env.plot.layout.meta);
  assert.equal(rendered.data[0].visible, "legendonly");
  assert.equal(rendered.data[0].marker.color, "#3f7e3f");
  assert.deepEqual(plain(rendered.data[0].ids), env.plot.data[0].ids);
  assert.equal(rendered.clone.dataset.chartExportClone, "true");
  assert.equal(env.image.hidden, false);
  assert.ok(env.calls.images.at(-1).config.scale <= 1);
  rendered.data[0].x[0] = 999;
  rendered.layout.meta.edges[0][0] = "changed-clone";
  await env.click("download");
  assert.equal(env.calls.plots.length, 2, "download renders a fresh copy, never the preview bitmap");
  assert.equal(env.calls.images.at(-1).config.scale, 300 / 96);
  assert.equal(JSON.stringify(env.plot), initial);
});

test("axisless flow/citation/curated exports leave axis titles absent or unchanged", async () => {
  for (const id of ["chart-flow", "chart-citation", "curated-survey-plot"]) {
    const env = fixture({ id, axes: false });
    env.plot.layout = { title: "String title", meta: { edges: [[1, 2], [2, 1]] } };
    if (id !== "chart-flow") Object.assign(env.plot.layout, { xaxis: { visible: false, title: "Hidden X" }, yaxis: { visible: false } });
    else env.plot._fullLayout = { title: { font: {} }, font: {} };
    await env.click("download");
    const layout = env.calls.plots[0].layout;
    assert.equal(layout.xaxis?.title, id === "chart-flow" ? undefined : "Hidden X");
    assert.equal(layout.yaxis?.title, undefined);
    assert.deepEqual(plain(layout.meta), env.plot.layout.meta);
  }
});

test("mm/DPI dimensions are exposed exactly and invalid/extreme values never render", async () => {
  const env = fixture();
  env.load();
  for (const [key, value] of [["widthMm", 0], ["widthMm", -1], ["widthMm", Infinity], ["widthMm", "NaN"], ["heightMm", ""], ["dpi", 0], ["dpi", 2401], ["fontSize", 0], ["titleFontSize", Infinity], ["fontFamily", " "], ["titleFontFamily", "x".repeat(201)]]) {
    const previous = env.inputs[key].value;
    await env.edit(key, value);
    await env.click("download");
    assert.equal(env.calls.plots.length, 0, `${key}=${value}`);
    assert.match(env.previewStatus.textContent, /must/);
    await env.edit(key, previous);
  }
  for (const [width, height, dpi] of [[1000, 1, 300], [200, 200, 600], [1, 1, 1]]) {
    await env.edit("widthMm", width); await env.edit("heightMm", height); await env.edit("dpi", dpi);
    await env.click("download");
    assert.equal(env.calls.plots.length, 0);
    assert.match(env.status.textContent, /8192.*16 megapixels/);
  }
  await env.edit("widthMm", 25.4); await env.edit("heightMm", 12.7); await env.edit("dpi", 600);
  await env.click("download");
  assert.equal(env.output.textContent, "600 × 300 pixels at 600 DPI");
  assert.equal(env.calls.plots[0].layout.width, 96);
  assert.equal(env.calls.plots[0].layout.height, 48);
  assert.deepEqual(plain(env.calls.images[0].config), { format: "png", width: 96, height: 48, scale: 6.25 });
  await env.edit("widthMm", 8192 * 25.4 / 300); await env.edit("heightMm", 10); await env.edit("dpi", 300);
  await env.click("download");
  assert.match(env.output.textContent, /^8192 ×/);
  const count = env.calls.plots.length;
  await env.edit("widthMm", 8193 * 25.4 / 300);
  await env.click("download");
  assert.equal(env.calls.plots.length, count);
});

function pngChunks(bytes) {
  const chunks = [];
  for (let offset = 8; offset < bytes.length;) {
    const end = offset + 12 + bytes.readUInt32BE(offset);
    chunks.push({ type: bytes.toString("ascii", offset + 4, offset + 8), bytes: bytes.subarray(offset, end) });
    offset = end;
  }
  return chunks;
}
test("PNG Blob adds one correctly CRC32-checked pHYs chunk without rerasterizing", async () => {
  const env = fixture();
  await env.click("download");
  const blob = env.calls.blobs[0];
  assert.equal(blob.type, "image/png");
  const chunks = pngChunks(Buffer.from(await blob.arrayBuffer()));
  const phys = chunks.find(chunk => chunk.type === "pHYs").bytes;
  assert.equal(chunks[1].type, "pHYs");
  assert.equal(phys.readUInt32BE(8), Math.round(300 / 0.0254));
  assert.equal(phys.readUInt32BE(12), Math.round(300 / 0.0254));
  assert.equal(phys[16], 1);
  // Independent lookup-table implementation of the PNG CRC, including chunk type.
  const table = Array.from({ length: 256 }, (_, value) => { for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1; return value >>> 0; });
  const crc = (phys.subarray(4, 17).reduce((value, byte) => table[(value ^ byte) & 255] ^ (value >>> 8), 0xffffffff) ^ 0xffffffff) >>> 0;
  assert.equal(phys.readUInt32BE(17), crc);
  const original = Buffer.from(png.split(",")[1], "base64");
  assert.deepEqual(chunks.filter(chunk => chunk.type !== "pHYs").map(chunk => chunk.bytes), pngChunks(original).map(chunk => chunk.bytes));
  env.window.Plotly.toImage = () => "data:image/png;base64," + Buffer.concat([original.subarray(0, 33), phys, original.subarray(33)]).toString("base64");
  await env.edit("dpi", 600);
  await env.click("download");
  const replaced = pngChunks(Buffer.from(await env.calls.blobs[1].arrayBuffer())).filter(chunk => chunk.type === "pHYs");
  assert.equal(replaced.length, 1);
  assert.equal(replaced[0].bytes.readUInt32BE(8), Math.round(600 / 0.0254));
  assert.equal(env.calls.images.length, 1, "the helper edits PNG bytes; it never calls toImage again");
});

test("empty charts, renderer errors and malformed PNGs clean up and remain retryable", async () => {
  const env = fixture();
  env.plot.data = [];
  env.controls.open = true;
  env.window.Plotly.newPlot = clone => { env.calls.plots.push({ clone }); throw new Error("synthetic"); };
  env.load();
  await env.flush(250);
  assert.match(env.previewStatus.textContent, /could not finish/);
  assert.equal(env.calls.purged.length, 1);
  assert.equal(env.calls.purged[0].removed, true);
  env.window.Plotly.newPlot = (clone, data, layout) => env.calls.plots.push({ clone, data, layout });
  for (const invalid of ["not a PNG", "data:image/png;base64,AAAA", png.slice(0, -12)]) {
    env.window.Plotly.toImage = () => invalid;
    await env.click("download");
    assert.match(env.status.textContent, /could not finish/);
    assert.equal(env.calls.blobs.length, 0);
  }
  env.window.Plotly.toImage = () => png;
  await env.click("download");
  assert.equal(env.calls.downloads.length, 1);
  assert.deepEqual(plain(env.calls.plots.at(-1).data), []);
  assert.equal(env.calls.plots.at(-1).layout.annotations[0].text, "Empty-state / footer");
});

test("stale previews coalesce edits and discard old/replaced/disconnected sources", async () => {
  const env = fixture();
  let finish;
  env.window.Plotly.toImage = () => new Promise(resolve => { finish = resolve; });
  env.controls.open = true;
  env.load();
  const first = env.flush(250);
  await tick();
  await env.edit("title", "New settings");
  await env.flush(250);
  finish("old-preview");
  await first;
  assert.equal(env.image.hidden, true);
  env.window.Plotly.toImage = () => "new-preview";
  await env.flush(250);
  assert.equal(env.image.src, "new-preview");
  assert.equal(env.calls.plots.at(-1).layout.title.text, "New settings");
  env.window.Plotly.toImage = () => new Promise(resolve => { finish = resolve; });
  await env.edit("title", "Later");
  const pending = env.flush(250);
  await tick();
  env.card.plot = { ...env.plot, id: "replacement-plot" };
  env.ready();
  env.window.Plotly.toImage = () => "replacement-preview";
  await env.flush(250);
  finish("obsolete-preview");
  await pending;
  assert.equal(env.image.src, "replacement-preview");
  env.window.Plotly.toImage = () => new Promise(resolve => { finish = resolve; });
  await env.edit("title", "Disconnected");
  const detached = env.flush(250);
  await tick();
  env.card.isConnected = false;
  finish("detached-preview");
  await detached;
  assert.notEqual(env.image.src, "detached-preview");
  assert.ok(env.calls.purged.every(clone => clone.removed));
});

test("late downloads are discarded after edits, source replacement or disconnect", async () => {
  for (const change of ["edit", "replace", "disconnect"]) {
    const env = fixture();
    let finish;
    env.window.Plotly.toImage = () => new Promise(resolve => { finish = resolve; });
    const pending = env.click("download");
    await tick();
    if (change === "edit") await env.edit("title", "New title");
    else if (change === "replace") { env.card.plot = { ...env.plot, id: "replacement" }; env.ready(); }
    else env.card.isConnected = false;
    finish(png);
    await pending;
    assert.equal(env.calls.blobs.length, 0);
    assert.equal(env.calls.downloads.length, 0);
    assert.equal(env.calls.purged[0].removed, true);
  }
});

test("delegated presets remain editable, settings are worker/project/chart scoped, storage is optional", async () => {
  const env = fixture();
  env.load();
  await env.edit("preset", "robotics");
  assert.equal(env.inputs.fontSize.value, 8);
  assert.equal(env.inputs.widthMm.value, 88);
  assert.equal(env.inputs.dpi.value, 600);
  await env.edit("title", "Local custom title");
  assert.equal(env.inputs.preset.value, "custom");
  const options = { localStorage: env.window.localStorage, href: env.window.location.href + "plots/" };
  const restored = fixture(options); restored.load();
  assert.equal(restored.inputs.title.value, "Local custom title");
  assert.equal(restored.inputs.widthMm.value, 88);
  for (const other of [{ ...options, id: "chart-source" }, { ...options, href: "http://localhost/p/uuid/epoch/project/2/" }, { ...options, href: "http://localhost/p/other/epoch/project/1/" }]) {
    const separate = fixture(other); separate.load();
    assert.equal(separate.inputs.title.value, "Original title");
  }
  const unavailable = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
  const privateMode = fixture({ localStorage: unavailable, sessionStorage: unavailable });
  privateMode.load();
  await privateMode.click("download");
  assert.equal(privateMode.calls.downloads.length, 1);
});

test("chart GET restores one-shot exact-destination scroll/focus after load and ready, never POST admission", async () => {
  const env = fixture();
  await env.dispatch("submit", env.form);
  const session = env.window.sessionStorage;
  const saved = JSON.parse(Array.from(session.values.values())[0]);
  assert.equal(saved.destination, "/p/uuid/epoch/project/1/?year_from=2021&prune=on");
  const next = fixture({ sessionStorage: session, href: env.window.location.origin + saved.destination, referrer: env.window.location.href });
  const fullLayout = next.plot._fullLayout;
  next.plot._fullLayout = null;
  next.load(); next.flushFrames();
  assert.equal(next.calls.scrolls.length, 0);
  next.plot._fullLayout = fullLayout;
  next.ready("survey-chart-ready"); next.flushFrames();
  assert.deepEqual(plain(next.calls.scrolls), [{ left: 30, top: 900, behavior: "instant" }]);
  assert.deepEqual(plain(next.form.elements[0].focused), { preventScroll: true });
  next.ready(); next.flushFrames();
  assert.equal(next.calls.scrolls.length, 1);
  assert.equal(session.values.size, 0);
  const manual = fixture({ sessionStorage: session, href: next.window.location.href, referrer: env.window.location.href });
  manual.load(); manual.flushFrames();
  assert.equal(manual.calls.scrolls.length, 0);
  env.form.method = "post";
  await env.dispatch("submit", env.form);
  assert.equal(session.values.size, 0);
  assert.equal(env.form.elements.length, 2, "the export controller never inserts/removes a project handle");
});

test("mismatched/manual/reload/back navigation, expired and prevented GETs never jump", async () => {
  const first = fixture();
  await first.dispatch("submit", first.form);
  const [key, raw] = Array.from(first.window.sessionStorage.values)[0];
  const saved = JSON.parse(raw);
  for (const change of [{ href: "http://localhost/p/uuid/epoch/project/1/?prune=on&year_from=2021" }, { referrer: "" }, { navigation: "reload" }, { navigation: "back_forward" }, { time: Date.now() - 31000 }]) {
    const session = storage(); session.setItem(key, JSON.stringify({ ...saved, ...("time" in change ? { time: change.time } : {}) }));
    const env = fixture({ sessionStorage: session, href: first.window.location.origin + saved.destination, referrer: first.window.location.href, ...change });
    env.load(); env.flushFrames();
    assert.equal(env.calls.scrolls.length, 0);
    assert.equal(session.values.size, 0);
  }
  const env = fixture();
  await env.dispatch("submit", env.form, { defaultPrevented: true });
  assert.equal(env.window.sessionStorage.values.size, 0);
  const cancelled = await env.dispatch("submit", env.form);
  cancelled.defaultPrevented = true;
  await env.flush(0);
  assert.equal(env.window.sessionStorage.values.size, 0);
});

test("project-only 44px/focus/hidden rules and actual CSS color contrast", () => {
  const base = readFileSync(join(__dirname, "../templates/literature/base.html"), "utf8");
  const detail = readFileSync(join(__dirname, "../templates/literature/project_detail.html"), "utf8");
  const scoped = base.split("/* WP02 project-only targets:")[1].split("/* End WP02 project-only targets. */")[0];
  let selectors = 0;
  for (const rule of scoped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (rule[1].trim().startsWith("@")) continue;
    for (const selector of rule[1].split(",")) {
      assert.match(selector, /body\.project-detail/);
      selectors++;
    }
  }
  assert.ok(selectors >= 25);
  assert.match(detail, /block body_class %}project-detail/);
  assert.match(scoped, /min-width: 44px; min-height: 44px/);
  assert.match(scoped, /outline: 3px solid var\(--wp-focus\)/);
  assert.match(scoped, /\[hidden\] \{ display: none !important/);
  assert.match(scoped, /prefers-reduced-motion: reduce/);
  const variables = text => Object.fromEntries(Array.from(text.matchAll(/--([\w-]+):\s*(#[\da-f]{6}|#[\da-f]{3})/gi), match => [match[1], match[2].length === 4 ? "#" + match[2].slice(1).split("").map(value => value + value).join("") : match[2]]));
  const light = { ...variables(base.match(/:root\s*\{([^}]+)\}/)[1]), ...variables(scoped.match(/body\.project-detail\s*\{([^}]+)\}/)[1]) };
  const dark = { ...variables(base.match(/html\[data-theme="dark"\]\s*\{([^}]+)\}/)[1]), ...variables(scoped.match(/html\[data-theme="dark"\] body\.project-detail\s*\{([^}]+)\}/)[1]) };
  function luminance(color) {
    const channels = color.slice(1).match(/../g).map(value => parseInt(value, 16) / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + 0.05) / (Math.min(luminance(a), luminance(b)) + 0.05);
  const evidence = [];
  for (const [theme, colors] of [["light", light], ["dark", dark]]) {
    const text = [];
    for (const fg of ["ink", "muted", "accent-dark", "danger"]) {
      for (const bg of ["surface", "canvas"]) text.push(contrast(colors[fg], colors[bg]));
    }
    for (const bg of ["button-bg", "button-hover"]) text.push(contrast("#ffffff", colors[bg]));
    text.push(contrast(colors.ink, colors["field-bg"]));
    assert.ok(Math.min(...text) >= 4.5, `${theme} text minimum ${Math.min(...text)}`);
    const controls = [];
    for (const fg of ["field-border", "wp-focus", "accent-dark"]) {
      for (const bg of ["surface", "canvas", "field-bg"]) controls.push(contrast(colors[fg], colors[bg]));
    }
    assert.ok(Math.min(...controls) >= 3, `${theme} control minimum ${Math.min(...controls)}`);
    evidence.push(`${theme} text=${Math.min(...text).toFixed(3)} control=${Math.min(...controls).toFixed(3)}`);
  }
  console.log("WP02 CSS-token contrast evidence (not browser geometry): " + evidence.join("; "));
});
