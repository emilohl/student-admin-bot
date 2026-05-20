// Minimal vanilla-JS chat client. Streams answers via SSE; renders source
// links in the answer body; supports 👍/👎 feedback per message.

const state = {
  sessionId: localStorage.getItem("session_id") || crypto.randomUUID(),
  name: localStorage.getItem("name") || "",
  optOut: localStorage.getItem("opt_out") === "1",
  // Opt-in "Learn more about how this chatbot works" toggle. When on, the
  // server assembles a per-turn diagnostic payload and the UI surfaces a
  // 🔍 button on each bot bubble that opens #debug-panel.
  learnMore: localStorage.getItem("learn_more") === "1",
  // In-memory conversation log, mirroring what's on screen. Captured client
  // side because that's where every conversation exists in full (the server
  // skips qa_log writes for opted-out users). Used by exportThreadMarkdown().
  thread: [],
  // Cached debug payloads keyed by qa_id. Populated from the SSE meta event
  // for fresh turns; older turns (within the session) are fetched on demand.
  debugCache: new Map(),
  isAdmin: false,
};
localStorage.setItem("session_id", state.sessionId);

const el = (sel) => document.querySelector(sel);
const onboarding = el("#onboarding");
const chat = el("#chat");
const messages = el("#messages");
const composer = el("#composer");
const statusEl = el("#status");
const connEl = el("#conn");
const perfContextEl = el("#perf-context");
const perfTokensEl = el("#perf-tokens");
const perfSystemEl = el("#perf-system");
const perfPanelEl = el("#perf-panel");
let perfEnabled = false;

function botDisplayName() {
  const full = ((window.t && window.t("brand.name")) || "Lux").trim();
  return full.split(" - ")[0].trim() || full;
}

el("#name").value = state.name;
el("#opt-out").checked = state.optOut;
if (el("#learn-more")) el("#learn-more").checked = state.learnMore;
if (el("#learn-more-chat")) el("#learn-more-chat").checked = state.learnMore;

function syncLearnMore(next) {
  state.learnMore = !!next;
  localStorage.setItem("learn_more", state.learnMore ? "1" : "0");
  if (el("#learn-more")) el("#learn-more").checked = state.learnMore;
  if (el("#learn-more-chat")) el("#learn-more-chat").checked = state.learnMore;
  // When the user flips the toggle off mid-session, force the view back to
  // chat (the tab bar disappears, so the debug tab would otherwise be
  // unreachable). The debug cache is preserved so flipping back on keeps
  // access to past turns.
  if (!state.learnMore) setView("chat");
  syncTabBarVisibility();
  // Existing bot bubbles get their 🔍 button shown/hidden in sync.
  document.querySelectorAll(".msg.bot").forEach((wrap) => {
    const btn = wrap.querySelector(".debug-btn");
    if (btn) btn.classList.toggle("hidden", !state.learnMore);
  });
}

// View tabs: only shown after onboarding AND when learn-more is on. The
// active-tab CSS rule (.viewing-debug on <body>) hides the chat card and
// reveals #debug-panel, plus widens `main` so the retrieval table has room.
function setView(view) {
  const isDebug = view === "debug";
  document.body.classList.toggle("viewing-debug", isDebug);
  document.querySelectorAll(".view-tab").forEach((btn) => {
    const active = btn.dataset.view === view;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function syncTabBarVisibility() {
  const tabBar = el("#view-tabs");
  if (!tabBar) return;
  const pastOnboarding = !el("#chat").classList.contains("hidden");
  const show = state.learnMore && pastOnboarding;
  tabBar.classList.toggle("hidden", !show);
}

document.querySelectorAll(".view-tab").forEach((btn) => {
  btn.addEventListener("click", () => setView(btn.dataset.view));
});

if (el("#learn-more-chat")) {
  el("#learn-more-chat").addEventListener("change", (e) => syncLearnMore(e.target.checked));
}

el("#start").addEventListener("click", () => {
  state.name = el("#name").value.trim() || "Anonym";
  state.optOut = el("#opt-out").checked;
  localStorage.setItem("name", state.name);
  localStorage.setItem("opt_out", state.optOut ? "1" : "0");
  if (el("#learn-more")) syncLearnMore(el("#learn-more").checked);
  // Tell the server about the name + opt-out preference.
  fetch("api/session", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: state.name, session_id: state.sessionId, opt_out: state.optOut }),
  }).catch(() => {});
  onboarding.classList.add("hidden");
  chat.classList.remove("hidden");
  syncTabBarVisibility();
  renderLoggingStatus();
  el("#question").focus();
  if (perfEnabled) refreshSystemLoad();
});

// Logging toggle: lets a returning user change their opt-out preference any
// time, not just at onboarding (#33). State is mirrored both in localStorage
// and on the server (set_opt_out keyed by web_user_id).
function renderLoggingStatus() {
  const dot = el("#logging-dot");
  const label = el("#logging-label");
  const btn = el("#logging-toggle");
  if (!dot || !label || !btn) return;
  const t = window.t || ((k) => k);
  if (state.optOut) {
    dot.classList.add("off");
    label.textContent = t("chat.logging.off");
    label.title = t("chat.logging.off.tip");
    btn.textContent = t("chat.logging.enable");
    btn.title = t("chat.logging.enable.tip");
    btn.setAttribute("aria-label", t("chat.logging.enable.tip"));
  } else {
    dot.classList.remove("off");
    label.textContent = t("chat.logging.on");
    label.title = t("chat.logging.on.tip");
    btn.textContent = t("chat.logging.disable");
    btn.title = t("chat.logging.disable.tip");
    btn.setAttribute("aria-label", t("chat.logging.disable.tip"));
  }
}

if (el("#logging-toggle")) {
  el("#logging-toggle").addEventListener("click", () => {
    state.optOut = !state.optOut;
    localStorage.setItem("opt_out", state.optOut ? "1" : "0");
    renderLoggingStatus();
    fetch("api/session", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: state.name,
        session_id: state.sessionId,
        opt_out: state.optOut,
      }),
    }).catch(() => {});
    const t = window.t || ((k) => k);
    statusEl.textContent = state.optOut ? t("chat.logging.toast.off") : t("chat.logging.toast.on");
  });
}

document.addEventListener("i18n:langchange", renderLoggingStatus);

el("#reset").addEventListener("click", async () => {
  // Confirm before discarding a non-empty conversation. Skip the prompt
  // when the thread is already empty (no work to lose).
  if (state.thread.length) {
    const msg = (window.t && window.t("chat.reset.confirm"))
      || "Vill du börja om? Den nuvarande tråden raderas.";
    if (!window.confirm(msg)) return;
  }
  await fetch("api/reset", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.sessionId }),
  }).catch(() => {});
  messages.innerHTML = "";
  state.thread = [];
  refreshExportButton();
  statusEl.textContent = window.t ? window.t("chat.newthread") : "tråden rensad";
});

el("#export").addEventListener("click", () => {
  exportThreadMarkdown();
});

