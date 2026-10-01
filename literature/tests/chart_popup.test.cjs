// Run: node literature/tests/chart_popup.test.cjs (stdlib only, no npm).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

// ponytail: a tiny DOM fake checks control state; real-browser layout still needs a browser.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.handlers = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren() { this.children = []; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  after(element) { this.controls = element; }
  showModal() { this.open = true; }
}
const ids = Object.fromEntries(["chart-paper-popup", "chart-popup-status", "chart-popup-title", "chart-popup-papers", "chart-popup-all"].map(id => [id, new Element(id)]));
ids["chart-paper-popup"].dataset.entriesUrl = "/project/1/plots/entries/";
const plot = new Element("plot");
plot.data = [{name: "Final", customdata: [["abstract", "through", 2025]]}];
plot.on = (name, handler) => { plot.handlers[name] = handler; };
const events = {};
const requests = [];
const context = {
  URL, URLSearchParams, Number, Array,
  document: {getElementById: id => id === "plot" ? plot : ids[id], createElement: tag => new Element(tag), querySelectorAll: () => []},
  window: {location: {origin: "http://localhost"}, addEventListener: (name, handler) => { events[name] = handler; }},
  fetch: url => new Promise(resolve => requests.push({url: String(url), resolve})),
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/literature/chart_popup.js"), "utf8"), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = (label, title) => ({ok: true, json: async () => ({label, count: 1, papers: [{title, year: 2025, url: "/paper/1/"}]})});
(async () => {
  assert.equal(plot.controls, undefined);
  events["survey-chart-ready"]({detail: "plot"});
  const controls = plot.controls;
  assert.equal(controls.children[1]["aria-label"], "Chart series and publication year");
  events["survey-chart-ready"]({detail: "plot"});
  assert.equal(plot.controls, controls); // No duplicate binding.
  controls.children[1].value = "0";
  controls.children[2].handlers.click(); // Keyboard/touch button uses the same popup.
  assert.equal(ids["chart-paper-popup"].open, true);
  assert.match(ids["chart-popup-all"].href, /step=abstract&mode=through&year=2025$/);
  plot.handlers.plotly_click({points: [{customdata: ["found", "year", 2020]}]});
  assert.match(requests[1].url, /step=found&mode=year&year=2020&format=json$/);
  requests[1].resolve(response("Latest", "<script>unsafe</script>"));
  await flush();
  requests[0].resolve(response("Stale", "Old"));
  await flush();
  assert.equal(ids["chart-popup-title"].textContent, "Latest");
  assert.equal(ids["chart-popup-papers"].children[0].children[0].textContent, "<script>unsafe</script> (2025)");
  assert.equal(ids["chart-paper-popup"].open, true); // Remains until explicitly closed.
  plot.handlers.plotly_click({points: [{customdata: ["found", "year", 2021]}]});
  requests[2].resolve({ok: false});
  await flush();
  assert.match(ids["chart-popup-status"].textContent, /could not be loaded/);
  assert.equal(ids["chart-popup-papers"].children.length, 0);
  plot.handlers.plotly_click({points: [{customdata: ["found", "through", 2022]}]});
  ids["chart-paper-popup"].open = false;
  requests[3].resolve(response("Closed", "Ignored"));
  await flush();
  assert.notEqual(ids["chart-popup-title"].textContent, "Closed");
  plot.handlers.plotly_click({points: [{customdata: ["bad", "year", 2025]}]});
  assert.equal(requests.length, 4);
  console.log("Chart popup control-state checks passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
