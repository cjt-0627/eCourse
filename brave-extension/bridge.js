window.addEventListener("message", event => {
  if (event.source !== window || event.origin !== location.origin) return;
  const message = event.data;
  if (message?.type !== "ecourse-extension-open" || typeof message.handoffToken !== "string") return;
  chrome.runtime.sendMessage({
    type: "ecourse-consume-handoff",
    requestId: message.requestId,
    handoffToken: message.handoffToken
  }, response => {
    const error = chrome.runtime.lastError;
    window.postMessage({
      type: "ecourse-extension-result",
      requestId: message.requestId,
      ok: Boolean(response?.ok) && !error,
      message: error?.message || response?.message || null
    }, location.origin);
  });
});