// Enter submits, Shift+Enter (and Cmd/Ctrl+Enter) inserts a newline.
// `isComposing` skips IME composition where Enter is used to commit a
// candidate word and shouldn't fire the form.
el("#question").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (e.shiftKey || e.ctrlKey || e.metaKey || e.isComposing) return;
  e.preventDefault();
  composer.requestSubmit();
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = el("#question").value.trim();
  if (!q) return;
  el("#question").value = "";
  el("#send").disabled = true;
  appendUser(q);
  state.thread.push({ role: "user", ts: Date.now(), text: q });
  refreshExportButton();

  const botMsg = appendBot();
  const botEntry = {
    role: "bot",
    ts: Date.now(),
    raw: "",            // full streamed text (filled at end)
    body: "",           // body after splitMessage strips badge/sources/tip
    jargon: "",         // jargon transparency prefix (if any)
    confidence: "",     // confidence label, e.g. "high" / "low"
    confidenceText: "", // localized confidence text ("Tillförlitlighet: hög")
    sources: [],        // numbered, cited-only references
    reaction: "",       // "positive" | "negative" | "" (only if user clicked)
  };
  state.thread.push(botEntry);
  botMsg.entry = botEntry;
  let firstToken = true;
  const reqStartedAt = performance.now();
  let firstTokenAt = null;

  let buf = "";
  let finalMeta = null;
  try {
    const resp = await fetch("api/chat", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        name: state.name,
        session_id: state.sessionId,
        opt_out: state.optOut,
        learn_more: state.learnMore,
      }),
    });
    if (!resp.ok || !resp.body) {
      const tw = botMsg.body.querySelector(".thinking-wrap");
      if (tw) tw.remove();
      botMsg.render.textContent = `[error ${resp.status}]`;
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        if (!part.trim()) continue;
        const { event, data } = parseSSE(part);
        if (event === "token") {
          if (firstToken) {
            const tw = botMsg.body.querySelector(".thinking-wrap");
            if (tw) tw.remove();
            firstToken = false;
            firstTokenAt = performance.now();
          }
          botMsg.entry.raw += data;
          scheduleRender(botMsg);
        } else if (event === "jargon") {
          const prefixEl = botMsg.body.querySelector(".msg-prefix");
          if (prefixEl) {
            const stick = nearBottom();
            // Accumulate the raw jargon text on the thread entry and
            // re-render via renderMarkdown so italic / bold show up
            // immediately during the thinking phase (not only after
            // decorateBot runs at end-of-stream). Keeping the raw form
            // on entry.jargon also means the conversation export's
            // `> _Tolkar..._` blockquote keeps its italic semantics.
            botMsg.entry.jargon = (botMsg.entry.jargon || "") + data;
            prefixEl.innerHTML = renderMarkdown(botMsg.entry.jargon);
            if (stick) messages.scrollTop = messages.scrollHeight;
          }
        } else if (event === "thinking") {
          // Show "<bot> funderar…" beside the dots while the model is in
          // its <think>...</think> phase. Cleared on "end" or when the
          // first answer token arrives.
          const label = botMsg.body.querySelector(".thinking-label");
          if (label) {
            if (data === "start") {
              const name = botDisplayName();
              const phase = (window.t && window.t("chat.thinking_phase")) || "is thinking…";
              label.textContent = `${name} ${phase}`;
              label.removeAttribute("hidden");
            } else {
              label.setAttribute("hidden", "");
            }
          }
        } else if (event === "meta") {
          try { finalMeta = JSON.parse(data); } catch {}
        }
      }
    }
  } catch (err) {
    const tw = botMsg.body.querySelector(".thinking-wrap");
    if (tw) tw.remove();
    if (botMsg.entry) botMsg.entry.raw += `\n[stream error: ${err.message}]`;
    scheduleRender(botMsg);
    firstToken = false;
  } finally {
    el("#send").disabled = false;
  }

  if (finalMeta) {
    if (Object.prototype.hasOwnProperty.call(finalMeta, "performance_panel_enabled")) {
      setPerfEnabled(!!finalMeta.performance_panel_enabled);
    }
    const rawText = botMsg.entry?.raw || "";
    if (firstTokenAt && rawText) {
      const tokEst = estimateTokens(rawText);
      const genSecs = Math.max(0.001, (performance.now() - firstTokenAt) / 1000);
      finalMeta.client_ttft_ms = Math.max(0, Math.round(firstTokenAt - reqStartedAt));
      finalMeta.client_tps = tokEst / genSecs;
    }
    const stick = nearBottom();
    decorateBot(botMsg, finalMeta);
    renderContextNotices(botMsg, finalMeta);
    if (stick) messages.scrollTop = messages.scrollHeight;
    if (perfEnabled) updatePerfPanel(finalMeta);
    // Cache the inline debug payload (when learn_more was on and the
    // server wrote qa_debug) so a click on this bubble's 🔍 button is
    // instant — no /api/debug/{qa_id} round-trip. We deliberately don't
    // auto-switch to the debug tab: that would hide the answer that just
    // finished streaming. The tab bar itself + the per-bubble 🔍 button
    // are the affordances for going to look at the data.
    if (finalMeta.qa_id && finalMeta.debug) {
      state.debugCache.set(finalMeta.qa_id, finalMeta.debug);
    }
  }
});

// Append UX-honesty notices to a bot bubble when the server reports that
// the LLM context has been truncated or the session was just resurrected
// from a TTL-pruned slot. Both render as small muted lines at the top of
// the bubble so they sit above the answer without competing with it.
function renderContextNotices(botMsg, meta) {
  if (!botMsg || !botMsg.body || !meta) return;
  const lines = [];
  if (meta.session_expired) {
    lines.push((window.t && window.t("chat.session_expired")) || "");
  }
  if (meta.history_truncated) {
    lines.push((window.t && window.t("chat.history_truncated")) || "");
  }
  if (!lines.length) return;
  const wrap = document.createElement("div");
  wrap.className = "ctx-notices";
  for (const text of lines) {
    if (!text) continue;
    const line = document.createElement("div");
    line.className = "ctx-notice";
    line.textContent = text;
    wrap.appendChild(line);
  }
  // Insert at the top of the body so the answer reads naturally below it.
  botMsg.body.insertBefore(wrap, botMsg.body.firstChild);
}

function setPerfEnabled(enabled) {
  perfEnabled = !!enabled;
  if (!perfPanelEl) return;
  perfPanelEl.classList.toggle("hidden", !perfEnabled);
}

function estimateTokens(text) {
  return Math.max(0, Math.round((text || "").length / 4));
}

function formatPct(v) {
  return Number.isFinite(v) ? `${v.toFixed(0)}%` : "–";
}

function updatePerfPanel(meta) {
  if (!perfEnabled) return;
  const used = meta.context_tokens_est;
  const limit = meta.context_tokens_limit;
  if (Number.isFinite(used) && Number.isFinite(limit) && limit > 0) {
    const pct = Math.max(0, Math.min(100, (used / limit) * 100));
    perfContextEl.textContent = `${used}/${limit} (${pct.toFixed(0)}%)`;
  } else {
    perfContextEl.textContent = "–";
  }

  const ttft = Number.isFinite(meta.ttft_ms) ? meta.ttft_ms : meta.client_ttft_ms;
  const tps = Number.isFinite(meta.gen_tps) ? meta.gen_tps : meta.client_tps;
  const tok = meta.gen_tokens_est;
  const parts = [];
  if (Number.isFinite(ttft)) parts.push(`TTFT ${formatDuration(ttft)}`);
  if (Number.isFinite(tps)) parts.push(`${tps.toFixed(1)} tok/s`);
  if (Number.isFinite(tok)) parts.push(`~${tok} tok`);
  perfTokensEl.textContent = parts.length ? parts.join(" · ") : "–";

  applyMergedSystemLoad(meta.system_load, meta.host_system_load);
}

