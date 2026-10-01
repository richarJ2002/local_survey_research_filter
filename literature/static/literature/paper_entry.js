// Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
(() => {
  const form = document.getElementById("paper-entry-form");
  if (!form) return;
  const keyword = form.elements.search_keyword;
  const newFields = document.getElementById("new-keyword-fields");
  const title = form.elements.title_decision;
  const abstract = form.elements.abstract_decision;

  function updateKeywordFields() {
    const creating = keyword.value === "NEW";
    newFields.hidden = !creating;
    for (const field of [form.elements.new_keyword, form.elements.new_keyword_search]) {
      field.disabled = !creating;
      field.required = creating;
    }
  }

  function updateAbstractOptions() {
    const passed = title.value === "ACCEPT";
    for (const option of abstract.options) {
      if (option.value) {
        option.disabled = passed ? option.value === "NOT_QUEUED" : option.value !== "NOT_QUEUED";
      }
    }
    if (title.value && !passed) {
      abstract.value = "NOT_QUEUED";
    } else if (passed && abstract.value === "NOT_QUEUED") {
      abstract.value = ""; // Require an explicit choice when Abstract becomes applicable.
    }
  }

  keyword.addEventListener("change", updateKeywordFields);
  title.addEventListener("change", updateAbstractOptions);
  updateKeywordFields();
  updateAbstractOptions();
  // Restore controls correctly when the browser returns through its back/forward cache.
  window.addEventListener("pageshow", () => {
    updateKeywordFields();
    updateAbstractOptions();
  });
})();
