// ponytail: native dialog and stable GET filters; no popup library or temporary page files.
(() => {
  const dialog = document.getElementById("chart-paper-popup");
  if (!dialog) return;
  const status = document.getElementById("chart-popup-status");
  const heading = document.getElementById("chart-popup-title");
  const list = document.getElementById("chart-popup-papers");
  const all = document.getElementById("chart-popup-all");
  let sequence = 0;
  async function openPoint(data) {
      if (!Array.isArray(data) || !["found", "title", "abstract"].includes(data[0]) ||
          !["year", "through"].includes(data[1]) || !Number.isInteger(data[2])) return;
      const url = new URL(dialog.dataset.entriesUrl, window.location.origin);
      url.search = new URLSearchParams({ step: data[0], mode: data[1], year: data[2] });
      all.href = url.href;
      list.replaceChildren();
      status.textContent = "Loading entries…";
      heading.textContent = "Papers at this point";
      if (!dialog.open) dialog.showModal();
      const ticket = ++sequence;
      url.searchParams.set("format", "json");
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error("The paper list could not be loaded.");
        const result = await response.json();
        if (ticket !== sequence || !dialog.open) return;
        heading.textContent = result.label;
        status.textContent = `${result.count} entries. Showing the first ${result.papers.length}.`;
        for (const paper of result.papers) {
          const item = document.createElement("li");
          const link = document.createElement("a");
          link.textContent = `${paper.title} (${paper.year})`;
          link.href = paper.url;
          item.append(link);
          list.append(item);
        }
      } catch (error) {
        if (ticket === sequence && dialog.open) status.textContent = error.message;
      }
  }
  function bind(plot) {
    if (!plot || typeof plot.on !== "function" || plot.dataset.paperPopupBound) return;
    const points = (plot.data || []).flatMap(trace => (trace.customdata || [])
      .filter(data => Array.isArray(data) && ["found", "title", "abstract"].includes(data[0]))
      .map(data => ({ data, name: trace.name })));
    if (!points.length) return;
    plot.dataset.paperPopupBound = "true";
    plot.on("plotly_click", event => openPoint(event.points?.[0]?.customdata));
    // ponytail: native select/button gives keyboard and touch access without rebuilding Plotly navigation.
    const controls = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Open a chart point's paper list (keyboard or touch)";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Chart series and publication year");
    for (const [index, point] of points.entries()) {
      const option = document.createElement("option");
      option.value = index;
      option.textContent = `${point.name}: ${point.data[1] === "year" ? "in" : "through"} ${point.data[2]}`;
      select.append(option);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Preview entries";
    button.addEventListener("click", () => openPoint(points[select.value].data));
    controls.append(summary, select, button);
    plot.after(controls);
  }
  window.addEventListener("survey-chart-ready", event => bind(document.getElementById(event.detail)));
  document.querySelectorAll(".js-plotly-plot").forEach(bind);
  window.addEventListener("load", () => document.querySelectorAll(".js-plotly-plot").forEach(bind), { once: true });
})();
