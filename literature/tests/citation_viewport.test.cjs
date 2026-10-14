// Run: node literature/tests/citation_viewport.test.cjs (stdlib only, no npm).
const assert = require("node:assert/strict");
const {clamp, bind, minimumSpan, smallestNodeDiameter} = require("../static/literature/citation_viewport.js");
const limit = {bounds: [-2, 12], minSpan: 2.5};
assert.equal(minimumSpan(limit.bounds, 25, 420), 2.5);
assert.equal(minimumSpan(limit.bounds, 50, 420), 5);
assert.equal(minimumSpan(limit.bounds, 25, 840), 1.25);
assert.equal(minimumSpan(limit.bounds, 25, 20), 14);
assert.equal(minimumSpan(limit.bounds, Number.MAX_VALUE, 420), 14);
assert.equal(minimumSpan(limit.bounds, Number.MIN_VALUE, Number.MAX_VALUE), 14);
for (const invalid of [0, -1, NaN, Infinity, undefined, null, "25"]) {
  assert.equal(minimumSpan(limit.bounds, invalid, 420), 14);
  assert.equal(minimumSpan(limit.bounds, 25, invalid), 14);
}
for (const [range, expected] of [
  [[2, 7], [2, 7]], [[-20, -15], [-2, 3]], [[20, 25], [7, 12]],
  [[-1000, 1000], [-2, 12]], [[5, 5], [3.75, 6.25]],
  [[5, 5.01], [3.755, 6.255]], [[7, 2], [2, 7]],
  [[NaN, 2], [-2, 12]], [[-Infinity, Infinity], [-2, 12]],
  [null, [-2, 12]], [[1], [-2, 12]], [["1", "2"], [-2, 12]],
  [[Number.MAX_VALUE, Number.MAX_VALUE], [9.5, 12]],
]) assert.deepEqual(clamp(range, limit), expected);
for (let i = 0; i < 1000; i++) {
  const result = clamp([i / 20 - 30, i / 10 - 40], limit);
  assert.ok(result[0] >= -2 && result[1] <= 12);
  assert.ok(result[1] - result[0] >= 2.5);
  assert.deepEqual(clamp(result, limit), result);
}

