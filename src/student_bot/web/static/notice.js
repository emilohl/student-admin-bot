// Dismissable experimental-service notice. Session-scoped: dismissal
// persists across reloads in the same tab but a fresh browser session
// shows the notice again, so returning users are reminded that this is
// still experimental.
(() => {
  const KEY = "notice_dismissed";
  const store = sessionStorage;
  const notice = document.querySelector(".notice");
  if (!notice) return;
  if (store.getItem(KEY) === "1") {
    notice.remove();
    return;
  }
  const close = notice.querySelector(".notice-close");
  if (!close) return;
  close.addEventListener("click", () => {
    notice.remove();
    store.setItem(KEY, "1");
  });
  // Clean up any prior persistent dismissal from earlier builds so users
  // who hid the notice via localStorage before this change see it again.
  try { localStorage.removeItem("notice_dismissed_v1"); } catch {}
})();
