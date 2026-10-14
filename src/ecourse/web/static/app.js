const csrf = document.body.dataset.csrf || "";
const locale = document.body.dataset.locale || "zh-Hant";
const englishMessages = {
  "操作處理中，請稍候。": "An operation is already in progress. Please wait.",
  "重新嘗試": "Try again", "校方登入驗證碼": "School verification code", "驗證碼": "Verification code",
  "提交驗證碼": "Submit code", "換一張": "New image", "答案已送出，正在驗證…": "Answer sent. Verifying…",
  "自動辨識未完成，請輸入圖片中的驗證碼。": "Automatic recognition was incomplete. Enter the code shown in the image.",
  "登入完成，資料同步已繼續。": "Signed in. Data sync has resumed.", "正在登入，自動辨識驗證碼中…": "Signing in and recognizing the verification code…",
  "目前沒有可同步的平台。": "There are no platforms available to sync.", "同步進行中；首次抓取所有課程可能需要幾分鐘…": "Syncing. The first full course fetch may take a few minutes…",
  "同步正在等待驗證碼，請到連接設定完成驗證。": "Sync is waiting for a verification code. Complete verification in Connection settings.", "前往連接設定": "Open Connection settings",
  "同步未完成，請查看系統狀態。": "Sync did not complete. Check System status.", "同步部分完成，部分資料未能更新。": "Sync partially completed; some data could not be updated.", "同步完成。": "Sync complete.",
  "新增手動事件": "Add manual event", "編輯手動事件": "Edit manual event", "公告已標為已讀。": "Announcement marked as read.",
  "帳密已更新，正在重新連接平台。": "Credentials updated. Reconnecting platforms.", "提醒設定已儲存。": "Reminder settings saved."
};
const tr = message => locale === "en" ? (englishMessages[message] || message) : message;
const pendingWrites = new Set();
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function notice(message, error = false) {
  const element = document.querySelector("#notice");
  if (!element) return;
  element.textContent = tr(message);
  element.classList.toggle("error", error);
  element.hidden = false;
}

function saveFlash(message, error = false) {
  try { sessionStorage.setItem("ecourse_flash", JSON.stringify({message, error})); } catch {}
}

try {
  const saved = sessionStorage.getItem("ecourse_flash");
  if (saved) {
    sessionStorage.removeItem("ecourse_flash");
    const flash = JSON.parse(saved);
    notice(flash.message, Boolean(flash.error));
  }
} catch {}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const write = !["GET", "HEAD"].includes(method);
  const key = `${method}:${path}:${options.body || ""}`;
  if (write && pendingWrites.has(key)) throw new Error(tr("操作處理中，請稍候。"));
  if (write) pendingWrites.add(key);
  try {
    const headers = {"Content-Type": "application/json", ...(options.headers || {})};
    if (write) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(path, {...options, method, headers});
    let body = {};
    try { body = await response.json(); } catch {}
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : "";
      throw new Error(body.message || detail || `操作失敗（HTTP ${response.status}），請稍後再試。`);
    }
    return body.data;
  } finally {
    if (write) pendingWrites.delete(key);
  }
}

async function withForm(form, operation) {
  const button = form.querySelector("button[type=submit]");
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try { return await operation(); }
  finally { if (button) button.disabled = false; }
}

const attemptTokens = new Map();
const challengeBox = platform => document.querySelector(`[data-challenge-platform="${CSS.escape(platform)}"]`);
function challengeText(platform, text) {
  const box = challengeBox(platform);
  if (!box) return;
  box.replaceChildren();
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  box.append(paragraph);
}

