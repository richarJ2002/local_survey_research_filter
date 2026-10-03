// Run: node literature/tests/graph_node_popup.test.cjs (stdlib only, no npm).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

// ponytail: a tiny DOM fake checks popup state; real-browser layout still needs a browser.
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.handlers = {};
    this.style = {};
    this.plotHandlers = {};
    this.hidden = false;
    this.textContent = "";
    this.value = "0";
  }
  setAttribute(name, value) { this[name] = value; }
  set id(value) { this._id = value; registry[value] = this; }
  get id() { return this._id; }
  append(...items) { this.children.push(...items); return items.length; }
  appendChild(item) { this.children.push(item); return item; }
  after(element) { this.controls = element; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  focus() { Element.focused = this; }
  contains(element) { return element === this || this.children.some(child => child.contains(element)); }
  getBoundingClientRect() { return {left: 10, top: 20, bottom: 50, width: 340, height: 220}; }
}
Element.focused = null;

const registry = {};
const plots = [];
const winEvents = {};
const docEvents = {};
const body = new Element("body");
const details0 = { title: "Alpha study", year: 2021, venue: "Nature", citations: 7, ref: 3 };
const details1 = { title: "<script>unsafe</script>", year: null, venue: "", citations: 0, ref: null };
const trace = { meta: { paper_details: [details0, details1] }, customdata: ["<b>kept html</b>", "<i>kept html</i>"] };
const plot = new Element("plot");
plot.dataset = {};
plot.data = [trace];
plot.on = (name, handler) => { plot.plotHandlers[name] = handler; };
plot.getBoundingClientRect = () => ({ left: 10, top: 20 });
registry.plot = plot;

const context = {
  setTimeout, clearTimeout, Number, Array, Object, Math, String,
  document: {
    get activeElement() { return Element.focused; },
    getElementById: (id) => registry[id] || null,
    createElement: (tag) => new Element(tag),
    querySelectorAll: () => plots.slice(),
    body,
    addEventListener: (name, handler) => { docEvents[name] = handler; },
  },
  window: {
    innerWidth: 1024, innerHeight: 768,
    addEventListener: (name, handler) => { winEvents[name] = handler; },
  },
};
context.window.window = context.window;
const source = fs.readFileSync(path.join(__dirname, "../static/literature/graph_node_popup.js"), "utf8");
assert.match(source, /ponytail:/);
assert.doesNotMatch(source, /innerHTML/);
vm.createContext(context);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const popupOf = () => registry["graph-node-popup"] || context.document.getElementById("graph-node-popup");
// Patch createElement to register the popup id for lookup.
const rawCreate = context.document.createElement;
context.document.createElement = (tag) => {
  const el = rawCreate(tag);
  const rawSet = el.setAttribute.bind(el);
  el.setAttribute = (name, value) => {
    rawSet(name, value);
    if (name === "id") registry[value] = el;
  };
  return el;
};