function formatDuration(ms) {
  if (!Number.isFinite(ms)) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function applyMergedSystemLoad(containerLoad, hostLoad) {
  if (!perfSystemEl) return;
  const cCpu = formatPct(containerLoad?.cpu_pct);
  const hCpu = formatPct(hostLoad?.cpu_pct);
  const cMem = formatPct(containerLoad?.mem_pct);
  const hMem = formatPct(hostLoad?.mem_pct);
  perfSystemEl.innerHTML = (
    `<span class="host">Host</span> / <span class="cont">Cont.</span> · ` +
    `CPU: <span class="host">${hCpu}</span> / <span class="cont">${cCpu}</span> · ` +
    `RAM: <span class="host">${hMem}</span> / <span class="cont">${cMem}</span>`
  );
}

async function refreshSystemLoad() {
  if (!perfEnabled) return;
  try {
    const resp = await fetch("api/system-load", { credentials: "include" });
    if (!resp.ok) return;
    const data = await resp.json();
    if (Object.prototype.hasOwnProperty.call(data, "performance_panel_enabled")) {
      setPerfEnabled(!!data.performance_panel_enabled);
      if (!perfEnabled) return;
    }
    applyMergedSystemLoad(data.system_load, data.host_system_load);
  } catch (_) {}
}

setInterval(() => {
  if (!perfEnabled) return;
  if (document.hidden) return;
  refreshSystemLoad();
}, 1000);

// Cloud-provider notice (#20). Populated from /api/health.cloud_provider_name;
// empty string = local model, hide the notice everywhere. Idempotent so
// language switches re-render the text.
function applyCloudProviderNotice(providerName) {
  const onboardingEl = el("#cloud-notice-onboarding");
  const chatEl = el("#cloud-notice-chat");
  const t = window.t || ((k) => k);
  if (!providerName) {
    if (onboardingEl) onboardingEl.classList.add("hidden");
    if (chatEl) chatEl.classList.add("hidden");
    return;
  }
  const interpolate = (key) =>
    (t(key) || "").replace(/\{provider\}/g, providerName);
  if (onboardingEl) {
    onboardingEl.innerHTML = interpolate("cloud.notice.onboarding");
    onboardingEl.classList.remove("hidden");
  }
  if (chatEl) {
    chatEl.innerHTML = interpolate("cloud.notice.chat");
    chatEl.classList.remove("hidden");
  }
}

let cloudProviderName = "";

async function initPerf() {
  try {
    const resp = await fetch("api/health", { credentials: "include" });
    if (!resp.ok) return;
    const data = await resp.json();
    setPerfEnabled(!!data.performance_panel_enabled);
    cloudProviderName = data.cloud_provider_name || "";
    applyCloudProviderNotice(cloudProviderName);
    state.isAdmin = !!data.is_admin;
    document.body.classList.toggle("is-admin", state.isAdmin);
    // Server-driven default for the learn-more toggle; only honored when
    // the user hasn't already set a preference in localStorage.
    if (
      localStorage.getItem("learn_more") === null &&
      data.learn_more_default
    ) {
      syncLearnMore(true);
    }
  } catch (_) {
    setPerfEnabled(false);
  }
}

document.addEventListener("i18n:langchange", () => applyCloudProviderNotice(cloudProviderName));

initPerf();

function parseSSE(block) {
  let event = "token", data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).replace(/^\s/, "") + "\n";
  }
  return { event, data: data.replace(/\n$/, "") };
}

// True if the message list is scrolled to (or within a small slop of) the
// bottom. Streaming-time appends consult this before pinning the scroll so
// the user can scroll up to read earlier parts of a long reply without each
// new token yanking them back down. Evaluate BEFORE appending content —
// once the new node is in the DOM, scrollHeight grows and the predicate
// returns false even though the user was at the bottom a moment ago.
function nearBottom() {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 40;
}

