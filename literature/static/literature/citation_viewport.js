// Inlined by build_citation_figure after Plotly.newPlot, including offline exports.
(function () {
  "use strict";

  function minimumSpan(bounds, nodePixels, axisPixels) {
    const fullSpan = bounds[1] - bounds[0];
    // Hidden/unfinished canvases retain the reference span until their size is known.
    if (![nodePixels, axisPixels].every(value => Number.isFinite(value) && value > 0)) return fullSpan;
    // Three smallest diameters measured at the readable initial view, independent of current zoom.
    const span = fullSpan * Math.min(1, 3 * nodePixels / axisPixels);
    return Number.isFinite(span) && span > 0 ? span : fullSpan;
  }

  function smallestNodeDiameter(plot) {
    let smallest = Infinity;
    for (const trace of plot._fullData || plot.data || []) {
      if (!trace.meta || !trace.meta.paper_details || !trace.marker) continue;
      const size = trace.marker.size;
      for (const value of Array.isArray(size) || ArrayBuffer.isView(size) ? size : [size]) {
        if (Number.isFinite(value) && value > 0) smallest = Math.min(smallest, value);
      }
    }
    return Number.isFinite(smallest) ? smallest : plot.layout.meta.smallest_node_diameter;
  }

  function clamp(range, limit, panSpan) {
    const [low, high] = limit.bounds;
    if (!Array.isArray(range) || range.length !== 2 || !range.every(Number.isFinite)) {
      return [low, high];
    }
    const [a, b] = range[0] <= range[1] ? range : [range[1], range[0]];
    const span = Math.max(limit.minSpan, Math.min(high - low, panSpan ?? b - a));
    // Shift, don't trim: panning into a wall must retain the zoomed window's size.
    const start = Math.max(low, Math.min(high - span, a / 2 + b / 2 - span / 2));
    return [start, start + span];
  }

  function bind(plot) {
    const limits = plot.layout.meta.citation_viewport;
    let queued = false;
    let correcting = null;
    let pan = null;
    const requested = {};
    function startPan(event) {
      if (plot.layout.dragmode !== "pan" || !event.target.closest(".nsewdrag")) return;
      pan = {};
      for (const name of Object.keys(limits)) {
        const range = plot._fullLayout[name].range;
        pan[name] = Math.abs(range[1] - range[0]);
      }
    }
    // Plotly trims even the emitted drag ranges at native walls; snapshot the pan's zoom level.
    plot.addEventListener("mousedown", startPan, true);
    plot.addEventListener("touchstart", startPan, {capture: true, passive: true});
    for (const end of ["mouseup", "touchend", "touchcancel", "blur"]) {
      window.addEventListener(end, () => { pan = null; });
    }
    function schedule(event) {
      for (const name of Object.keys(limits)) {
        if (!event) break;
        let range;
        if (event[name + ".autorange"] === true) range = limits[name].bounds;
        else if (name + ".range" in event) range = event[name + ".range"];
        else if (name + ".range[0]" in event || name + ".range[1]" in event) {
          range = plot._fullLayout[name].range.map((value, i) =>
            name + ".range[" + i + "]" in event ? event[name + ".range[" + i + "]"] : value);
        } else continue;
        const own = correcting && correcting[name + ".range"];
        if (own && Array.isArray(range) && range.length === 2 && range.every((value, i) => value === own[i])) continue;
        // Native outer bounds can trim an attempted pan; retain the requested span before clipping.
        requested[name] = {range: Array.isArray(range) ? range.slice() : range,
          panSpan: event[name + ".autorange"] === true ? null : pan && pan[name]};
      }
      if (queued || correcting) return;
      queued = true;
      requestAnimationFrame(enforce);
    }
    function enforce() {
      queued = false;
      const update = {};
      const diameter = smallestNodeDiameter(plot);
      for (const [name, geometry] of Object.entries(limits)) {
        const limit = {bounds: geometry.bounds,
          minSpan: minimumSpan(geometry.initial || geometry.bounds, diameter, plot._fullLayout[name]._length)};
        const range = plot._fullLayout[name].range;
        const desired = requested[name];
        const bounded = clamp(desired ? desired.range : range, limit, desired && desired.panSpan);
        delete requested[name];
        const tolerance = limit.minSpan * 1e-9;
        if (range.some((value, i) => !Number.isFinite(value) || Math.abs(value - bounded[i]) > tolerance)) {
          update[name + ".range"] = bounded;
          update[name + ".autorange"] = false;
        }
      }
      if (!Object.keys(update).length) return;
      correcting = update;
      // Coalesce wheel/drag/resize events; reread the latest viewport after our async correction.
      Promise.resolve(Plotly.relayout(plot, update)).then(() => {
        correcting = null;
        schedule();
      }, (error) => {
        correcting = null;
        console.error("Citation viewport correction failed", error);
      });
    }
    plot.on("plotly_relayouting", schedule);
    plot.on("plotly_relayout", schedule);
    plot.on("plotly_afterplot", schedule);
    const region = plot.closest('[role="region"]');
    region.addEventListener("keydown", event => {
      if (event.target !== region || event.altKey || event.ctrlKey || event.metaKey) return;
      const key = event.key;
      if (!["+", "=", "-", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home"].includes(key)) return;
      const update = {};
      for (const [name, geometry] of Object.entries(limits)) {
        const range = plot._fullLayout[name].range;
        const span = range[1] - range[0];
        if (key === "Home") update[name + ".range"] = geometry.fit || geometry.bounds;
        else if (key.length === 1) {
          const middle = range[0] / 2 + range[1] / 2;
          const half = span / 2 * (key === "-" ? 1.25 : 0.8);
          update[name + ".range"] = [middle - half, middle + half];
        } else if ((name === "xaxis" && ["ArrowLeft", "ArrowRight"].includes(key)) ||
                   (name === "yaxis" && ["ArrowDown", "ArrowUp"].includes(key))) {
          const shift = span * (["ArrowLeft", "ArrowDown"].includes(key) ? -0.1 : 0.1);
          update[name + ".range"] = range.map(value => value + shift);
        }
      }
      event.preventDefault();
      // Keyboard movement shares the wheel/drag clamp and redraw coalescing.
      schedule(update);
    });
    schedule();
  }

  if (typeof module === "object" && module.exports) module.exports = {clamp, bind, minimumSpan, smallestNodeDiameter};
  else bind(document.getElementById("{plot_id}"));
})();
