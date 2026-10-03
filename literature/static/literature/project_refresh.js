// ponytail: native focus/visibility revalidation for the same project only; drafts never auto-wiped.
(() => {
  const marker = document.querySelector("[data-project-revision-url]");
  if (!marker) return;
  const url = marker.dataset.projectRevisionUrl;
  const initial = marker.dataset.revision || "";
  let known = initial;
  let notice = null;
  function isDirty() {
    return document.querySelector("form[data-dirty='true']") !== null
      || (document.querySelector("form") !== null && document.activeElement
        && document.activeElement.form !== undefined
        && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName || ""));
  }
  function showNotice() {
    if (notice) return;
    notice = document.createElement("div");
    notice.setAttribute("role", "status");
    notice.style.cssText = "margin:1rem 0;padding:.7rem .9rem;border:1px solid;border-radius:.55rem;";
    const text = document.createElement("span");
    text.textContent = "This project changed in another tab. Your drafts are kept. ";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Reload to see updates (discards unsaved drafts)";
    button.addEventListener("click", () => window.location.reload());
    notice.append(text, button);
    const main = document.getElementById("main-content");
    if (main) main.prepend(notice);
  }
  async function revalidate() {
    if (document.visibilityState === "hidden") return;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      const data = await response.json();
      if (!known) { known = data.revision; return; }
      if (data.revision && data.revision !== known) {
        if (isDirty()) showNotice();
        else window.location.reload();
      }
    } catch (_) { /* ponytail: offline/stale reads fail silent; next focus retries. */ }
  }
  document.addEventListener("visibilitychange", revalidate);
  window.addEventListener("focus", revalidate);
  document.addEventListener("input", (event) => {
    if (event.target && event.target.form) event.target.form.dataset.dirty = "true";
  });
  document.addEventListener("submit", (event) => {
    if (event.target && event.target.tagName === "FORM") event.target.dataset.dirty = "false";
  });
})();
