// Run: node literature/tests/survey_entry_dialog.test.cjs (stdlib only).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const script = fs.readFileSync(path.join(__dirname, "../static/literature/survey_entry_dialog.js"), "utf8");
const handlers = {};
const dialog = {open: false, showModal() { this.open = true; }, addEventListener(name, fn) { handlers[name] = fn; }};
const frame = {};
const heading = {};
const links = ["/project/1/survey/add/", "/project/1/survey/2/edit/"].map((href, index) => ({href, dataset: {surveyTitle: index ? "Edit [2]" : "Add entry"}, addEventListener(name, fn) { this.click = fn; }}));
const ids = {"survey-entry-dialog": dialog, "survey-entry-frame": frame, "survey-entry-dialog-title": heading};
const document = {getElementById: id => ids[id], querySelectorAll: () => links};
vm.runInNewContext(script, {document, window: {location: {origin: "http://localhost"}}, URL});
function click(overrides = {}) {
  const event = {button: 0, prevented: false, preventDefault() { this.prevented = true; }, ...overrides};
  links[0].click(event);
  return event;
}
assert.equal(click({ctrlKey: true}).prevented, false);
assert.equal(click({button: 1}).prevented, false);
assert.equal(dialog.open, false);
assert.equal(click().prevented, true);
assert.equal(frame.src, "http://localhost/project/1/survey/add/?popup=1");
assert.equal(frame.title, "Add entry");
assert.equal(heading.textContent, "Add entry");
assert.equal(dialog.open, true);
handlers.close();
assert.equal(frame.src, "about:blank");
dialog.open = false;
links[1].click({button: 0, preventDefault() {}});
assert.equal(frame.src, "http://localhost/project/1/survey/2/edit/?popup=1");
assert.equal(frame.title, "Edit [2]");
const unsupportedLinks = [{addEventListener() { throw new Error("Must preserve ordinary link fallback"); }}];
vm.runInNewContext(script, {document: {getElementById: () => ({}), querySelectorAll: () => unsupportedLinks}});
const saved = fs.readFileSync(path.join(__dirname, "../templates/literature/survey_entry_saved.html"), "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
let refreshed = "";
const parentURL = "http://localhost/project/1/plots/?depth=3";
vm.runInNewContext(saved, {window: {parent: {location: {href: parentURL, replace(url) { refreshed = url; }}}}});
assert.equal(refreshed, parentURL, "Return by GET without resubmitting an iframe POST; retain owning page filters");
console.log("Survey entry dialog checks passed: add/edit, modifiers, close, fallback and saved-page refresh.");
