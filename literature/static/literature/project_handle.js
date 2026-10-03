/* Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group. */
/* WP01.05 tab-scoped project handle: attach ownership, explicit Exit only.
   Unload/pagehide never releases: a tab moving to its next project page must
   not close the worker. Expiry is a hint; durability is committed SQLite.
   Web Locks distinguish a live duplicate from navigation; editor frames use
   the parent's handle. No unload event is needed for saved-data durability. */
(() => {
  "use strict";
  const match = window.location.pathname.match(/^(\/p\/[^/]+\/[^/]+)\/project\/(\d+)/);
  if (!match) return;
  const prefix = match[1];
  const projectId = match[2];
  const storeKey = "wp_tabs:" + prefix;
  const managerKey = "wp_manager_base";
  const params = new URLSearchParams(window.location.search);
  const managerParam = params.get("manager");
  let ready = false;
  // ponytail: stored strings only, no global active-project cookie.
  if (managerParam) {
    try { window.sessionStorage.setItem(managerKey, decodeURIComponent(managerParam)); } catch (_) {}
  }

  // ponytail: exact worker CSRF cookie for this UUID/epoch; never a global
  // fallback, so one tab cannot spend another worker's token.
  function csrfToken() {
    const uuid = prefix.split("/")[2] || "";
    const short = uuid.replace(/-/g, "").slice(0, 8);
    const want = "wpcsrft_" + short;
    const parts = document.cookie.split(";");
    for (const part of parts) {
      const eq = part.indexOf("=");
      const name = (eq < 0 ? part : part.slice(0, eq)).trim();
      if (name === want) return decodeURIComponent((eq < 0 ? "" : part.slice(eq + 1)).trim());
    }
    return "";
  }

  async function ensureCsrf() {
    if (csrfToken()) return;
    try {
      await fetch(prefix + "/project/" + projectId + "/lifecycle/status/", {
        method: "GET",
        credentials: "same-origin",
      });
    } catch (_) { /* ponytail: best effort; the POST will fail closed. */ }
  }

  function readTabs() {
    try {
      const raw = window.sessionStorage.getItem(storeKey);
      if (!raw) return {};
      const data = JSON.parse(raw);
      return data && typeof data === "object" ? data : {};
    } catch (_) { return {}; }
  }

  function writeTabs(data) {
    try { window.sessionStorage.setItem(storeKey, JSON.stringify(data)); } catch (_) {}
  }

  async function attach(nav) {
    await ensureCsrf();
    const stored = readTabs();
    const body = new URLSearchParams({
      handle_id: stored.h || "",
      tab_token: stored.t || "",
      nav: nav,
    });
    const response = await fetch(prefix + "/project/" + projectId + "/lifecycle/attach/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!response.ok) throw new Error("attach failed: " + response.status);
    const data = await response.json();
    writeTabs({ h: data.handle_id, t: data.tab_token });
    ready = true;
    return data;
  }

  async function heartbeat(handle) {
    const body = new URLSearchParams({ handle_id: handle });
    const response = await fetch(prefix + "/project/" + projectId + "/lifecycle/heartbeat/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    return response.status;
  }

  async function release(handle) {
    const body = new URLSearchParams({ handle_id: handle });
    const response = await fetch(prefix + "/project/" + projectId + "/lifecycle/release/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
    });
    if (!response.ok) throw new Error("release failed: " + response.status);
    const data = await response.json();
    if (data.released !== true) throw new Error("project handle was not released");
    writeTabs({});
  }

  // ponytail: every worker form carries its tab's handle as a hidden field,
  // so the middleware can enforce ownership on each mutation, fetch or form.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || form.tagName !== "FORM") return;
    if ((form.method || "get").toLowerCase() !== "post") return;
    if (!window.__wpHandle()) {
      event.preventDefault();
      note("This project tab is still opening; retry your save when ready.");
      return;
    }
    if (form.querySelector('input[name="wp_handle"]')) return;
    const stored = readTabs();
    if (!stored.h) return;
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "wp_handle";
    input.value = stored.h;
    form.appendChild(input);
  }, true);

  // ponytail: JSON fetch mutations (screening decisions) read the same tab
  // handle through this accessor instead of duplicating storage logic.
  window.__wpHandle = () => ready ? readTabs().h || "" : "";
  // An editor iframe is part of this tab, not another project lease.
  let editorFrame = false;
  try { editorFrame = window.parent !== window && !!window.parent.__wpHandle; } catch (_) {}
  if (editorFrame) window.__wpHandle = () => window.parent.__wpHandle();

  function note(text) {
    let bar = document.getElementById("wp-handle-note");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "wp-handle-note";
      bar.setAttribute("role", "alert");
      bar.style.cssText = "margin:1rem 0;padding:.7rem .9rem;border:1px solid;border-radius:.55rem;";
      const main = document.getElementById("main-content");
      if (main) main.prepend(bar);
    }
    bar.textContent = text;
  }

  function managerBase() {
    try { return window.sessionStorage.getItem(managerKey) || ""; } catch (_) { return ""; }
  }

  // ponytail: dirty-form guard distinguishes drafts (browser-only) from
  // committed records (already POSTed SQLite). Exit confirms only on drafts.
  let dirty = false;
  window.__wpDirty = () => dirty;
  window.__wpDiscard = () => { dirty = false; };
  document.addEventListener("input", (event) => {
    if (event.target && event.target.form) dirty = true;
  });
  document.addEventListener("submit", (event) => { if (!event.defaultPrevented) dirty = false; });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) event.preventDefault();
  });

  async function ensure() {
    try {
      if (!navigator.locks) throw new Error("Web Locks unavailable");
      const stored = readTabs();
      const hold = () => new Promise(() => {});
      if (stored.h) {
        // Native document lifetime locks survive neither navigation nor crash.
        // A copied sessionStorage token in a LIVE duplicate cannot take this lock.
        await navigator.locks.request("wp-tab:" + prefix + stored.h, { ifAvailable: true }, async (lock) => {
          const data = await attach(lock ? "same_tab" : "navigate");
          if (lock && data.handle_id === stored.h) return hold();
          return navigator.locks.request("wp-tab:" + prefix + data.handle_id, hold);
        });
      } else {
        const data = await attach("navigate");
        await navigator.locks.request("wp-tab:" + prefix + data.handle_id, hold);
      }
    } catch (_) {
      note("Could not open this project tab. Committed records are safe; reopen it from Projects.");
      return;
    }
  }

  async function exitProject(back = false) {
    if (!ready) {
      if (back) window.history.forward();
      note("This project tab is still opening; retry Exit when ready.");
      return;
    }
    let hasDraft = dirty;
    for (const frame of document.querySelectorAll("iframe")) {
      try { hasDraft ||= !!frame.contentWindow.__wpDirty?.(); } catch (_) {}
    }
    if (hasDraft && !window.confirm("Unsaved form drafts will be discarded. Committed records are safe. Exit anyway?")) {
      if (back) window.history.forward();
      return;
    }
    const stored = readTabs();
    try {
      if (stored.h) await release(stored.h);
    } catch (_) {
      if (back) window.history.forward();
      note("Could not release this tab. Your draft and project handle remain; retry Exit.");
      return;
    }
    dirty = false; // Confirmed exit only: suppress a second beforeunload prompt.
    for (const frame of document.querySelectorAll("iframe")) {
      try { frame.contentWindow.__wpDiscard?.(); } catch (_) {}
    }
    const base = managerBase();
    if (back) window.history.back();
    else if (base) window.location.href = base + "/";
    else note("Exited this project tab. Committed records are safe; close the tab or reopen from Projects.");
  }

  if (editorFrame) return;
  // A same-document history boundary lets ordinary Back release before crossing
  // origins. Internal Back stays in the project; no unload/beacon release.
  if (managerParam && !window.history.state?.wpProject) {
    window.history.replaceState({ wpProject: prefix, wpBoundary: true }, "");
    window.history.pushState({ wpProject: prefix }, "");
  }
  window.addEventListener("popstate", (event) => {
    if (event.state?.wpBoundary && event.state.wpProject === prefix) exitProject(true);
  });

  let button = document.getElementById("wp-exit");
  if (!button) {
    const nav = document.querySelector(".site-nav");
    if (nav) {
      button = document.createElement("a");
      button.id = "wp-exit";
      button.href = "#";
      button.textContent = "\u2190 Projects (Exit)";
      button.setAttribute("aria-label", "Exit this project tab and return to Projects");
      nav.prepend(button);
    }
  }
  if (button) button.addEventListener("click", (event) => { event.preventDefault(); exitProject(); });
  document.querySelectorAll("[data-project-exit]").forEach((link) => {
    link.addEventListener("click", (event) => { event.preventDefault(); exitProject(); });
  });

  ensure();
  window.setInterval(async () => {
    const stored = readTabs();
    if (!stored.h) return;
    const status = await heartbeat(stored.h);
    if (status === 410 || status === 404) {
      writeTabs({});
      note("This tab's project handle expired. Committed records are safe; reopen the project from Projects to continue.");
    }
  }, 30000);
})();