function retryChallenge(platform) {
  const box = challengeBox(platform);
  if (!box || !platform) return;
  const button = document.createElement("button");
  button.textContent = tr("重新嘗試");
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api(`/api/v1/connections/${platform}/retry`, {method: "POST", body: "{}"});
      await beginAttempt(result.attempt_id, platform);
    } catch (error) {
      notice(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  box.append(button);
}

function renderChallenge(attemptId, platform) {
  const box = challengeBox(platform);
  if (!box) return;
  box.replaceChildren();
  const image = document.createElement("img");
  image.src = `/api/v1/login-attempts/${encodeURIComponent(attemptId)}/challenge-image`;
  image.alt = tr("校方登入驗證碼");
  image.className = "captcha";
  const form = document.createElement("form");
  const label = document.createElement("label");
  label.textContent = tr("驗證碼");
  const input = document.createElement("input");
  input.required = true;
  input.autocomplete = "off";
  label.append(input);
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = tr("提交驗證碼");
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.className = "secondary";
  refresh.textContent = tr("換一張");
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    try {
      await api(`/api/v1/login-attempts/${encodeURIComponent(attemptId)}/refresh`, {method: "POST", body: "{}"});
      await beginAttempt(attemptId, platform);
    } catch (error) { notice(error.message, true); }
    finally { refresh.disabled = false; }
  });
  form.append(label, submit, refresh);
  form.addEventListener("submit", event => {
    event.preventDefault();
    withForm(form, async () => {
      try {
        await api(`/api/v1/login-attempts/${encodeURIComponent(attemptId)}/answer`, {method: "POST", body: JSON.stringify({answer: input.value, request_id: crypto.randomUUID()})});
        challengeText(platform, tr("答案已送出，正在驗證…"));
        await delay(1200);
        await beginAttempt(attemptId, platform);
      } catch (error) { notice(error.message, true); }
    });
  });
  box.append(image, form);
}

function sendExtensionHandoff(handoffToken) {
  return new Promise((resolve, reject) => {
    const requestId = crypto.randomUUID();
    const timeout = setTimeout(() => {
      window.removeEventListener("message", receive);
      reject(new Error("未侦测到 eCourse Brave 扩充功能，请先安装或重新载入扩充功能。"));
    }, 8000);
    function receive(event) {
      if (event.source !== window || event.origin !== location.origin) return;
      if (event.data?.type !== "ecourse-extension-result" || event.data?.requestId !== requestId) return;
      clearTimeout(timeout);
      window.removeEventListener("message", receive);
      if (event.data.ok) resolve(event.data);
      else reject(new Error(event.data.message || "扩充功能无法开启平台。"));
    }
    window.addEventListener("message", receive);
    window.postMessage({type: "ecourse-extension-open", requestId, handoffToken}, location.origin);
  });
}

async function openManagedPlatform(platform, targetUrl = null) {
  const body = targetUrl ? {target_url: targetUrl} : {};
  const result = await api(`/api/v1/connections/${encodeURIComponent(platform)}/extension-handoff`, {method: "POST", body: JSON.stringify(body)});
  if (result.status === "reauthentication_required") {
    try { sessionStorage.setItem("ecourse_open_after_login", JSON.stringify({platform, targetUrl})); } catch {}
    notice("平台登入已失效，正在自動重新登入；完成後會開啟專用瀏覽器。");
    await beginAttempt(result.attempt_id, platform);
    return;
  }
  await sendExtensionHandoff(result.handoff_token);
  notice(`已在目前的 Brave 視窗開啟 ${platform === "elearn" ? "eLearn" : "eeclass"} 分頁。`);
}

async function beginAttempt(attemptId, platform) {
  const token = (attemptTokens.get(platform) || 0) + 1;
  attemptTokens.set(platform, token);
  return watchAttempt(attemptId, platform, token);
}