(async () => {
  vm.runInContext(source, context);
  assert.equal(plots.length, 0);
  plots.push(plot); // Plot appears asynchronously after Plotly renders.
  winEvents["graph-ready"]({ detail: "plot" });
  const firstControls = plot.controls;
  assert.ok(firstControls, "keyboard/touch controls are added");
  assert.equal(firstControls.children[1]["aria-label"], "Graph node paper");
  assert.equal(plot.dataset.graphNodePopupBound, "true");
  winEvents["graph-ready"]({ detail: "plot" });
  assert.equal(plot.controls, firstControls, "no duplicate per-plot binding");
  assert.equal(trace.customdata[0], "<b>kept html</b>", "existing customdata untouched");

  const popup = popupOf();
  assert.ok(popup, "shared popup exists");
  assert.equal(popup.hidden, true);
  const closeBtn = popup.children[popup.children.length - 1];
  const titleOf = () => popup.children[1].children[1].textContent;

  // Hover shows details without stealing focus.
  Element.focused = null;
  plot.plotHandlers.plotly_hover({ points: [{ curveNumber: 0, pointNumber: 0, data: trace }], event: { clientX: 50, clientY: 60 } });
  assert.equal(popup.hidden, false);
  assert.equal(titleOf(), "Alpha study");
  assert.equal(Element.focused, null, "hover must not steal focus");
  assert.match(popup.style.cssText || "", /user-select:text/);
  assert.match(source, /user-select:text/);

  // Unhover starts a delayed close; entering the popup retains it so text can be copied.
  plot.plotHandlers.plotly_unhover();
  assert.equal(popup.hidden, false, "close is delayed, not immediate");
  popup.handlers.pointerenter();
  await sleep(420);
  assert.equal(popup.hidden, false, "popup enter cancels the unhover timer");
  plot.plotHandlers.plotly_unhover();
  await sleep(420);
  assert.equal(popup.hidden, false, "late plot unhover cannot close a hovered popup");
  popup.handlers.pointerleave();
  await sleep(420);
  assert.equal(popup.hidden, true, "leaving an unpinned popup closes it after the delay");
  plot.plotHandlers.plotly_hover({ points: [{ curveNumber: 0, pointNumber: 0, data: trace }], event: { clientX: 50, clientY: 60 } });

  // Selecting text / pressing in the popup pins it open.
  popup.handlers.pointerdown();
  if (popup.handlers.selectstart) popup.handlers.selectstart();
  plot.plotHandlers.plotly_unhover();
  await sleep(420);
  assert.equal(popup.hidden, false, "pointer-down/selection pins the popup");
  plot.plotHandlers.plotly_hover({ points: [{ curveNumber: 0, pointNumber: 1, data: trace }], event: { clientX: 40, clientY: 30 } });
  assert.equal(titleOf(), "Alpha study", "hovering another node must not replace text being copied");

  // Close button dismisses.
  closeBtn.handlers.click();
  assert.equal(popup.hidden, true);

  // Click/tap pins the selection.
  plot.plotHandlers.plotly_click({ points: [{ curveNumber: 0, pointNumber: 0, data: trace }], event: { clientX: 70, clientY: 80 } });
  assert.equal(popup.hidden, false);
  plot.plotHandlers.plotly_unhover();
  await sleep(420);
  assert.equal(popup.hidden, false, "click/tap pins the selection");
  docEvents.keydown({ key: "Escape" });
  assert.equal(popup.hidden, true);

  // Unsafe strings land in textContent, never as markup.
  plot.plotHandlers.plotly_hover({ points: [{ curveNumber: 0, pointNumber: 1, data: trace }], event: { clientX: 10, clientY: 10 } });
  assert.equal(titleOf(), "<script>unsafe</script>");
  assert.equal(popup.children[1].children[3].textContent, "unknown");
  assert.equal(popup.children[1].children[9].textContent, "—");
  assert.match(popup.style.cssText, /max-height:calc\(100vh - 1rem\);overflow:auto/);
  plot.plotHandlers.plotly_click({ points: [{ curveNumber: 0, pointNumber: 1, data: trace }], event: { clientX: 9999, clientY: 9999 } });
  assert.equal(popup.style.left, "676px");
  assert.equal(popup.style.top, "540px");
  docEvents.keydown({ key: "Escape" });
  assert.equal(popup.hidden, true, "document Escape dismisses");

  // Keyboard/touch path opens, focuses popup, and returns to the opener.
  firstControls.children[1].value = "0";
  Element.focused = null;
  firstControls.children[2].handlers.click();
  assert.equal(popup.hidden, false);
  assert.equal(Element.focused, closeBtn, "keyboard opens and focuses the popup");
  closeBtn.handlers.click();
  assert.equal(popup.hidden, true);
  assert.equal(Element.focused, firstControls.children[2], "close returns to the opener");

  // A second figure script emission must not duplicate listeners or controls.
  const before = winEvents["graph-ready"];
  vm.runInContext(source, context);
  assert.equal(winEvents["graph-ready"], before, "no duplicate global listeners");
  winEvents["graph-ready"]({ detail: "plot" });
  assert.equal(plot.controls, firstControls, "still a single binding after re-emit");
  console.log("Graph node popup checks passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