function appendUser(text) {
  const div = document.createElement("div");
  div.className = "msg user";
  div.textContent = text;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function appendBot() {
  const wrap = document.createElement("div");
  wrap.className = "msg bot";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = botDisplayName();
  const body = document.createElement("div");
  body.className = "body";
  // Prefix (e.g. jargon transparency) streams before answer tokens without
  // clearing the animated "thinking" dots — see SSE `jargon` vs `token`.
  const prefix = document.createElement("div");
  prefix.className = "msg-prefix";
  // Animated three-dot indicator while we wait for the first answer token.
  // The label sits next to the dots and shows "<bot> funderar…" while
  // the model is in its <think>...</think> reasoning phase.
  const thinking = document.createElement("div");
  thinking.className = "thinking-wrap";
  thinking.setAttribute("aria-live", "polite");
  thinking.innerHTML =
    '<span class="thinking-dots" aria-label="thinking">' +
      "<span></span><span></span><span></span>" +
    "</span>" +
    '<span class="thinking-label" hidden></span>';
  // Live-rendered markdown container. Streamed tokens accumulate in
  // botMsg.entry.raw and are re-rendered here on each animation frame so
  // the user sees formatting (bold, lists, links) as the answer arrives
  // rather than only after decorateBot runs at end-of-stream.
  const render = document.createElement("div");
  render.className = "msg-render";
  body.appendChild(prefix);
  body.appendChild(thinking);
  body.appendChild(render);
  wrap.appendChild(meta);
  wrap.appendChild(body);
  messages.appendChild(wrap);
  messages.scrollTop = messages.scrollHeight;
  return { wrap, meta, body, render };
}

// Coalesce per-token markdown re-renders to ~60Hz via requestAnimationFrame.
// After decorateBot runs, further schedules are no-ops — the final pass owns
// the rendered HTML (with numbered citations, sources block, and tip).
function scheduleRender(botMsg) {
  if (!botMsg || !botMsg.render || !botMsg.entry) return;
  if (botMsg._decorated || botMsg._renderRaf) return;
  botMsg._renderRaf = requestAnimationFrame(() => {
    botMsg._renderRaf = 0;
    if (botMsg._decorated) return;
    const stick = nearBottom();
    botMsg.render.innerHTML = renderMarkdown(botMsg.entry.raw || "");
    if (stick) messages.scrollTop = messages.scrollHeight;
  });
}

function decorateBot(botMsg, meta) {
  // Confidence badge in the meta line above the bubble.
  if (meta.confidence) {
    const badge = document.createElement("span");
    badge.className = `conf ${meta.confidence_level}`;
    badge.textContent = meta.confidence;
    const tip = confidenceTooltip(meta.confidence_level);
    if (tip) {
      badge.title = tip;
      badge.setAttribute("aria-label", tip);
    }
    botMsg.meta.appendChild(badge);
  }

  // Split the streamed text into [body, sources, tip] and render each
  // with its own styling. Numbered citations replace the inline
  // [doc · section] markers the LLM emitted; only sources that were
  // actually cited inline appear in the references list, renumbered
  // in order of first appearance.
  const prefixEl = botMsg.body.querySelector(".msg-prefix");
  // Prefer the RAW markdown source for the jargon (accumulated on
  // entry.jargon during the stream so the live-render path can italicise
  // `_Tolkar X._`). Fall back to the prefix's textContent only if no
  // entry.jargon was captured — the rendered text would lose the
  // underscores and the merged-body re-render below couldn't restore
  // the italic.
  const jargonRaw = (botMsg.entry?.jargon || prefixEl?.textContent || "").trim();
  // Mirror the pre-live-render behaviour: merge the jargon prefix into the
  // raw text fed to splitMessage so it gets markdown-rendered alongside the
  // body (e.g. `_Tolkar X._` becomes italic). No separator between prefix
  // and body matches the old `body.textContent` concatenation. The prefix
  // div is emptied below so the jargon isn't shown twice.
  const streamedRaw = botMsg.entry?.raw || "";
  const raw = jargonRaw + (streamedRaw || botMsg.body.textContent);
  const { body, sources: allSources, tip } = splitMessage(raw);
  const serverSources = Array.isArray(meta.sources) ? meta.sources : [];
  const sourceCandidates = Array.isArray(meta.source_candidates) ? meta.source_candidates : [];
  const effectiveSources = mergeSources(allSources, serverSources, sourceCandidates);
  const bodyForCitations = (meta.numbered_body || "").trim() || body;

  const { html: bodyHtml, citedSources } = renderBodyWithCitations(bodyForCitations, effectiveSources);
  const citedSourcesWithUrls = mergeSourceUrlsHeuristic(
    mergeCitedUrlsFromServerMeta(
      backfillMissingSourceUrls(citedSources, effectiveSources),
      serverSources
    ),
    meta.source_urls
  );
  let html = bodyHtml;
  // Show only sources that were actually cited inline.
  // This avoids surfacing unrelated retrieved chunks as references.
  if (citedSourcesWithUrls.length) {
    html += renderSources(citedSourcesWithUrls, meta.lang || "sv");
  } else if (serverSources.length) {
    // Fallback: if inline citation matching fails, still show the
    // authoritative server-provided references.
    html += renderSources(serverSources, meta.lang || "sv");
  }
  if (tip) html += renderTip(tip);
  // Cancel any pending live re-render and target the live element so the
  // jargon prefix (.msg-prefix) stays in place. Falls back to replacing
  // the whole body if .msg-render is somehow missing.
  if (botMsg._renderRaf) {
    cancelAnimationFrame(botMsg._renderRaf);
    botMsg._renderRaf = 0;
  }
  botMsg._decorated = true;
  const renderTarget = botMsg.render || botMsg.body;
  renderTarget.innerHTML = html;
  // Empty the prefix since its content was merged into the rendered body
  // above; otherwise the jargon line would appear twice.
  if (prefixEl) prefixEl.textContent = "";

  // Snapshot the parts of the answer we want to keep in the exportable
  // thread log. `body` retains markdown links and inline citation markers;
  // the export converts those to numbered links against `sources`.
  if (botMsg.entry) {
    botMsg.entry.raw = raw;
    botMsg.entry.body = body;
    // entry.jargon was populated incrementally during streaming with the
    // RAW markdown; keep that form so the conversation export's
    // `> _Tolkar..._` blockquote still renders as italic. Falls back to
    // the prefix textContent for code paths that bypassed the streaming
    // accumulator.
    if (!botMsg.entry.jargon) botMsg.entry.jargon = jargonRaw;
    botMsg.entry.confidence = meta.confidence_level || "";
    botMsg.entry.confidenceText = meta.confidence || "";
    botMsg.entry.sources = (citedSourcesWithUrls && citedSourcesWithUrls.length)
      ? citedSourcesWithUrls
      : (serverSources || []);
    refreshExportButton();
  }

  // Feedback buttons (only if we have a qa id).
  if (meta.qa_id) {
    botMsg.wrap.dataset.qaId = String(meta.qa_id);
    const actions = document.createElement("div");
    actions.className = "actions";
    const up = document.createElement("button");
    up.className = "up"; up.textContent = "👍";
    const down = document.createElement("button");
    down.className = "down"; down.textContent = "👎";
    const send = (sentiment, btn) => {
      fetch("api/feedback", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ qa_id: meta.qa_id, sentiment }),
      });
      up.classList.remove("active");
      down.classList.remove("active");
      btn.classList.add("active");
      if (botMsg.entry) botMsg.entry.reaction = sentiment;
    };
    up.addEventListener("click", () => send("positive", up));
    down.addEventListener("click", () => send("negative", down));
    actions.appendChild(up);
    actions.appendChild(down);
    // 🔍 Details button: opens the debug panel for this turn. Hidden when
    // the learn-more toggle is off so non-curious users don't see the
    // pedagogical UI; toggled back on by syncLearnMore() if they flip it.
    const tDetails = (window.t && window.t("debug.msg.details")) || "🔍 Details";
    const tDetailsTitle =
      (window.t && window.t("debug.msg.details.title")) || tDetails;
    const dbg = document.createElement("button");
    dbg.type = "button";
    dbg.className = "debug-btn";
    dbg.textContent = tDetails;
    dbg.title = tDetailsTitle;
    dbg.setAttribute("aria-label", tDetailsTitle);
    if (!state.learnMore) dbg.classList.add("hidden");
    dbg.addEventListener("click", () => showDebugPanel(meta.qa_id));
    actions.appendChild(dbg);
    botMsg.wrap.appendChild(actions);
  }
}