async function watchAttempt(attemptId, platform, token) {
  if (!attemptId || !challengeBox(platform)) return;
  try {
    while (true) {
      if (attemptTokens.get(platform) !== token) return;
      const attempt = await api(`/api/v1/login-attempts/${encodeURIComponent(attemptId)}`);
      if (attemptTokens.get(platform) !== token) return;
      if (attempt.state === "challenge_required") {
        renderChallenge(attemptId, platform);
        const box = challengeBox(platform);
        const hint = document.createElement("p");
        hint.className = "fine";
        hint.textContent = tr("自動辨識未完成，請輸入圖片中的驗證碼。");
        box?.prepend(hint);
        return;
      }
      if (["connected", "succeeded", "complete"].includes(attempt.state)) {
        try {
          const pending = JSON.parse(sessionStorage.getItem("ecourse_open_after_login") || "null");
          if (pending?.platform === platform) {
            sessionStorage.removeItem("ecourse_open_after_login");
            await openManagedPlatform(platform, pending.targetUrl);
            return;
          }
        } catch { sessionStorage.removeItem("ecourse_open_after_login"); }
        saveFlash(tr("登入完成，資料同步已繼續。"));
        location.reload();
        return;
      }
      if (["failed", "credentials_invalid", "expired"].includes(attempt.state)) {
        challengeText(platform, `登入未完成：${attempt.error_code || attempt.state}`);
        retryChallenge(platform);
        return;
      }
      challengeText(platform, tr("正在登入，自動辨識驗證碼中…"));
      await delay(1500);
    }
  } catch (error) {
    if (attemptTokens.get(platform) !== token) return;
    challengeText(platform, error.message);
    retryChallenge(platform);
  }
}

async function watchSync(jobs) {
  if (!jobs.length) { notice("目前沒有可同步的平台。", true); return; }
  const startedAt = Date.now();
  notice("同步進行中；首次抓取所有課程可能需要幾分鐘…");
  while (true) {
    let states;
    try {
      states = await Promise.all(jobs.map(job => api(`/api/v1/sync/jobs/${encodeURIComponent(job.id)}`)));
    } catch (error) {
      notice(error.message, true);
      throw error;
    }
    if (states.some(job => job.state === "waiting_user")) {
      notice("同步正在等待驗證碼，請到連接設定完成驗證。", true);
      const link = document.createElement("a");
      link.href = "/settings/connections";
      link.textContent = tr("前往連接設定");
      link.className = "button secondary";
      document.querySelector("#notice")?.append(" ", link);
      return;
    }
    if (states.every(job => ["succeeded", "partial", "failed", "cancelled"].includes(job.state))) {
      const partial = states.some(job => job.state === "partial");
      const failed = states.some(job => ["failed", "cancelled"].includes(job.state));
      saveFlash(failed ? "同步未完成，請查看系統狀態。" : partial ? "同步部分完成，部分資料未能更新。" : "同步完成。", failed || partial);
      location.reload();
      return;
    }
    const elapsed = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
    notice(`同步進行中（已過 ${elapsed} 秒）；可離開此頁，背景仍會繼續。`);
    await delay(1500);
  }
}

const login = document.querySelector("#login-form");
async function watchSchoolLogin() {
  const box = document.querySelector("#login-challenge");
  while (box) {
    const state = await api("/api/v1/auth/school/status");
    if (state.state === "succeeded") {
      await api("/api/v1/auth/school/complete", {method: "POST", body: "{}"});
      location.href = "/courses";
      return;
    }
    if (state.state === "challenge_required") {
      box.replaceChildren();
      const hint = document.createElement("p");
      hint.textContent = "請輸入校方驗證碼。";
      const image = document.createElement("img");
      image.src = "/api/v1/auth/school/challenge-image";
      image.alt = "校方登入驗證碼";
      image.className = "captcha";
      const form = document.createElement("form");
      const input = document.createElement("input");
      input.required = true;
      input.autocomplete = "off";
      input.setAttribute("aria-label", "驗證碼");
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.textContent = "送出驗證碼";
      form.append(input, submit);
      form.addEventListener("submit", event => {
        event.preventDefault();
        withForm(form, async () => {
          try {
            await api("/api/v1/auth/school/challenge-answer", {method: "POST", body: JSON.stringify({answer: input.value, request_id: crypto.randomUUID()})});
            box.textContent = "正在驗證…";
            await watchSchoolLogin();
          } catch (error) { box.textContent = error.message; }
        });
      });
      box.append(hint, image, form);
      return;
    }
    if (["failed", "credentials_invalid", "expired"].includes(state.state)) throw new Error("登入資料不正確或校方暫時無法驗證。");
    box.textContent = "正在驗證校務帳號…";
    await delay(1500);
  }
}
login?.addEventListener("submit", event => {
  event.preventDefault();
  withForm(login, async () => {
    const error = document.querySelector("#login-error");
    error.textContent = "";
    try {
      const result = await api("/api/v1/auth/login", {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(login)))});
      if (result.signed_in) location.href = "/";
      else await watchSchoolLogin();
    } catch (reason) { error.textContent = reason.message; }
  });
});

