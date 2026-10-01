// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
// Dependency-free control-state tests; these are not a real-browser layout test.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const { runInNewContext } = require("node:vm");
const source = readFileSync(join(__dirname, "../static/literature/paper_entry.js"), "utf8");

function controls(keywordValue = "", titleValue = "", abstractValue = "") {
  function field(value) {
    return { value, disabled: false, required: false, listeners: {},
      addEventListener(event, listener) { this.listeners[event] = listener; } };
  }
  const fields = {
    search_keyword: field(keywordValue), new_keyword: field(""), new_keyword_search: field("1"),
    title_decision: field(titleValue), abstract_decision: field(abstractValue),
  };
  fields.abstract_decision.options = ["", "NOT_QUEUED", "PENDING", "ACCEPT", "REJECT"]
    .map(value => ({ value, disabled: false }));
  const newFields = { hidden: false };
  const windowListeners = {};
  runInNewContext(source, {
    document: { getElementById(id) { return id === "paper-entry-form" ? { elements: fields } : newFields; } },
    window: { addEventListener(event, listener) { windowListeners[event] = listener; } },
  });
  return { fields, newFields, windowListeners };
}

test("existing keyword mode hides and disables the creation fields", () => {
  const { fields, newFields } = controls("1|navigation");
  assert.equal(newFields.hidden, true);
  assert.equal(fields.new_keyword.disabled, true);
  assert.equal(fields.new_keyword.required, false);
});

test("new keyword mode reveals required fields and survives pageshow", () => {
  const { fields, newFields, windowListeners } = controls("NEW");
  assert.equal(newFields.hidden, false);
  assert.equal(fields.new_keyword.required, true);
  assert.equal(fields.new_keyword_search.disabled, false);
  fields.search_keyword.value = "1|navigation";
  fields.search_keyword.listeners.change();
  assert.equal(newFields.hidden, true);
  fields.search_keyword.value = "NEW";
  windowListeners.pageshow();
  assert.equal(newFields.hidden, false);
});

test("a new entry does not receive assumed status choices", () => {
  const { fields } = controls();
  assert.equal(fields.title_decision.value, "");
  assert.equal(fields.abstract_decision.value, "");
});

test("pending or failed Title sets Abstract to not applicable", () => {
  const { fields } = controls("", "REJECT", "ACCEPT");
  assert.equal(fields.abstract_decision.value, "NOT_QUEUED");
  assert.equal(fields.abstract_decision.options.find(option => option.value === "ACCEPT").disabled, true);
});

test("passing Title makes Abstract choices available and requires a choice", () => {
  const { fields } = controls("", "PENDING", "NOT_QUEUED");
  fields.title_decision.value = "ACCEPT";
  fields.title_decision.listeners.change();
  assert.equal(fields.abstract_decision.value, "");
  assert.equal(fields.abstract_decision.options.find(option => option.value === "NOT_QUEUED").disabled, true);
  assert.equal(fields.abstract_decision.options.find(option => option.value === "ACCEPT").disabled, false);
});

test("existing accepted Abstract is retained on initial render", () => {
  const { fields } = controls("1|navigation", "ACCEPT", "ACCEPT");
  assert.equal(fields.abstract_decision.value, "ACCEPT");
});