function mergeSources(...lists) {
  const out = [];
  const seen = new Set();
  for (const list of lists) {
    if (!Array.isArray(list)) continue;
    for (const s of list) {
      if (!s || typeof s.label !== "string") continue;
      const key = `${s.n ?? ""}|${s.label}|${s.url ?? ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(s);
    }
  }
  return out;
}

function confidenceTooltip(level) {
  const t = window.t || ((k) => k);
  if (level === "high") return t("confidence.tip.high");
  if (level === "medium") return t("confidence.tip.medium");
  if (level === "low") return t("confidence.tip.low");
  return "";
}

// Split the streamed answer (body + optional confidence badge + optional
// sources block + literacy tip) into structured pieces. Order in stream:
//   [jargon note]  body  [\n\n_Conf_]  [**Sources:**\n1. ...]  \n\n_Tip: ..._
// Each detector tolerates being the very first thing in the string so an
// empty body (rare LLM hiccup) doesn't leave any of these fragments
// rendered as part of the answer.
function splitMessage(text) {
  text = text.trim();

  // Trailing literacy footer (italic line starting with _Tip / _Tips).
  let tip = "";
  const tipRe = /(?:^|\n+)_(Tip|Tips):[^_]*_\s*$/;
  const tipMatch = text.match(tipRe);
  if (tipMatch) {
    tip = tipMatch[0].trim();
    text = text.slice(0, tipMatch.index).trimEnd();
  }

  // Sources block: "**Källor:**" or "**Sources:**" followed by 1./2./... items.
  let sources = [];
  const srcRe = /(?:^|\n+)\*\*(Källor|Sources):\*\*\n([\s\S]+)$/;
  const srcMatch = text.match(srcRe);
  if (srcMatch) {
    const lines = srcMatch[2].split("\n").map(s => s.trim()).filter(Boolean);
    for (const line of lines) {
      // "1. [label](url)" or "1. label"
      const m = line.match(/^(\d+)\.\s+(?:\[(.+?)\]\((.+)\)|(.+))$/);
      if (m) {
        sources.push({
          n: parseInt(m[1], 10),
          label: (m[2] || m[4] || "").trim(),
          url: m[3] || "",
        });
      }
    }
    text = text.slice(0, srcMatch.index).trimEnd();
  }

  // Confidence badge can sit at the start (older _render order) or end
  // (current order) — strip it wherever, including when it's the sole
  // remaining content after tip + sources have been peeled off.
  text = text
    .replace(/(^|\n+)_(Tillförlitlighet|Confidence):[^_]*_(\n+|$)/g, "$1")
    .trim();

  return { body: text, sources, tip };
}

// Build a flexible lookup from inline-citation text → matching source.
// The LLM's inline format is loose — sometimes [Title · Section], often
// [Title · Section · Subsection · BilagaInfo, Sida X av Y] — so we register
// multiple keys per source and fall back to title-only matching when no
// fuller key matches and the title is unambiguous.
function buildCitationLookup(sources) {
  const lookup = {};
  const titleCount = {};

  for (const s of sources) {
    const noPage = s.label.replace(/,\s*s\.\s*\d+\s*$/, "").trim();
    lookup[noPage] = s;
    // Server emits en-dash. Register dot- and em-dash variants so LLM
    // citations using any of the three separators still resolve.
    lookup[noPage.replace(/\s+–\s+/g, " · ")] = s;
    lookup[noPage.replace(/\s+–\s+/g, " — ")] = s;
    lookup[noPage.replace(/\s+·\s+/g, " – ")] = s;

    const sep = noPage.indexOf(" – ");
    const title = sep > -1 ? noPage.slice(0, sep).trim() : noPage;
    titleCount[title] = (titleCount[title] || 0) + 1;
  }

  // Title-only fallback — only register when unambiguous, so we never
  // collapse two sources that share a title but differ by section/page.
  for (const s of sources) {
    const noPage = s.label.replace(/,\s*s\.\s*\d+\s*$/, "").trim();
    const sep = noPage.indexOf(" – ");
    const title = sep > -1 ? noPage.slice(0, sep).trim() : noPage;
    if (titleCount[title] === 1 && !lookup[title]) {
      lookup[title] = s;
    }
  }
  return lookup;
}

// Pull the title (everything before the first " · ") from an inline
// citation. Used as the last-resort matching key when the full text
// doesn't line up with any registered lookup entry.
function inlineCitationTitle(text) {
  const idx = text.indexOf(" · ");
  return (idx > -1 ? text.slice(0, idx) : text).trim();
}

function normalizeCitationText(s) {
  return (s || "")
    .toLowerCase()
    .replace(/^www\.kth\.se:\s*/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

function sourceTitleFromLabel(label) {
  const noPage = (label || "").replace(/,\s*(?:s|p)\.\s*\d+\s*$/, "").trim();
  const sep = noPage.indexOf(" – ");
  return (sep > -1 ? noPage.slice(0, sep) : noPage).trim();
}

function approximateSourceMatch(allSources, inlineTitle) {
  const want = normalizeCitationText(inlineTitle);
  if (!want) return null;
  const candidates = [];
  for (const s of allSources) {
    const have = normalizeCitationText(sourceTitleFromLabel(s?.label || ""));
    if (!have) continue;
    if (have === want || have.includes(want) || want.includes(have)) {
      candidates.push(s);
    }
  }
  if (candidates.length === 1) return candidates[0];
  return null;
}

// Walk the body's inline citations in order:
//   1. assign new sequential numbers to each unique matched source
//      ([1] = first cited, [2] = next new one, ...),
//   2. replace the inline [Title · Section] markers with anchor links
//      that scroll to the corresponding entry in the references list,
//   3. return only the cited sources (not the full top-K from retrieval).
// Citations the LLM emitted that don't match any retrieved source are
// left in the body text as-is so un-grounded claims stay visible.
/** True if `n` is a finite numeric source index (coerces string "1" from JSON). */
function isNumericSourceN(n) {
  if (n === null || n === undefined || n === "") return false;
  const v = Number(n);
  return Number.isFinite(v);
}

function normalizeHttpUrl(u) {
  if (u == null || u === "") return "";
  const s = String(u).trim();
  if (s.startsWith("http://") || s.startsWith("https://")) return s;
  return "";
}

// Accepts http(s) and root-relative paths (e.g. "/docs/markdown/FAQ.md" from
// the local corpus static mount). Use this for link rendering and "is a URL
// already attached" checks; keep `normalizeHttpUrl` for places that must
// filter to externally-fetched URLs only.
function normalizeUrl(u) {
  if (u == null || u === "") return "";
  const s = String(u).trim();
  if (s.startsWith("http://") || s.startsWith("https://")) return s;
  if (s.startsWith("/")) return s;
  return "";
}

function renderBodyWithCitations(body, allSources) {
  const lookup = buildCitationLookup(allSources);
  const byNumber = new Map();
  let maxKnownN = 0;
  for (const s of allSources) {
    if (!s || !isNumericSourceN(s.n)) continue;
    const n = Number(s.n);
    byNumber.set(n, s);
    if (n > maxKnownN) maxKnownN = n;
  }
  const cited = [];                  // sources in citation order, renumbered
  const numberFor = new Map();       // serverN -> newNumber
  const syntheticByLabel = new Map(); // inline label -> synthetic source number

  // Sentinel `CIT<n>MARK` survives the markdown pass below intact, and
  // we swap it for the final anchor link in a second pass.
  const numbered = body.replace(/\[([^\[\]]+?)\]/g, (full, content) => {
    const trimmed = content.trim();
    // If server already numbered inline citations as [N], preserve N.
    const nRaw = Number.parseInt(trimmed, 10);
    if (/^\d+$/.test(trimmed) && Number.isFinite(nRaw) && byNumber.has(nRaw)) {
      const src = byNumber.get(nRaw);
      let n = numberFor.get(nRaw);
      if (!n) {
        n = cited.length + 1;
        numberFor.set(nRaw, n);
        cited.push({ ...src, n });
      }
      return `CIT${n}MARK`;
    }
    const src = lookup[trimmed]
      || lookup[trimmed.replace(/\s+·\s+/g, " – ")]
      || lookup[trimmed.replace(/\s+·\s+/g, " — ")]
      || lookup[trimmed.replace(/\s+—\s+/g, " · ")]
      || lookup[trimmed.replace(/\s+–\s+/g, " · ")]
      || lookup[inlineCitationTitle(trimmed)]
      || approximateSourceMatch(allSources, inlineCitationTitle(trimmed));
    if (!src) {
      // Last-resort fallback: if it looks like an inline source marker,
      // still number it and include it in the references list.
      const looksLikeCitation =
        trimmed.includes(" · ") || trimmed.includes(" — ") || trimmed.includes(" – ");
      if (!looksLikeCitation) return full;
      let syntheticN = syntheticByLabel.get(trimmed);
      if (!syntheticN) {
        syntheticN = maxKnownN + syntheticByLabel.size + 1;
        syntheticByLabel.set(trimmed, syntheticN);
        cited.push({ n: syntheticN, label: trimmed, url: "" });
      }
      return `CIT${syntheticN}MARK`;
    }
    const srcNum = Number(src.n);
    const nk = Number.isFinite(srcNum)
      ? srcNum
      : `L:${normalizeSourceLabel(src.label || "")}`;
    let n = numberFor.get(nk);
    if (!n) {
      n = cited.length + 1;
      numberFor.set(nk, n);
      cited.push({ ...src, n });
    }
    return `CIT${n}MARK`;
  });

  let html = renderMarkdown(numbered);
  html = html.replace(/CIT(\d+)MARK/g, (_, n) =>
    `<a class="citation" href="#cite-${n}">[${n}]</a>`
  );
  return { html, citedSources: cited };
}

// Strip the " – Section" suffix and trailing ", s. N" page from a
// server-formatted label so we can render "Title – Section, s. N".
function parseSourceLabel(label) {
  let title = label;
  let page = null;
  let section = "";
  const pageMatch = title.match(/,\s*(?:s|p)\.\s*(\d+)\s*$/);
  if (pageMatch) {
    page = pageMatch[1];
    title = title.slice(0, pageMatch.index).trim();
  }
  const sectionIdx = title.indexOf(" – ");
  if (sectionIdx > -1) {
    section = title.slice(sectionIdx + 3).trim();
    title = title.slice(0, sectionIdx).trim();
  }
  return { title, section, page };
}

function renderSources(sources, lang) {
  const pageLabel = lang === "en" ? "p." : "s.";
  let out = '<div class="sources">';
  for (const s of sources) {
    const { title, section, page } = parseSourceLabel(s.label);
    const titleHtml = escapeHtml(title);
    const sectionHtml = section ? ` – ${escapeHtml(section)}` : "";
    const pageHtml = page ? `, ${pageLabel} ${escapeHtml(page)}` : "";
    const core = `${titleHtml}${sectionHtml}${pageHtml}`;
    const href = normalizeUrl(s.url);
    const linkBlock = href
      ? `<a href="${escapeAttr(href)}" target="_blank" rel="noopener">${core}</a>`
      : core;
    out += `<div class="source-item" id="cite-${s.n}"><span class="source-num">[${s.n}]</span> ${linkBlock}</div>`;
  }
  out += "</div>";
  return out;
}

function normalizeSourceLabel(label) {
  return (label || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/\s+·\s+/g, " – ")
    .trim();
}

function backfillMissingSourceUrls(citedSources, allSources) {
  if (!Array.isArray(citedSources) || !Array.isArray(allSources)) return citedSources || [];
  const byNum = new Map();
  const byLabel = new Map();
  const byTitle = new Map();
  for (const s of allSources) {
    const u = normalizeUrl(s?.url);
    if (!s || !u) continue;
    if (isNumericSourceN(s.n)) byNum.set(Number(s.n), u);
    const key = normalizeSourceLabel(s.label);
    if (key && !byLabel.has(key)) byLabel.set(key, u);
    const titleKey = normalizeCitationText(sourceTitleFromLabel(s.label));
    if (titleKey && !byTitle.has(titleKey)) byTitle.set(titleKey, u);
  }
  return citedSources.map((s) => {
    if (!s || normalizeUrl(s.url)) return s;
    let url = "";
    if (isNumericSourceN(s.n)) url = byNum.get(Number(s.n)) || "";
    if (!url) url = byLabel.get(normalizeSourceLabel(s.label)) || "";
    if (!url) {
      const titleKey = normalizeCitationText(inlineCitationTitle(s.label || ""));
      url = byTitle.get(titleKey) || "";
    }
    return url ? { ...s, url } : s;
  });
}

/** Attach URLs from the SSE `sources` list (authoritative) when citation rows lack them. */
function mergeCitedUrlsFromServerMeta(citedSources, serverSources) {
  if (!Array.isArray(citedSources) || !citedSources.length) return citedSources || [];
  if (!Array.isArray(serverSources) || !serverSources.length) return citedSources;
  const byMetaN = new Map();
  const byNormLabel = new Map();
  for (const s of serverSources) {
    if (!s) continue;
    const u = normalizeUrl(s.url);
    if (!u) continue;
    if (isNumericSourceN(s.n)) byMetaN.set(Number(s.n), u);
    if (typeof s.label === "string") {
      const k = normalizeSourceLabel(s.label);
      if (k && !byNormLabel.has(k)) byNormLabel.set(k, u);
    }
  }
  return citedSources.map((c, i) => {
    if (!c) return c;
    if (normalizeUrl(c.url)) return c;
    let url = "";
    if (isNumericSourceN(c.n)) url = byMetaN.get(Number(c.n)) || "";
    if (!url) url = byNormLabel.get(normalizeSourceLabel(c.label || "")) || "";
    if (!url && citedSources.length === 1) {
      const first = normalizeUrl(serverSources[0]?.url);
      if (first) url = first;
    }
    if (!url && serverSources[i]) url = normalizeUrl(serverSources[i].url) || "";
    return url ? { ...c, url } : c;
  });
}

/** Last resort: `meta.source_urls` from dynamic-web turns (program code in label → path). */
function mergeSourceUrlsHeuristic(citedSources, sourceUrls) {
  if (!Array.isArray(citedSources) || !citedSources.length) return citedSources || [];
  // `meta.source_urls` only contains externally-fetched URLs from the
  // dynamic-web pipeline; keep the http(s) filter so we don't synthesise a
  // local-static URL into a citation that has no externally-fetched origin.
  const urls = [...new Set((sourceUrls || []).map(normalizeHttpUrl).filter(Boolean))];
  if (!urls.length) return citedSources;
  return citedSources.map((c) => {
    if (!c || normalizeUrl(c.url)) return c;
    let url = "";
    if (urls.length === 1) {
      url = urls[0];
    } else {
      const label = c.label || "";
      const m = label.match(/\b([A-Z]{5})\b/);
      if (m) {
        const code = m[1];
        url = urls.find((u) => u.toUpperCase().includes(`/${code}/`)) || "";
      }
    }
    return url ? { ...c, url } : c;
  });
}

function renderTip(tip) {
  // Strip the surrounding markdown italic underscores.
  const inner = tip.replace(/^_+|_+$/g, "").trim();
  return `<div class="tip">${escapeHtml(inner)}</div>`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}
function escapeAttr(s) {
  return (s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function renderMarkdown(text) {
  // Small markdown subset used in chat:
  // - paragraphs + hard line breaks
  // - unordered + ordered lists
  // - ATX headings (# .. ######)
  // - horizontal rules (--- / *** / ___ on a line of their own)
  // - **bold**, _italic_, links
  //
  // Intentionally *not* a full markdown parser; keep deterministic and safe.
  const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));

  const renderInline = (s) => {
    let out = esc(s);
    out = out.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g,
      (_, lbl, href) => `<a href="${href}" target="_blank" rel="noopener">${lbl}</a>`
    );
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/_([^_]+)_/g, "<em>$1</em>");
    return out;
  };

  const lines = (text || "").split("\n");
  let html = "";
  let para = [];
  let listKind = ""; // "", "ul", "ol"
  let listItems = [];

  const flushPara = () => {
    if (!para.length) return;
    html += `<p>${para.map(renderInline).join("<br>")}</p>`;
    para = [];
  };

  const flushList = () => {
    if (!listKind || !listItems.length) {
      listKind = "";
      listItems = [];
      return;
    }
    const itemsHtml = listItems.map((it) => `<li>${renderInline(it)}</li>`).join("");
    html += `<${listKind}>${itemsHtml}</${listKind}>`;
    listKind = "";
    listItems = [];
  };

  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/, ""); // trim end only
    // Headings: `#` to `######` followed by a space and content. Check
    // before list/hr so a `# heading` line doesn't get re-parsed as text.
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    // Horizontal rule: a line that is ONLY 3+ of `-`, `*`, or `_`. The
    // anchored `\s*$` keeps `***bold***` and `- list item` out.
    const hr = line.match(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/);
    const ul = line.match(/^\s*[*-]\s+(.+)$/);
    const ol = line.match(/^\s*(\d+)\.\s+(.+)$/);

    if (heading) {
      flushPara();
      flushList();
      const level = heading[1].length;
      html += `<h${level}>${renderInline(heading[2])}</h${level}>`;
      continue;
    }

    if (hr) {
      flushPara();
      flushList();
      html += "<hr>";
      continue;
    }

    if (ul || ol) {
      flushPara();
      const kind = ul ? "ul" : "ol";
      if (listKind && listKind !== kind) flushList();
      listKind = kind;
      listItems.push((ul ? ul[1] : ol[2]) || "");
      continue;
    }

    // Blank line ends current block(s).
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }

    // Non-list line: if we were in a list, end it and start a paragraph.
    if (listKind) flushList();
    para.push(line);
  }

  flushPara();
  flushList();
  return html;
}