document.querySelectorAll("[data-dialog]").forEach(button => button.addEventListener("click", () => {
  const dialog = document.getElementById(button.dataset.dialog);
  const form = dialog.querySelector("form");
  form?.reset();
  if (form) delete form.dataset.id;
  const heading = dialog.querySelector("h2");
  if (heading) heading.textContent = tr("新增手動事件");
  dialog.showModal();
}));
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));

function taipeiInput(iso) {
  if (!iso) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(new Date(iso));
  const get = type => parts.find(part => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action === "edit-event") {
    const form = document.querySelector("#event-form");
    form.dataset.id = button.dataset.id;
    form.elements.title.value = button.dataset.title;
    form.elements.date_precision.value = button.dataset.precision;
    form.elements.starts_at.value = taipeiInput(button.dataset.starts);
    form.elements.event_date.value = button.dataset.date;
    document.querySelector("#event-dialog h2").textContent = tr("編輯手動事件");
    document.querySelector("#event-dialog").showModal();
    return;
  }
  button.disabled = true;
  try {
    if (action === "logout") { await api("/api/v1/auth/logout", {method: "POST", body: "{}"}); location.href = "/login"; }
    else if (action === "sync") await watchSync(await api("/api/v1/sync", {method: "POST", body: "{}"}));
    else if (action === "open-platform") await openManagedPlatform(button.dataset.platform, button.dataset.targetUrl || null);
    else if (action === "connect" || action === "retry-connect") {
      const platform = button.dataset.platform;
      const result = action === "connect" ? await api(`/api/v1/connections/${platform}/connect`, {method: "POST", body: JSON.stringify({credential_id: button.dataset.credential})}) : await api(`/api/v1/connections/${platform}/retry`, {method: "POST", body: "{}"});
      await beginAttempt(result.attempt_id, platform);
    } else if (action === "course-none") {
      const sources = button.querySelectorAll("[data-none-source]");
      for (const source of sources) {
        await api(`/api/v1/courses/${encodeURIComponent(button.dataset.course)}/selection`, {method: "PUT", body: JSON.stringify({source_id: source.dataset.noneSource, selection_state: "disabled", expected_version: Number(source.dataset.version)})});
      }
      location.reload();
    } else if (action === "course-selection") { await api(`/api/v1/courses/${encodeURIComponent(button.dataset.course)}/selection`, {method: "PUT", body: JSON.stringify({source_id: button.dataset.source, selection_state: button.dataset.state, expected_version: Number(button.dataset.version)})}); location.reload(); }
    else if (action === "read-notification") { await api(`/api/v1/notifications/${encodeURIComponent(button.dataset.id)}/read`, {method: "PUT", body: "{}"}); location.reload(); }
    else if (action === "read-announcement") { await api(`/api/v1/announcements/${encodeURIComponent(button.dataset.id)}/read`, {method: "PUT", body: "{}"}); notice("公告已標為已讀。"); }
    else if (action === "delete-event" && confirm("確定刪除這個手動事件？")) { await api(`/api/v1/manual-events/${encodeURIComponent(button.dataset.id)}`, {method: "DELETE"}); location.reload(); }
    else if (action === "disconnect" && confirm(`確定解除 ${button.dataset.platform} 連接？`)) { await api(`/api/v1/connections/${button.dataset.platform}`, {method: "DELETE"}); location.reload(); }
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

const credentials = document.querySelector("#credentials-form");
credentials?.addEventListener("submit", event => {
  event.preventDefault();
  withForm(credentials, async () => {
    try {
      const saved = await api("/api/v1/settings/credentials", {method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(credentials)))});
      credentials.reset();
      const attempts = Object.entries(saved.attempts || {});
      notice("帳密已更新，正在重新連接平台。");
      await Promise.all(attempts.map(([platform, result]) => beginAttempt(result.attempt_id, platform)));
    } catch (error) { notice(error.message, true); }
  });
});

