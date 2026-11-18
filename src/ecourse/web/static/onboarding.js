(() => {
  const csrf = document.body.dataset.csrf || "";
  const tour = document.querySelector("#tutorial");
  const gate = document.querySelector("#first-sync");
  let checking = false;

  async function firstSyncDone() {
    try {
      const response = await fetch("/api/v1/initial-sync", {headers: {"Accept": "application/json"}});
      if (!response.ok) return {done: false};
      return (await response.json()).data || {done: false};
    } catch { return {done: false}; }
  }

  async function markTutorialComplete() {
    try {
      await fetch("/api/v1/tutorial/complete", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: "{}"});
    } catch {}
  }

  function showGate() {
    // Rendered by the server only after the tutorial is done; build it on the fly when the
    // tutorial finishes while the first sync is still running.
    const template = document.querySelector("#first-sync-template");
    if (!template) { location.reload(); return; }
    document.body.append(template.content.cloneNode(true));
    startPolling(document.querySelector("#first-sync"));
  }

  async function startPolling(element) {
    if (!element) return;
    element.querySelector("[data-tour=enter]")?.addEventListener("click", () => element.remove());
    while (document.body.contains(element)) {
      const status = await firstSyncDone();
      if (status.done) { location.href = "/"; return; }
      element.querySelector("#first-sync-attention")?.toggleAttribute("hidden", !status.needs_attention);
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
  }

  if (gate) startPolling(gate);
  if (!tour) return;
  const replay = tour.dataset.done === "1";

  const slides = [...tour.querySelectorAll(".tour-slide")];
  const dots = tour.querySelector(".tour-dots");
  const next = tour.querySelector("[data-tour=next]");
  const back = tour.querySelector("[data-tour=back]");
  const title = tour.querySelector("#tour-title");
  const nextLabel = next.textContent;
  let index = 0;
  slides.forEach(() => dots.append(document.createElement("i")));

  function show(position) {
    index = Math.max(0, Math.min(slides.length - 1, position));
    slides.forEach((slide, i) => slide.classList.toggle("active", i === index));
    [...dots.children].forEach((dot, i) => dot.classList.toggle("on", i === index));
    back.hidden = index === 0;
    next.textContent = index === slides.length - 1 ? next.dataset.lastLabel : nextLabel;
    title.textContent = slides[index].dataset.title || "";
  }

  async function finish() {
    if (replay) { tour.hidden = true; show(0); return; }
    next.disabled = true;
    await markTutorialComplete();
    const status = await firstSyncDone();
    if (status.done) { location.href = "/"; return; }  // sync finished while reading: no loading screen
    tour.remove();
    showGate();
  }

  tour.addEventListener("click", event => {
    const action = event.target.closest("[data-tour]")?.dataset.tour;
    if (action === "next") index === slides.length - 1 ? finish() : show(index + 1);
    else if (action === "back") show(index - 1);
    else if (action === "skip") finish();
  });
  document.addEventListener("keydown", event => {
    if (!document.body.contains(tour) || tour.hidden) return;
    if (event.key === "ArrowRight") next.click();
    else if (event.key === "ArrowLeft" && index > 0) show(index - 1);
    else if (event.key === "Escape") finish();
  });
  document.querySelectorAll("[data-action=replay-tutorial]").forEach(button => button.addEventListener("click", () => { show(0); tour.hidden = false; next.focus(); }));
  show(0);
  if (!tour.hidden) next.focus();
})();