// -----------------------------------------------------------------------------
// Conversation export — write the in-memory thread to a Markdown blob the user
// can save. The format is hand-rolled (no JS dep) and consciously preserves:
//   - Speaker labels with timestamps (date + HH:MM in local time)
//   - In-message body formatting (Markdown already; copied verbatim)
//   - Numbered sources list with URLs after each bot turn
//   - Reactions only when the user actually clicked 👍/👎
// And consciously excludes:
//   - Literacy tip footers (.tip)
//   - The 👍/👎 buttons themselves (we record `reaction` instead)
//   - Thinking-phase / streaming-only UI

function refreshExportButton() {
  const btn = document.getElementById("export");
  if (!btn) return;
  const has = state.thread && state.thread.length > 0;
  btn.hidden = !has;
}

function pad2(n) { return n < 10 ? `0${n}` : `${n}`; }

function formatExportTs(ts) {
  const d = new Date(ts);
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  );
}

function exportFilename(firstTs) {
  const d = new Date(firstTs || Date.now());
  return (
    `student-bot-conversation-${d.getFullYear()}-${pad2(d.getMonth() + 1)}-` +
    `${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}.md`
  );
}

function exportBotName() {
  return botDisplayName();
}

function exportUserName() {
  const fallback = (window.t && window.t("export.user_default")) || "Användare";
  return state.name && state.name.trim() ? state.name.trim() : fallback;
}

