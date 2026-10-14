// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
// Control-state checks only; not browser geometry qualification.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");
const test = require("node:test");
const source = readFileSync(join(__dirname, "../static/literature/paper_overview.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));

function controls(trackDrafts = false) {
  const elements = {}, docListeners = {}, windowListeners = {};
  const editForm = { tagName: "FORM", method: "post", dataset: {}, querySelector() { return {}; } };
  for (const id of ["paper-overview", "paper-overview-open", "paper-overview-body", "paper-overview-status", "paper-overview-retry", "paper-overview-close"]) {
    elements[id] = { listeners: {}, hidden: false, open: false, innerHTML: "", dataset: {}, textContent: "", focused: false,
      addEventListener(name, fn) { this.listeners[name] = fn; },
      replaceChildren() { this.innerHTML = ""; },
      showModal() { this.open = true; },
      close() { this.open = false; this.listeners.close(); },
      focus() { this.focused = true; } };
  }
  elements["paper-overview"].dataset.papersUrl = "/p/store/epoch/project/1/papers/";
  const requests = [];
  const document = { cookie: "", activeElement: null, visibilityState: "visible",
    getElementById(id) { return elements[id]; },
    addEventListener(name, fn) { (docListeners[name] ||= []).push(fn); },
    querySelector(selector) {
      if (selector === "[data-project-revision-url]") return { dataset: { projectRevisionUrl: "/revision", revision: "before" } };
      if (selector.startsWith("form[data-dirty")) return editForm.dataset.dirty === "true" ? editForm : null;
      return null;
    }, querySelectorAll() { return []; } };
  const window = { location: { origin: "http://127.0.0.1:8000", pathname: "/p/store/epoch/project/1/", search: "", reload() {} },
    parent: { __wpHandle() { return "test-handle"; } },
    sessionStorage: { getItem() { return ""; }, setItem() {} },
    addEventListener(name, fn) { (windowListeners[name] ||= []).push(fn); } };
  const context = { document, window, URL, URLSearchParams, AbortController,
    FormData: class { constructor(form) { return form.fields; } },
    fetch(url, options) { return new Promise(resolve => requests.push({ url: new URL(url), options, resolve })); } };
  if (trackDrafts) {
    for (const filename of ["project_handle.js", "project_refresh.js"]) runInNewContext(readFileSync(join(__dirname, "../static/literature/", filename), "utf8"), context);
  }
  runInNewContext(source, context);
  const click = id => elements[id].listeners.click({ preventDefault() {} });
  const reply = (index, html, ok = true) => requests[index].resolve({ ok, text: async () => html });
  return { elements, requests, click, reply, document, window, docListeners, windowListeners, editForm };
}

test("on-demand pages ignore stale/late responses, Escape cancels, retry and reopen revalidate", async () => {
  const { elements: e, requests, click, reply } = controls();
  assert.equal(requests.length, 0);
  let stopped = false;
  e["paper-overview-body"].listeners.input({ stopPropagation() { stopped = true; } });
  assert.equal(stopped, true);
  assert.equal(requests.length, 0);
  click("paper-overview-open");
  assert.equal(requests[0].url.searchParams.get("view"), "hud");
  click("paper-overview-open");
  assert.equal(requests[0].options.signal.aborted, true);
  reply(1, "new bounded page"); await tick();
  reply(0, "obsolete page"); await tick();
  assert.equal(e["paper-overview-body"].innerHTML, "new bounded page");
  const form = { fields: [["q", "explicit filter"], ["status", "final"]] };
  e["paper-overview-body"].listeners.submit({ target: { closest() { return form; } }, preventDefault() {}, stopPropagation() {} });
  assert.equal(requests[2].url.searchParams.get("q"), "explicit filter");
  e["paper-overview"].listeners.cancel();
  assert.equal(requests[2].options.signal.aborted, true);
  click("paper-overview-close"); // Models native Escape's default close event.
  assert.equal(e["paper-overview-open"].focused, true);
  reply(2, "late closed page"); await tick();
  assert.notEqual(e["paper-overview-body"].innerHTML, "late closed page");
  click("paper-overview-open");
  assert.equal(requests[3].url.searchParams.get("status"), "final");
  reply(3, "", false); await tick();
  assert.equal(e["paper-overview-retry"].hidden, false);
  assert.match(e["paper-overview-status"].textContent, /Nothing was changed/);
  click("paper-overview-retry"); reply(4, "retried page"); await tick();
  assert.equal(e["paper-overview-body"].innerHTML, "retried page");
  const link = { getAttribute() { return "?view=hud&status=final&page=2"; } };
  e["paper-overview-body"].listeners.click({ target: { closest() { return link; } }, preventDefault() {} });
  assert.equal(requests[5].url.pathname, "/p/store/epoch/project/1/papers/");
  assert.equal(requests[5].url.searchParams.get("page"), "2");
});

test("HUD events neither create nor discard existing research drafts", () => {
  for (const genuine of [false, true]) {
    const { elements: e, document, window, docListeners, windowListeners, editForm, click } = controls(true);
    const form = { tagName: "FORM", method: "get", dataset: { hudFilter: "" }, fields: [["status", "final"]], closest() { return this; } };
    if (genuine) (docListeners.input || []).forEach(fn => fn({ target: { form: editForm } }));
    click("paper-overview-open");
    document.activeElement = { tagName: "INPUT", form };
    for (const name of ["input", "submit"]) {
      const event = { target: name === "input" ? { form } : form, defaultPrevented: false, stopped: false,
        preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
      e["paper-overview-body"].listeners[name](event);
      if (!event.stopped) (docListeners[name] || []).forEach(fn => fn(event));
    }
    click("paper-overview-close");
    assert.equal(window.__wpDirty(), genuine);
    assert.equal(form.dataset.dirty, undefined);
    assert.equal(editForm.dataset.dirty === "true", genuine);
    let prompt = false;
    windowListeners.beforeunload.forEach(fn => fn({ preventDefault() { prompt = true; } }));
    assert.equal(prompt, genuine);
  }
});

test("missing native dialog support leaves the ordinary href usable", () => {
  const opener = { addEventListener() { assert.fail("fallback was intercepted"); } };
  runInNewContext(source, { document: { getElementById(id) { return id === "paper-overview" ? {} : opener; } } });
});

test("chart/filter GETs neither create drafts nor clear genuine POST drafts", () => {
  for (const genuine of [false, true]) {
    const { window, docListeners, windowListeners, editForm } = controls(true);
    if (genuine) docListeners.input.forEach(fn => fn({ target: { form: editForm } }));
    const filter = { tagName: "FORM", method: "get", dataset: {} };
    docListeners.input.forEach(fn => fn({ target: { form: filter } }));
    docListeners.submit.forEach(fn => fn({ target: filter, defaultPrevented: false }));
    assert.equal(window.__wpDirty(), genuine);
    assert.equal(filter.dataset.dirty, undefined);
    assert.equal(editForm.dataset.dirty === "true", genuine);
    let prompt = false;
    windowListeners.beforeunload.forEach(fn => fn({ preventDefault() { prompt = true; } }));
    assert.equal(prompt, genuine);
    docListeners.submit.forEach(fn => fn({ target: editForm, defaultPrevented: false }));
    assert.equal(window.__wpDirty(), false);
    assert.equal(editForm.dataset.dirty, "false");
  }
});
