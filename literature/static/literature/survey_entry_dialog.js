// ponytail: native dialog wraps the existing Django editor; links still work without JavaScript.
(() => {
  const dialog = document.getElementById("survey-entry-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  const frame = document.getElementById("survey-entry-frame");
  const heading = document.getElementById("survey-entry-dialog-title");
  document.querySelectorAll("a[data-survey-entry]").forEach(link => {
    link.addEventListener("click", event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const url = new URL(link.href, window.location.origin);
      if (url.origin !== window.location.origin) return;
      event.preventDefault();
      url.searchParams.set("popup", "1");
      heading.textContent = link.dataset.surveyTitle;
      frame.title = link.dataset.surveyTitle;
      frame.src = url.href;
      dialog.showModal();
    });
  });
  dialog.addEventListener("close", () => { frame.src = "about:blank"; });
})();
