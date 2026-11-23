(() => {
  const csrf = document.body.dataset.csrf || "";
  const locale = document.body.dataset.locale || "zh-Hant";
  const isEn = locale === "en";
  const tour = document.querySelector("#tutorial");
  const gate = document.querySelector("#first-sync");

  const platformLabel = name => name === "elearn" ? "eLearn" : name === "eeclass" ? "eeclass" : name;
  const EXP_PROGRESS_K = 2.5;
  const EXP_PROGRESS_DENOM = Math.expm1(EXP_PROGRESS_K);
  // Normalized exponential ease-in: f(x) = (e^(k*x) - 1) / (e^k - 1)
  function expProgress(rawPercent, done = false) {
    if (done) return 100;
    const x = Math.max(0, Math.min(1, Number(rawPercent || 0) / 100));
    if (x <= 0) return 0;
    if (x >= 1) return 100;
    return Math.min(99, Math.max(1, Math.round((Math.expm1(EXP_PROGRESS_K * x) / EXP_PROGRESS_DENOM) * 100)));
  }

  function phaseText(p) {
    if (p.done) {
      if (p.state === "failed" || p.state === "cancelled" || p.phase === "failed") {
        return isEn ? "Sync incomplete" : "同步未完成";
      }
      if (p.state === "partial") return isEn ? "Partially complete" : "部分完成";
      return isEn ? "Done" : "已完成";
    }
    if (p.needs_attention || p.phase === "waiting_user") {
      return isEn ? "Waiting for verification code" : "等待輸入驗證碼";
    }
    if (p.phase === "details") {
      if (p.items_total > 0) {
        return isEn
          ? `Loading coursework (${p.items_done} / ${p.items_total})`
          : `正在讀取作業與公告（${p.items_done} / ${p.items_total}）`;
      }
      return isEn ? "Loading coursework…" : "正在讀取作業與公告…";
    }
    if (p.phase === "scanning") {
      if (p.courses_total > 0) {
        return isEn
          ? `Scanning courses (${p.courses_done} / ${p.courses_total})`
          : `正在掃描課程（${p.courses_done} / ${p.courses_total}）`;
      }
      return isEn ? "Scanning courses…" : "正在掃描課程…";
    }
    if (p.phase === "discovering") return isEn ? "Loading course list…" : "正在讀取課程清單…";
    if (p.phase === "authenticating") return isEn ? "Signing in…" : "正在登入平台…";
    return isEn ? "Waiting to start…" : "準備開始…";
  }

  function summaryText(status) {
    if (status.done) return isEn ? "All done! Opening overview…" : "抓取完成，正在進入總覽…";
    if (status.needs_attention) return isEn ? "Verification code required" : "需要輸入驗證碼";
    const platforms = status.platforms || [];
    const active = platforms.find(p => !p.done);
    if (!active) return isEn ? "Preparing…" : "準備中…";
    const label = platformLabel(active.platform);
    return `${label} · ${phaseText(active)}`;
  }

  function renderProgress(element, status, highWater) {
    if (!element || !status) return;
    const rawOverall = typeof status.percent === "number" ? status.percent : 0;
    const mappedOverall = expProgress(rawOverall, Boolean(status.done));
    const overall = status.done ? 100 : Math.min(99, Math.max(highWater.overall, mappedOverall));
    highWater.overall = overall;

    const bar = element.querySelector("#first-sync-bar");
    const pct = element.querySelector("#first-sync-percent");
    const summary = element.querySelector("#first-sync-summary");
    if (bar) {
      bar.value = overall;
      bar.classList.toggle("is-done", Boolean(status.done));
    }
    if (pct) pct.textContent = `${overall}%`;
    if (summary) summary.textContent = summaryText(status);

    const list = element.querySelector("#first-sync-platforms");
    if (list && Array.isArray(status.platforms)) {
      status.platforms.forEach(p => {
        let row = list.querySelector(`[data-platform="${CSS.escape(p.platform)}"]`);
        if (!row) {
          row = document.createElement("li");
          row.className = "sync-platform";
          row.dataset.platform = p.platform;
          const top = document.createElement("div");
          top.className = "sync-platform-row";
          const name = document.createElement("span");
          name.className = "sync-platform-name";
          name.textContent = platformLabel(p.platform);
          const detail = document.createElement("span");
          detail.className = "sync-platform-detail";
          detail.dataset.role = "detail";
          top.append(name, detail);
          const subBar = document.createElement("progress");
          subBar.className = "sync-progress sync-progress-sm";
          subBar.max = 100;
          row.append(top, subBar);
          list.append(row);
        }
        const prev = highWater.platforms.get(p.platform) || 0;
        const mappedPlatform = expProgress(Number(p.percent || 0), Boolean(p.done));
        const pPct = p.done ? 100 : Math.min(99, Math.max(prev, mappedPlatform));
        highWater.platforms.set(p.platform, pPct);
        const detail = row.querySelector('[data-role="detail"]');
        if (detail) detail.textContent = phaseText(p);
        const subBar = row.querySelector("progress");
        if (subBar) {
          subBar.value = pPct;
          subBar.classList.toggle("is-done", Boolean(p.done));
        }
      });
    }
    element.querySelector("#first-sync-attention")?.toggleAttribute("hidden", !status.needs_attention);
  }

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

  function showGate(initialStatus = null) {
    // Rendered by the server only after the tutorial is done; build it on the fly when the
    // tutorial finishes while the first sync is still running.
    const template = document.querySelector("#first-sync-template");
    if (!template) { location.reload(); return; }
    document.body.append(template.content.cloneNode(true));
    startPolling(document.querySelector("#first-sync"), initialStatus);
  }

  async function startPolling(element, initialStatus = null) {
    if (!element) return;
    element.querySelector("[data-tour=enter]")?.addEventListener("click", () => element.remove());
    const highWater = {overall: 0, platforms: new Map()};
    if (initialStatus) renderProgress(element, initialStatus, highWater);
    while (document.body.contains(element)) {
      const status = await firstSyncDone();
      renderProgress(element, status, highWater);
      if (status.done) {
        await new Promise(resolve => setTimeout(resolve, 250));
        location.href = "/";
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 600));
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
    showGate(status);
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