const eventForm = document.querySelector("#event-form");
eventForm?.addEventListener("submit", event => {
  event.preventDefault();
  withForm(eventForm, async () => {
    const raw = Object.fromEntries(new FormData(eventForm));
    const body = {title: raw.title, kind: "exam", date_precision: raw.date_precision, starts_at: raw.starts_at ? `${raw.starts_at}:00+08:00` : null, event_date: raw.event_date || null};
    try {
      const id = eventForm.dataset.id;
      await api(id ? `/api/v1/manual-events/${encodeURIComponent(id)}` : "/api/v1/manual-events", {method: id ? "PATCH" : "POST", body: JSON.stringify(body)});
      location.reload();
    } catch (error) { notice(error.message, true); }
  });
});

const reminder = document.querySelector("#reminder-form");
reminder?.addEventListener("submit", event => {
  event.preventDefault();
  withForm(reminder, async () => {
    const form = new FormData(reminder);
    const body = {daily_enabled: form.has("daily_enabled"), daily_time: form.get("daily_time"), weekly_enabled: form.has("weekly_enabled"), weekly_day: 6, weekly_time: form.get("weekly_time"), quiet_start: form.get("quiet_start"), quiet_end: form.get("quiet_end"), lead_hours: String(form.get("lead_hours")).split(",").map(Number).filter(value => Number.isInteger(value) && value > 0)};
    try { await api("/api/v1/settings/reminders", {method: "PUT", body: JSON.stringify(body)}); notice("提醒設定已儲存。"); }
    catch (error) { notice(error.message, true); }
  });
});

const language = document.querySelector("#language-form");
language?.querySelectorAll('[name="locale"]').forEach(input => input.addEventListener("change", async () => {
  if (!input.checked) return;
  input.disabled = true;
  try {
    await api("/api/v1/settings/language", {method: "PUT", body: JSON.stringify({locale: input.value})});
    location.reload();
  } catch (error) {
    input.disabled = false;
    notice(error.message, true);
  }
}));

const search = document.querySelector("#todo-search"), status = document.querySelector("#todo-status"), course = document.querySelector("#todo-course"), filterEmpty = document.querySelector("#todo-filter-empty");
function filterTodos() {
  const query = (search?.value || "").trim().toLocaleLowerCase();
  let visible = 0;
  document.querySelectorAll(".todo").forEach(element => {
    const matches = element.dataset.search.toLocaleLowerCase().includes(query) && (!status?.value || element.dataset.status === status.value) && (!course?.value || element.dataset.course === course.value);
    element.hidden = !matches;
    if (matches) visible += 1;
  });
  if (filterEmpty) filterEmpty.hidden = visible !== 0;
}
search?.addEventListener("input", filterTodos); status?.addEventListener("change", filterTodos); course?.addEventListener("change", filterTodos);
const requestedCourse = new URLSearchParams(location.search).get("course");
if (course && requestedCourse && [...course.options].some(option => option.value === requestedCourse)) { course.value = requestedCourse; filterTodos(); }
document.querySelectorAll(".connection[data-attempt]").forEach(row => { const platform=row.querySelector("[data-platform]")?.dataset.platform;if(row.dataset.attempt&&platform)beginAttempt(row.dataset.attempt,platform); });
const targetParams = new URLSearchParams(location.search), targetId = targetParams.get("item") || targetParams.get("manual");
if (targetId) { const target = document.querySelector(`[data-id="${CSS.escape(targetId)}"]`)?.closest(".todo"); if (target) { target.classList.add("targeted"); target.scrollIntoView({block: "center"}); } }

