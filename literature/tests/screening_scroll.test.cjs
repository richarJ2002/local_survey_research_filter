// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
// Run: node literature/tests/screening_scroll.test.cjs (stdlib only).
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");
const test = require("node:test");
const source = readFileSync(join(__dirname, "../templates/literature/screening.html"), "utf8")
  .match(/<script>([\s\S]*?)<\/script>/)[1];

function makePage(path, storage, response, blocked = false) {
  const listeners = {};
  const scrolls = [];
  const requests = [];
  const error = {hidden: true, textContent: ""};
  const buttons = ["REJECT", "ACCEPT"].map(decision => ({
    dataset: {decision}, disabled: false,
    addEventListener(_event, handler) { this.click = handler; },
  }));
  const card = {dataset: {paperId: "1", stageId: "2"},
    querySelectorAll() { return buttons; },
    querySelector() { return {value: "synthetic-csrf"}; }};
  const document = {addEventListener() {},
    getElementById(id) { return id === "screening-card" ? card : error; },
    querySelectorAll() { return buttons; }};
  const window = {
    location: {origin: "http://127.0.0.1:8000", pathname: path,
      href: "http://127.0.0.1:8000" + path + "?paper=1"},
    scrollX: 15, scrollY: 340,
    sessionStorage: {
      getItem(key) { if (blocked) throw Error("blocked"); return storage.get(key) || null; },
      removeItem(key) { if (blocked) throw Error("blocked"); storage.delete(key); },
      setItem(key, value) { if (blocked) throw Error("blocked"); storage.set(key, value); },
    },
    addEventListener(name, handler) { listeners[name] = handler; },
    scrollTo(value) { scrolls.push(value); },
    __wpHandle() { return "synthetic-handle"; },
  };
  runInNewContext(source, {document, window, URL,
    async fetch(url, options) { requests.push({url, options}); return response; }});
  return {window, listeners, scrolls, buttons, card, error, requests};
}

test("next Title and Abstract entries consume one-shot scroll coordinates without animation", async () => {
  for (const stage of ["title", "abstract"]) {
    const path = `/p/project-uuid/worker-epoch/project/1/screen/${stage}/`;
    const key = "screening-scroll:" + path;
    const storage = new Map();
    const response = {ok: true, async json() { return {next_paper_url: path}; }};
    const first = makePage(path, storage, response);
    await first.buttons[1].click();
    assert.deepEqual(JSON.parse(storage.get(key)), [15, 340]);
    assert.equal(first.window.location.href, path);
    assert.equal(first.requests[0].options.headers["X-Project-Handle"], "synthetic-handle");
    assert.equal(first.requests[0].options.headers["X-CSRFToken"], "synthetic-csrf");
    assert.equal(JSON.parse(first.requests[0].options.body).decision, "ACCEPT");
    const next = makePage(path, storage, response);
    assert.equal(storage.has(key), false, "consume once, not on every future visit");
    next.listeners.load();
    assert.deepEqual({...next.scrolls[0]}, {left: 15, top: 340, behavior: "instant"});
    assert.equal(makePage(path, storage, response).listeners.load, undefined);
  }
});

test("completion, reference review, other stages/projects and failed decisions do not inherit a position", async () => {
  const path = "/p/project-uuid/worker-epoch/project/1/screen/abstract/";
  for (const target of [
    "/p/project-uuid/worker-epoch/project/1/screen/abstract/complete/",
    "/p/project-uuid/worker-epoch/project/1/references/",
    "/p/project-uuid/worker-epoch/project/1/screen/title/",
    "/p/other-project/other-epoch/project/1/screen/abstract/",
    "http://127.0.0.1:9000" + path,
  ]) {
    const storage = new Map();
    const page = makePage(path, storage, {ok: true,
      async json() { return {next_paper_url: target}; }});
    await page.buttons[0].click();
    assert.equal(storage.size, 0);
    assert.equal(page.window.location.href, target);
  }
  for (const acceptDisabled of [false, true]) {
    const storage = new Map();
    const failed = makePage(path, storage, {ok: false,
      async json() { return {error: "Synthetic decision failure"}; }});
    failed.buttons[1].disabled = acceptDisabled;
    await failed.buttons[0].click();
    assert.equal(storage.size, 0);
    assert.equal(failed.error.hidden, false);
    assert.equal(failed.card.dataset.submitting, "false");
    assert.deepEqual(failed.buttons.map(button => button.disabled), [false, acceptDisabled]);
  }
});

test("malformed or blocked storage cannot break screening", async () => {
  const path = "/project/1/screen/title/";
  const key = "screening-scroll:" + path;
  for (const value of ["not-json", "null", "{}", "[1]", '[1,"340"]', "[-1,340]", "[0,1e999]"]) {
    const storage = new Map([[key, value]]);
    const page = makePage(path, storage, {ok: true});
    assert.equal(page.listeners.load, undefined);
    assert.equal(storage.size, 0);
  }
  const blocked = makePage(path, new Map(), {ok: true,
    async json() { return {next_paper_url: path}; }}, true);
  await blocked.buttons[1].click();
  assert.equal(blocked.window.location.href, path);
  assert.equal(blocked.error.hidden, true);
});
