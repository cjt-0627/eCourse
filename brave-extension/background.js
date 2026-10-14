const LOCAL_ORIGIN = "http://localhost:8765";

function cookieDetails(cookie) {
  const host = String(cookie.domain || "").replace(/^\./, "");
  const path = cookie.path || "/";
  const details = {
    // Both supported learning platforms are HTTPS-only. A cookie can omit the
    // Secure attribute and still be written against an HTTPS URL; using HTTP
    // here would require an unnecessary host permission and break handoff.
    url: `https://${host}${path}`,
    name: cookie.name,
    value: cookie.value,
    path,
    secure: cookie.secure !== false,
    httpOnly: Boolean(cookie.httpOnly)
  };
  if (String(cookie.domain || "").startsWith(".")) details.domain = cookie.domain;
  if (Number(cookie.expires) > 0) details.expirationDate = Number(cookie.expires);
  const sameSite = String(cookie.sameSite || "").toLowerCase();
  if (["no_restriction", "lax", "strict", "unspecified"].includes(sameSite)) {
    details.sameSite = sameSite;
  } else if (sameSite === "none") {
    details.sameSite = "no_restriction";
  }
  return details;
}

function isRequiredSessionCookie(cookie) {
  const name = String(cookie?.name || "").toLowerCase();
  return name.startsWith("moodlesession") || name === "phpsessid";
}

async function replaceCookie(cookie, storeId) {
  const details = cookieDetails(cookie);
  if (storeId) details.storeId = storeId;
  let written;
  try {
    written = await chrome.cookies.set(details);
  } catch (error) {
    if (isRequiredSessionCookie(cookie)) throw error;
    return false;
  }
  if (!written || written.value !== cookie.value) {
    if (isRequiredSessionCookie(cookie)) {
      throw new Error(`无法写入 ${cookie.name}，请重新载入扩充功能后再试。`);
    }
    // WAF/load-balancer cookies may be immediately rotated by Brave or the
    // upstream service. They are best-effort; Moodle/PHP session cookies are
    // the only values whose exact write is required for handoff.
    return false;
  }
  return true;
}

async function clearPlatformCookies(targetUrl, storeId) {
  const host = new URL(targetUrl).hostname.toLowerCase();
  // Clear cookies owned by the platform plus any Moodle/PHP session cookie
  // visible from a parent domain. A stale parent-domain session can otherwise
  // be sent beside the freshly imported host-only cookie; Moodle may read the
  // stale value first and immediately redirect the new tab back to login.
  for (let round = 0; round < 3; round += 1) {
    const query = {url: targetUrl};
    if (storeId) query.storeId = storeId;
    const existing = (await chrome.cookies.getAll(query)).filter(cookie => {
      const cookieHost = String(cookie.domain || "").replace(/^\./, "").toLowerCase();
      const isPlatformCookie = cookieHost === host;
      const name = String(cookie.name || "").toLowerCase();
      const isConflictingSession = name.startsWith("moodlesession") || name === "phpsessid";
      return isPlatformCookie || isConflictingSession;
    });
    if (!existing.length) return;
    for (const cookie of existing) {
      const removal = {
        // Non-Secure cookies can also be removed through their HTTPS URL.
        // Using http:// here falls outside this extension's narrow host
        // permission and silently leaves the conflicting WAF cookie behind.
        url: `https://${host}${cookie.path || "/"}`,
        name: cookie.name,
        storeId: cookie.storeId
      };
      if (cookie.partitionKey) removal.partitionKey = cookie.partitionKey;
      await chrome.cookies.remove(removal);
    }
  }
  const query = {url: targetUrl};
  if (storeId) query.storeId = storeId;
  const remaining = (await chrome.cookies.getAll(query)).filter(cookie => {
    const cookieHost = String(cookie.domain || "").replace(/^\./, "").toLowerCase();
    const name = String(cookie.name || "").toLowerCase();
    return cookieHost === host || name.startsWith("moodlesession") || name === "phpsessid";
  });
  if (remaining.length) throw new Error("无法清除平台的旧 Cookie，请重新启动 Brave 后再试。");
}

async function cookieStoreForTab(tabId) {
  if (!Number.isInteger(tabId)) return null;
  const stores = await chrome.cookies.getAllCookieStores();
  return stores.find(store => store.tabIds.includes(tabId))?.id || null;
}

async function consumeHandoff(token, senderTab) {
  const response = await fetch(`${LOCAL_ORIGIN}/api/v1/browser-extension/handoffs/${encodeURIComponent(token)}`, {
    method: "POST",
    credentials: "omit",
    cache: "no-store"
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || `交接失败（HTTP ${response.status}）`);
  const payload = body.data;
  if (!payload?.target_url || !Array.isArray(payload.cookies) || !payload.cookies.length) {
    throw new Error("交接资料不完整，请重新连接平台。");
  }
  const storeId = await cookieStoreForTab(senderTab?.id);
  await clearPlatformCookies(payload.target_url, storeId);
  for (const cookie of payload.cookies) await replaceCookie(cookie, storeId);
  // Open immediately after the local cookie write.  Waiting for a duplicate
  // network verification here made a normal handoff take several seconds;
  // the destination tab performs the same request as it loads.
  const destination = new URL(payload.target_url);
  destination.searchParams.set("ecourse_handoff", crypto.randomUUID());
  await chrome.tabs.create({url: destination.toString(), active: true, windowId: senderTab?.windowId});
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const senderUrl = sender.tab?.url || "";
  if (message?.type !== "ecourse-consume-handoff" || !senderUrl.startsWith(LOCAL_ORIGIN + "/")) return;
  consumeHandoff(message.handoffToken, sender.tab)
    .then(() => sendResponse({ok: true}))
    .catch(error => sendResponse({ok: false, message: error.message}));
  return true;
});