// Pointer events support both touch and mouse without making course links draggable.
const courseGrid = document.querySelector('[data-course-grid]');
if (courseGrid) {
  const key = `ecourse.course-order.v1:${courseGrid.dataset.owner}`;
  const cards = () => [...courseGrid.querySelectorAll('[data-course-id]')];
  const status = document.querySelector('[data-sort-status]');
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '[]');
    if (Array.isArray(saved)) {
      const current = new Map(cards().map(card => [card.dataset.courseId, card]));
      saved.forEach(id => { if (current.has(id)) { courseGrid.append(current.get(id)); current.delete(id); } });
      current.forEach(card => courseGrid.append(card));
    }
  } catch {}
  const save = card => {
    const rows = cards();
    try {
      localStorage.setItem(key, JSON.stringify(rows.map(row => row.dataset.courseId)));
      status.textContent = locale === 'en' ? `Order saved · ${rows.indexOf(card) + 1} / ${rows.length}` : `排序已儲存 · 第 ${rows.indexOf(card) + 1} / ${rows.length} 門`;
    } catch {
      status.textContent = locale === 'en' ? 'Order changed. Browser storage is unavailable; reloading will reset it.' : '排序已調整，但瀏覽器無法儲存，重新整理後會還原。';
    }
  };
  let drag = null;
  courseGrid.addEventListener('keydown', event => {
    const handle = event.target.closest('.course-drag');
    if (!handle) return;
    if (event.key === 'Escape' && drag) { finish(true); return; }
    if (!['ArrowLeft','ArrowUp','ArrowRight','ArrowDown','Home','End'].includes(event.key) || drag) return;
    event.preventDefault();
    const card = handle.closest('[data-course-id]');
    const rows = cards(), index = rows.indexOf(card);
    const target = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : index + (['ArrowLeft','ArrowUp'].includes(event.key) ? -1 : 1);
    if (target < 0 || target >= rows.length || target === index) return;
    courseGrid.insertBefore(card, target > index ? rows[target].nextSibling : rows[target]);
    handle.focus(); save(card);
  });
  courseGrid.addEventListener('pointerdown', event => {
    const handle = event.target.closest('.course-drag');
    if (!handle || event.button !== 0 || drag) return;
    event.preventDefault(); handle.focus();
    drag = {card: handle.closest('[data-course-id]'), handle, id: event.pointerId, original: cards(), x:event.clientX, y:event.clientY, moved:false};
    // Capture on the stable grid; moving a card must not interrupt the pointer stream.
    courseGrid.setPointerCapture(event.pointerId);
  });
  courseGrid.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(event.clientX-drag.x,event.clientY-drag.y)<6) return;
    drag.moved=true; drag.card.classList.add('sorting');
    if(event.clientY > window.innerHeight - 65) window.scrollBy(0, 18);
    if(event.clientY < 65) window.scrollBy(0, -18);
    const target = document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-course-id]');
    if (!target || target === drag.card || !courseGrid.contains(target)) return;
    const rows=cards();
    courseGrid.insertBefore(drag.card, rows.indexOf(target)>rows.indexOf(drag.card) ? target.nextSibling : target);
  });
  function finish(cancel = false) {
    if (!drag) return;
    const current = drag; drag = null;
    if (cancel) current.original.forEach(card => courseGrid.append(card));
    current.card.classList.remove('sorting');
    if (courseGrid.hasPointerCapture(current.id)) courseGrid.releasePointerCapture(current.id);
    current.handle.focus({preventScroll:true});
    if (!cancel && current.moved) save(current.card);
  }
  courseGrid.addEventListener('pointerup', () => finish());
  courseGrid.addEventListener('pointercancel', () => finish(true));
  courseGrid.addEventListener('lostpointercapture', () => finish(true));
}