// Only scheduling is faked here; installed Plotly is exercised in the Firefox check.
const frames = [];
global.requestAnimationFrame = fn => frames.push(fn);
const handlers = {};
const dom = {};
const region = {addEventListener: (name, fn) => { dom["region_" + name] = fn; }};
global.window = {addEventListener: (name, fn) => { dom[name] = fn; }};
const plot = {
  layout: {dragmode: "pan", meta: {smallest_node_diameter: 25,
    citation_viewport: {xaxis: {bounds: limit.bounds}, yaxis: {bounds: limit.bounds}}}},
  _fullData: [{meta: {paper_details: [{}]}, marker: {size: [25, 50]}},
    {mode: "markers", marker: {size: 7}}], // Edge tooltips are not node diameters.
  _fullLayout: {xaxis: {range: [5, 5.01], _length: 420}, yaxis: {range: [20, 25], _length: 420}},
  on: (name, fn) => { handlers[name] = fn; },
  addEventListener: (name, fn) => { dom[name] = fn; },
  closest: () => region,
};
assert.equal(smallestNodeDiameter(plot), 25);
assert.equal(smallestNodeDiameter({...plot, _fullData: [
  {meta: {paper_details: [{}]}, marker: {size: new Float64Array([50, 35])}},
]}), 35);
assert.equal(smallestNodeDiameter({...plot, _fullData: [
  {meta: {paper_details: [{}]}, marker: {size: 46}},
]}), 46);
assert.equal(smallestNodeDiameter({...plot, _fullData: [
  {meta: {paper_details: [{}]}, marker: {size: [NaN, 0, Infinity]}},
]}), 25, "missing/invalid rendered sizes fall back to builder metadata");
let finish;
let calls = 0;
global.Plotly = {relayout: (plot, update) => {
  calls++;
  for (const axis of ["xaxis", "yaxis"]) {
    if (update[axis + ".range"]) plot._fullLayout[axis].range = update[axis + ".range"];
  }
  handlers.plotly_relayout(update);
  return new Promise(resolve => { finish = resolve; });
}};
(async () => {
  bind(plot);
  handlers.plotly_relayouting();
  handlers.plotly_afterplot();
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.equal(calls, 1);
  // A new interaction while the correction is pending must not get lost.
  plot._fullLayout.xaxis.range = [-200, -195];
  handlers.plotly_relayout({"xaxis.range[0]": -200, "xaxis.range[1]": -195});
  finish();
  await Promise.resolve();
  frames.shift()();
  assert.equal(calls, 2);
  assert.deepEqual(plot._fullLayout.xaxis.range, [-2, 3]);
  finish();
  await Promise.resolve();
  frames.shift()();
  assert.equal(calls, 2, "corrective relayout must settle, not loop");
  assert.equal(frames.length, 0);
  // Resize uses current canvas pixels, not the old minimum or current zoom scale.
  plot._fullLayout.xaxis.range = [4, 6.5];
  plot._fullLayout.xaxis._length = 210;
  handlers.plotly_afterplot();
  frames.shift()();
  assert.deepEqual(plot._fullLayout.xaxis.range, [2.75, 7.75]);
  finish();
  await Promise.resolve();
  frames.shift()();
  plot._fullLayout.xaxis._length = 0;
  handlers.plotly_afterplot();
  frames.shift()();
  assert.deepEqual(plot._fullLayout.xaxis.range, [-2, 12], "hidden canvas stays finite at full fit");
  finish();
  await Promise.resolve();
  frames.shift()();
  plot._fullLayout.xaxis._length = 420;
  plot.layout.meta.citation_viewport.xaxis.initial = [2, 7];
  plot._fullLayout.xaxis.range = [4, 4.001];
  handlers.plotly_relayout({"xaxis.range": [4, 4.001]});
  frames.shift()();
  assert.ok(Math.abs(plot._fullLayout.xaxis.range[1] - plot._fullLayout.xaxis.range[0] - 5 * 75 / 420) < 1e-9,
    "a large graph can still zoom into its readable initial window");
  finish();
  await Promise.resolve();
  frames.shift()();
  delete plot.layout.meta.citation_viewport.xaxis.initial;
  plot._fullLayout.xaxis.range = [-2, 3];
  dom.mousedown({target: {closest: () => true}});
  plot._fullLayout.xaxis.range = [-2, 1];
  handlers.plotly_relayouting({"xaxis.range[0]": -2, "xaxis.range[1]": 1});
  handlers.plotly_relayout({"xaxis.range[0]": -2, "xaxis.range[1]": 1});
  dom.mouseup();
  frames.shift()();
  assert.deepEqual(plot._fullLayout.xaxis.range, [-2, 3], "native-clipped drag keeps its original span");
  finish();
  await Promise.resolve();
  frames.shift()();
  assert.equal(frames.length, 0);
  plot._fullLayout.xaxis.range = [-2, .5]; // Native bounds trimmed an attempted five-unit pan.
  handlers.plotly_relayout({"xaxis.range": [-20, -15]});
  frames.shift()();
  assert.deepEqual(plot._fullLayout.xaxis.range, [-2, 3]);
  finish();
  await Promise.resolve();
  frames.shift()();
  handlers.plotly_relayout({"xaxis.autorange": true});
  frames.shift()();
  assert.deepEqual(plot._fullLayout.xaxis.range, [-2, 12]);
  finish();
  await Promise.resolve();
  frames.shift()();
  assert.equal(frames.length, 0);
  plot._fullLayout.xaxis.range = [2, 7];
  plot._fullLayout.yaxis.range = [2, 7];
  plot.layout.meta.citation_viewport.xaxis.fit = [-1, 11];
  plot.layout.meta.citation_viewport.yaxis.fit = [-1, 11];
  for (const [key, expectedX, expectedY] of [
    ["ArrowRight", [2.5, 7.5], [2, 7]],
    ["ArrowLeft", [2, 7], [2, 7]],
    ["ArrowUp", [2, 7], [2.5, 7.5]],
    ["ArrowDown", [2, 7], [2, 7]],
    ["+", [2.5, 6.5], [2.5, 6.5]],
    ["-", [2, 7], [2, 7]],
    ["Home", [-1, 11], [-1, 11]],
  ]) {
    let prevented = false;
    dom.region_keydown({key, target: region, preventDefault: () => { prevented = true; }});
    frames.shift()();
    assert.equal(prevented, true);
    assert.deepEqual(plot._fullLayout.xaxis.range, expectedX);
    assert.deepEqual(plot._fullLayout.yaxis.range, expectedY);
    finish();
    await Promise.resolve();
    frames.shift()();
    assert.equal(frames.length, 0);
  }
  for (const event of [{key: "+", ctrlKey: true, target: region},
    {key: "+", target: {}}, {key: "Tab", target: region}]) {
    dom.region_keydown({...event, preventDefault: () => assert.fail("leave browser/form keys alone")});
    assert.equal(frames.length, 0);
  }
  console.log("citation viewport clamp/coalescing checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