function exportSourcesHeading() {
  return (window.t && window.t("export.sources_heading")) || "Källor";
}

function exportSourceLine(src) {
  // Mirror `parseSourceLabel` in renderSources: split " – Section" suffix and
  // trailing ", s. N" page so the markdown text reads as in the chat bubble.
  const m = (src.label || "").match(/,\s*(?:s|p)\.\s*(\d+)\s*$/);
  let title = src.label || "";
  let page = "";
  if (m) { page = m[1]; title = title.slice(0, m.index).trim(); }
  let section = "";
  const sepIdx = title.indexOf(" – ");
  if (sepIdx > -1) { section = title.slice(sepIdx + 3).trim(); title = title.slice(0, sepIdx).trim(); }
  let body = title;
  if (section) body += ` – ${section}`;
  if (page) body += `, s. ${page}`;
  const href = (src.url || "").trim();
  return href ? `[${body}](${href})` : body;
}

function exportThreadMarkdown() {
  if (!state.thread.length) return;
  const lines = [];
  const firstTs = state.thread[0].ts;
  for (const entry of state.thread) {
    if (entry.role === "user") {
      lines.push(`### ${exportUserName()} · ${formatExportTs(entry.ts)}`);
      lines.push("");
      lines.push(entry.text);
      lines.push("");
      lines.push("---");
      lines.push("");
      continue;
    }
    // Bot turn
    let header = `### ${exportBotName()} · ${formatExportTs(entry.ts)}`;
    if (entry.confidenceText) header += ` · _${entry.confidenceText}_`;
    lines.push(header);
    lines.push("");
    if (entry.jargon) {
      lines.push(`> ${entry.jargon}`);
      lines.push("");
    }
    if (entry.body) {
      lines.push(entry.body);
      lines.push("");
    }
    if (entry.reaction === "positive") { lines.push("👍"); lines.push(""); }
    else if (entry.reaction === "negative") { lines.push("👎"); lines.push(""); }
    if (entry.sources && entry.sources.length) {
      lines.push(`**${exportSourcesHeading()}:**`);
      const numbered = entry.sources
        .slice()
        .sort((a, b) => (Number(a.n) || 0) - (Number(b.n) || 0));
      let n = 1;
      for (const s of numbered) {
        lines.push(`${n}. ${exportSourceLine(s)}`);
        n += 1;
      }
      lines.push("");
    }
    lines.push("---");
    lines.push("");
  }
  const md = lines.join("\n").replace(/\n{3,}/g, "\n\n").trimEnd() + "\n";
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = exportFilename(firstTs);
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Defer revoke so Safari has time to read the blob.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// -----------------------------------------------------------------------------
// Debug panel — "Learn more about how this chatbot works"
//
// Per-turn diagnostic surface that mirrors the JSON payload assembled by
// pipeline._build_debug_payload. Opt-in: visible only when the user
// checked "Show how the bot thinks" in onboarding (or flipped it on later
// from the chat header row). For owned turns inline-cached from the meta
// event, rendering is instant; older turns fall back to /api/debug/{qa_id}.

const debugPanel = el("#debug-panel");
const debugPanelBody = el("#debug-panel-body");
const debugPanelEmpty = el("#debug-panel-empty");

async function showDebugPanel(qaId) {
  if (!debugPanel || !debugPanelBody) return;
  setView("debug");
  if (debugPanelEmpty) debugPanelEmpty.classList.add("hidden");

  let payload = state.debugCache.get(qaId);
  if (!payload) {
    debugPanelBody.innerHTML =
      `<p class="debug-loading">${escapeHtml(
        (window.t && window.t("debug.msg.details")) || "Loading…"
      )}</p>`;
    try {
      const url = new URL("api/debug/" + encodeURIComponent(qaId), location.href);
      url.searchParams.set("session_id", state.sessionId);
      if (state.name) url.searchParams.set("name", state.name);
      const resp = await fetch(url.toString(), { credentials: "include" });
      if (!resp.ok) {
        // 404 = no qa_debug row (toggle was off for this turn, opted out,
        // or guardrail short-circuit). Show the dedicated empty message
        // rather than the generic error so the reason is obvious.
        if (resp.status === 404) {
          debugPanelBody.innerHTML = "";
          if (debugPanelEmpty) debugPanelEmpty.classList.remove("hidden");
          return;
        }
        const tpl =
          (window.t && window.t("debug.fetch_error")) ||
          "Could not load details (error {status}).";
        debugPanelBody.innerHTML =
          `<p class="debug-error">${escapeHtml(
            tpl.replace("{status}", String(resp.status))
          )}</p>`;
        return;
      }
      const data = await resp.json();
      payload = data && data.payload;
      if (payload) state.debugCache.set(qaId, payload);
    } catch (e) {
      const tpl =
        (window.t && window.t("debug.fetch_error")) ||
        "Could not load details (error {status}).";
      debugPanelBody.innerHTML =
        `<p class="debug-error">${escapeHtml(tpl.replace("{status}", e.message || "?"))}</p>`;
      return;
    }
  }

  if (!payload) {
    debugPanelBody.innerHTML = "";
    if (debugPanelEmpty) debugPanelEmpty.classList.remove("hidden");
    return;
  }
  renderDebugPayload(payload, qaId);
}

function renderDebugPayload(payload, qaId) {
  const t = window.t || ((k) => k);
  const sections = [];

  // --- Routing
  const routing = payload.routing || {};
  const routingRows = [
    [t("debug.field.lang"), routing.lang || "–"],
    [
      t("debug.field.expanded_query"),
      routing.expanded_query
        ? escapeHtml(routing.expanded_query)
        : "<em>(none)</em>",
    ],
    [
      t("debug.field.jargon"),
      (routing.jargon_hits || []).length
        ? (routing.jargon_hits || [])
            .map((j) => escapeHtml(j.term || j.key || ""))
            .join(", ")
        : "<em>(none)</em>",
    ],
  ];
  sections.push(debugSection(t("debug.section.routing"), kvRows(routingRows)));

  // --- Gate
  const gate = payload.gate || {};
  const gateRows = [
    [t("debug.field.pass"), gate.pass ? "✓" : "✗"],
    [t("debug.field.reason"), escapeHtml(gate.reason || "–")],
    [t("debug.field.top1"), fmtNum(gate.top1)],
    [t("debug.field.meanK"), fmtNum(gate.meanK)],
    [t("debug.field.distinct"), gate.distinct_sources ?? "–"],
  ];
  sections.push(debugSection(t("debug.section.gate"), kvRows(gateRows)));

  // --- Stages
  const stages = payload.stages || {};
  const host = payload.host || {};
  const stageRows = [
    [t("debug.field.chroma_ms"), stages.chroma_ms ?? "–"],
    [t("debug.field.rerank_ms"), stages.rerank_ms ?? "–"],
    [t("debug.field.llm_ms"), stages.llm_ms ?? "–"],
    [t("debug.field.rss_mb"), host.rss_mb ?? "–"],
  ];
  sections.push(debugSection(t("debug.section.stages"), kvRows(stageRows)));

  // --- Retrieval (two tables, candidates + reranked)
  const retrieval = payload.retrieval || {};
  const candidates = retrieval.candidates || [];
  const reranked = retrieval.reranked || [];
  const retrievalHtml =
    `<details class="debug-collapse" open><summary>${escapeHtml(
      t("debug.section.reranked")
    )} (${reranked.length})</summary>${chunkTable(reranked, true)}</details>` +
    `<details class="debug-collapse"><summary>${escapeHtml(
      t("debug.section.candidates")
    )} (${candidates.length})</summary>${chunkTable(candidates, false)}</details>`;
  sections.push(debugSection(t("debug.section.retrieval"), retrievalHtml));

  // --- LLM
  const llm = payload.llm || {};
  const llmRows = [
    [t("debug.field.model"), escapeHtml(llm.model || "–")],
    [t("debug.field.prompt_tokens"), llm.prompt_tokens_est ?? "–"],
  ];
  // Gate-failed turns take the meta_fallback path where the last user
  // message is the bare question (no retrieved chunks injected). Pass
  // gate.pass through so the role label reflects that.
  const llmMessagesHtml = renderLlmMessages(llm.messages || [], !!gate.pass);
  sections.push(
    debugSection(
      t("debug.section.llm"),
      kvRows(llmRows) + llmMessagesHtml
    )
  );

  debugPanelBody.innerHTML =
    `<div class="debug-meta"><span>qa_id: <code>${escapeHtml(
      String(qaId)
    )}</code></span></div>` + sections.join("");
}

function debugSection(title, innerHtml) {
  return (
    `<section class="debug-section">` +
    `<h3>${escapeHtml(title)}</h3>` +
    innerHtml +
    `</section>`
  );
}

function kvRows(rows) {
  return (
    `<dl class="debug-kv">` +
    rows
      .map(
        ([k, v]) =>
          `<dt>${escapeHtml(k)}</dt><dd>${v === null || v === undefined ? "–" : v}</dd>`
      )
      .join("") +
    `</dl>`
  );
}

function chunkTable(chunks, includeRerank) {
  if (!chunks || !chunks.length) return `<p class="debug-empty-inline">–</p>`;
  const t = window.t || ((k) => k);
  const headers = [
    t("debug.col.doc"),
    t("debug.col.section"),
    t("debug.col.distance"),
    ...(includeRerank ? [t("debug.col.rerank")] : []),
    t("debug.col.snippet"),
  ];
  const rows = chunks
    .map((c) => {
      const distance = fmtNum(c.chroma_distance);
      const rerank = includeRerank ? `<td>${fmtNum(c.rerank_score)}</td>` : "";
      const page = c.page ? `, p.${escapeHtml(String(c.page))}` : "";
      const docCell =
        `<div class="debug-doc-title">${escapeHtml(c.doc_title || c.id || "")}</div>` +
        `<div class="debug-doc-src"><code>${escapeHtml(
          (c.rel_source || c.id || "") + page
        )}</code></div>`;
      return (
        `<tr>` +
        `<td>${docCell}</td>` +
        `<td>${escapeHtml(c.section_path || "")}</td>` +
        `<td>${distance}</td>` +
        rerank +
        `<td class="debug-snippet">${escapeHtml(c.snippet || "")}</td>` +
        `</tr>`
      );
    })
    .join("");
  return (
    `<table class="debug-table">` +
    `<thead><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows}</tbody></table>`
  );
}

function renderLlmMessages(messages, gatePassed) {
  if (!messages || !messages.length) return "";
  const items = messages
    .map((m, i) => {
      const label = debugRoleLabel(m.role, i, messages.length, gatePassed);
      const content = escapeHtml(String(m.content || ""));
      return (
        `<details class="debug-msg"><summary>${escapeHtml(label)}` +
        `<span class="debug-msg-len"> · ${content.length} ch</span></summary>` +
        `<pre>${content}</pre></details>`
      );
    })
    .join("");
  return `<div class="debug-msgs">${items}</div>`;
}

// Map raw OpenAI/Ollama role names to descriptive labels for non-RAG-savvy
// readers. The last user message is the "current turn" — composite of the
// retrieved excerpts and the question on the main path, or just the bare
// question on the gate-failed meta_fallback path. Anything before that is
// history from ConversationMemory (bare Q&A pairs, no retrieval attached).
function debugRoleLabel(role, index, total, gatePassed) {
  const t = window.t || ((k) => k);
  if (role === "system") return t("debug.role.system");
  const isLast = index === total - 1;
  if (role === "user" && isLast) {
    return gatePassed
      ? t("debug.role.current_user_rag")
      : t("debug.role.current_user_bare");
  }
  if (role === "user") return t("debug.role.history_user");
  if (role === "assistant") return t("debug.role.history_assistant");
  return role;
}

function fmtNum(v) {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  if (!Number.isFinite(n)) return "–";
  return n.toFixed(Math.abs(n) >= 100 ? 1 : 3);
}

// Skip onboarding if name already set.
if (state.name) { el("#start").click(); }

// Admin deep-link from /stats: `#debug=<qa_id>` opens the panel for that
// qa_id directly. The fetch path applies the same ownership / admin check
// as a normal /api/debug/{qa_id} call, so non-admins clicking another
// user's link cleanly fall through to a 403.
function maybeOpenDebugFromHash() {
  const m = (location.hash || "").match(/^#debug=(\d+)/);
  if (!m) return;
  const qaId = Number(m[1]);
  if (!Number.isFinite(qaId)) return;
  // Admin deep-links may arrive with learn-more off; enable it so the tab
  // bar appears and the user can switch back to chat.
  if (!state.learnMore) syncLearnMore(true);
  // Defer one tick so the chat section is visible (start-click above runs
  // synchronously, but the css transition + i18n pass shouldn't fight us).
  setTimeout(() => showDebugPanel(qaId), 0);
}
maybeOpenDebugFromHash();
window.addEventListener("hashchange", maybeOpenDebugFromHash);
