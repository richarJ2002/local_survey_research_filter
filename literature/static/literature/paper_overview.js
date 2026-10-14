// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
// ponytail: one bounded HTML page on demand; no corpus cache or revision poller.
(() => {
  const dialog = document.getElementById("paper-overview");
  const opener = document.getElementById("paper-overview-open");
  if (!dialog || !opener || typeof dialog.showModal !== "function") return;
  const body = document.getElementById("paper-overview-body");
  const status = document.getElementById("paper-overview-status");
  const retry = document.getElementById("paper-overview-retry");
  let sequence = 0;
  let controller;
  let currentUrl = new URL(dialog.dataset.papersUrl, window.location.origin);
  currentUrl.searchParams.set("view", "hud");
  function cancel() {
    sequence++;
    controller?.abort();
  }
  async function load(url) {
    cancel();
    const ticket = sequence;
    controller = new AbortController();
    currentUrl = new URL(url, window.location.origin);
    currentUrl.searchParams.set("view", "hud");
    status.textContent = "Loading papers…";
    retry.hidden = true;
    body.replaceChildren();
    try {
      const response = await fetch(currentUrl, { signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) throw new Error("The paper list could not be loaded. Nothing was changed.");
      const html = await response.text();
      if (ticket !== sequence || !dialog.open) return;
      // Only the owning local HTML template is inserted; research text is escaped.
      body.innerHTML = html;
      status.textContent = "";
    } catch (error) {
      if (ticket !== sequence || !dialog.open || error.name === "AbortError") return;
      status.textContent = error.message;
      retry.hidden = false;
    }
  }
  opener.addEventListener("click", event => {
    event.preventDefault();
    if (!dialog.open) dialog.showModal();
    load(currentUrl);
  });
  document.getElementById("paper-overview-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", cancel); // Escape keeps the native closing behavior.
  dialog.addEventListener("close", () => { cancel(); opener.focus(); });
  retry.addEventListener("click", () => load(currentUrl));
  // Read-only HUD inputs must not clear or create genuine research draft guards.
  body.addEventListener("input", event => event.stopPropagation());
  body.addEventListener("submit", event => {
    const form = event.target.closest("form[data-hud-filter]");
    if (!form) return;
    event.preventDefault();
    event.stopPropagation();
    const url = new URL(dialog.dataset.papersUrl, window.location.origin);
    url.search = new URLSearchParams(new FormData(form));
    load(url);
  });
  body.addEventListener("click", event => {
    const link = event.target.closest("a[data-hud-page]");
    if (!link) return;
    event.preventDefault();
    load(new URL(link.getAttribute("href"), currentUrl));
  });
})();
