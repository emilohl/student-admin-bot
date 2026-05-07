// Minimal vanilla-JS chat client. Streams answers via SSE; renders source
// links in the answer body; supports 👍/👎 feedback per message.

const state = {
  sessionId: localStorage.getItem("session_id") || crypto.randomUUID(),
  name: localStorage.getItem("name") || "",
  optOut: localStorage.getItem("opt_out") === "1",
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

el("#start").addEventListener("click", () => {
  state.name = el("#name").value.trim() || "Anonym";
  state.optOut = el("#opt-out").checked;
  localStorage.setItem("name", state.name);
  localStorage.setItem("opt_out", state.optOut ? "1" : "0");
  // Tell the server about the name + opt-out preference.
  fetch("/api/session", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: state.name, session_id: state.sessionId, opt_out: state.optOut }),
  }).catch(() => {});
  onboarding.classList.add("hidden");
  chat.classList.remove("hidden");
  el("#question").focus();
  if (perfEnabled) refreshSystemLoad();
});

el("#reset").addEventListener("click", async () => {
  await fetch("/api/reset", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.sessionId }),
  }).catch(() => {});
  messages.innerHTML = "";
  statusEl.textContent = window.t ? window.t("chat.newthread") : "ny tråd";
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

  const botMsg = appendBot();
  let firstToken = true;
  const reqStartedAt = performance.now();
  let firstTokenAt = null;

  let buf = "";
  let finalMeta = null;
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        name: state.name,
        session_id: state.sessionId,
        opt_out: state.optOut,
      }),
    });
    if (!resp.ok || !resp.body) {
      botMsg.body.textContent = `[error ${resp.status}]`;
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
            // Replace the animated thinking indicator with real content.
            botMsg.body.textContent = "";
            firstToken = false;
            firstTokenAt = performance.now();
          }
          botMsg.body.textContent += data;
          messages.scrollTop = messages.scrollHeight;
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
    if (firstToken) botMsg.body.textContent = "";
    botMsg.body.textContent += `\n[stream error: ${err.message}]`;
  } finally {
    el("#send").disabled = false;
  }

  if (finalMeta) {
    if (Object.prototype.hasOwnProperty.call(finalMeta, "performance_panel_enabled")) {
      setPerfEnabled(!!finalMeta.performance_panel_enabled);
    }
    if (firstTokenAt && botMsg.body.textContent) {
      const tokEst = estimateTokens(botMsg.body.textContent);
      const genSecs = Math.max(0.001, (performance.now() - firstTokenAt) / 1000);
      finalMeta.client_ttft_ms = Math.max(0, Math.round(firstTokenAt - reqStartedAt));
      finalMeta.client_tps = tokEst / genSecs;
    }
    decorateBot(botMsg, finalMeta);
    if (perfEnabled) updatePerfPanel(finalMeta);
  }
});

function setPerfEnabled(enabled) {
  perfEnabled = !!enabled;
  if (!perfPanelEl) return;
  perfPanelEl.classList.toggle("hidden", !perfEnabled);
}

function estimateTokens(text) {
  return Math.max(0, Math.round((text || "").length / 4));
}

function formatPct(v) {
  return Number.isFinite(v) ? `${v.toFixed(0)}%` : "—";
}

function updatePerfPanel(meta) {
  if (!perfEnabled) return;
  const used = meta.context_tokens_est;
  const limit = meta.context_tokens_limit;
  if (Number.isFinite(used) && Number.isFinite(limit) && limit > 0) {
    const pct = Math.max(0, Math.min(100, (used / limit) * 100));
    perfContextEl.textContent = `${used}/${limit} (${pct.toFixed(0)}%)`;
  } else {
    perfContextEl.textContent = "—";
  }

  const ttft = Number.isFinite(meta.ttft_ms) ? meta.ttft_ms : meta.client_ttft_ms;
  const tps = Number.isFinite(meta.gen_tps) ? meta.gen_tps : meta.client_tps;
  const tok = meta.gen_tokens_est;
  const parts = [];
  if (Number.isFinite(ttft)) parts.push(`TTFT ${formatDuration(ttft)}`);
  if (Number.isFinite(tps)) parts.push(`${tps.toFixed(1)} tok/s`);
  if (Number.isFinite(tok)) parts.push(`~${tok} tok`);
  perfTokensEl.textContent = parts.length ? parts.join(" · ") : "—";

  applyMergedSystemLoad(meta.system_load, meta.host_system_load);
}

function formatDuration(ms) {
  if (!Number.isFinite(ms)) return "—";
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
    `<span class="host">Host</span> / <span class="cont">Container</span> · ` +
    `CPU: <span class="host">${hCpu}</span> / <span class="cont">${cCpu}</span> · ` +
    `RAM: <span class="host">${hMem}</span> / <span class="cont">${cMem}</span>`
  );
}

async function refreshSystemLoad() {
  if (!perfEnabled) return;
  try {
    const resp = await fetch("/api/system-load", { credentials: "include" });
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

async function initPerf() {
  try {
    const resp = await fetch("/api/health", { credentials: "include" });
    if (!resp.ok) return;
    const data = await resp.json();
    setPerfEnabled(!!data.performance_panel_enabled);
  } catch (_) {
    setPerfEnabled(false);
  }
}

initPerf();

function parseSSE(block) {
  let event = "token", data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).replace(/^\s/, "") + "\n";
  }
  return { event, data: data.replace(/\n$/, "") };
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
  // Animated three-dot indicator while we wait for the first token.
  // The label sits next to the dots and shows "<bot> funderar…" while
  // the model is in its <think>...</think> reasoning phase.
  body.innerHTML =
    '<div class="thinking-wrap" aria-live="polite">' +
      '<span class="thinking-dots" aria-label="thinking">' +
        '<span></span><span></span><span></span>' +
      '</span>' +
      '<span class="thinking-label" hidden></span>' +
    '</div>';
  wrap.appendChild(meta);
  wrap.appendChild(body);
  messages.appendChild(wrap);
  messages.scrollTop = messages.scrollHeight;
  return { wrap, meta, body };
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
  const raw = botMsg.body.textContent;
  const { body, sources: allSources, tip } = splitMessage(raw);
  const serverSources = Array.isArray(meta.sources) ? meta.sources : [];
  const effectiveSources = allSources.length ? allSources : serverSources;
  const bodyForCitations = (meta.numbered_body || "").trim() || body;

  const { html: bodyHtml, citedSources } = renderBodyWithCitations(bodyForCitations, effectiveSources);
  let html = bodyHtml;
  // Show only sources that were actually cited inline.
  // This avoids surfacing unrelated retrieved chunks as references.
  if (citedSources.length) {
    html += renderSources(citedSources, meta.lang || "sv");
  } else if (serverSources.length) {
    // Fallback: if inline citation matching fails, still show the
    // authoritative server-provided references.
    html += renderSources(serverSources, meta.lang || "sv");
  }
  if (tip) html += renderTip(tip);
  botMsg.body.innerHTML = html;

  // Feedback buttons (only if we have a qa id).
  if (meta.qa_id) {
    const actions = document.createElement("div");
    actions.className = "actions";
    const up = document.createElement("button");
    up.className = "up"; up.textContent = "👍";
    const down = document.createElement("button");
    down.className = "down"; down.textContent = "👎";
    const send = (sentiment, btn) => {
      fetch("/api/feedback", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ qa_id: meta.qa_id, sentiment }),
      });
      up.classList.remove("active");
      down.classList.remove("active");
      btn.classList.add("active");
    };
    up.addEventListener("click", () => send("positive", up));
    down.addEventListener("click", () => send("negative", down));
    actions.appendChild(up);
    actions.appendChild(down);
    botMsg.wrap.appendChild(actions);
  }
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
    lookup[noPage.replace(/\s+—\s+/, " · ")] = s;
    lookup[noPage.replace(/\s+·\s+/, " — ")] = s;

    const sep = noPage.indexOf(" — ");
    const title = sep > -1 ? noPage.slice(0, sep).trim() : noPage;
    titleCount[title] = (titleCount[title] || 0) + 1;
  }

  // Title-only fallback — only register when unambiguous, so we never
  // collapse two sources that share a title but differ by section/page.
  for (const s of sources) {
    const noPage = s.label.replace(/,\s*s\.\s*\d+\s*$/, "").trim();
    const sep = noPage.indexOf(" — ");
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

// Walk the body's inline citations in order:
//   1. assign new sequential numbers to each unique matched source
//      ([1] = first cited, [2] = next new one, ...),
//   2. replace the inline [Title · Section] markers with anchor links
//      that scroll to the corresponding entry in the references list,
//   3. return only the cited sources (not the full top-K from retrieval).
// Citations the LLM emitted that don't match any retrieved source are
// left in the body text as-is so un-grounded claims stay visible.
function renderBodyWithCitations(body, allSources) {
  const lookup = buildCitationLookup(allSources);
  const byNumber = new Map();
  for (const s of allSources) {
    if (Number.isFinite(s?.n)) byNumber.set(Number(s.n), s);
  }
  const cited = [];                  // sources in citation order, renumbered
  const numberFor = new Map();       // serverN -> newNumber

  // Sentinel `CIT<n>MARK` survives the markdown pass below intact, and
  // we swap it for the final anchor link in a second pass.
  const numbered = body.replace(/\[([^\[\]]+?)\]/g, (full, content) => {
    const trimmed = content.trim();
    // If server already numbered inline citations as [N], preserve N.
    const nRaw = Number.parseInt(trimmed, 10);
    if (/^\d+$/.test(trimmed) && Number.isFinite(nRaw) && byNumber.has(nRaw)) {
      const src = byNumber.get(nRaw);
      if (!cited.some((x) => x.n === src.n)) cited.push(src);
      return `CIT${nRaw}MARK`;
    }
    const src = lookup[trimmed]
      || lookup[trimmed.replace(/\s+·\s+/, " — ")]
      || lookup[trimmed.replace(/\s+—\s+/, " · ")]
      || lookup[inlineCitationTitle(trimmed)];
    if (!src) return full;
    let n = numberFor.get(src.n);
    if (!n) {
      n = cited.length + 1;
      numberFor.set(src.n, n);
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

// Strip the " — Section" suffix and trailing ", s. N" page from a
// server-formatted label so we can render "Title — Section, s. N".
function parseSourceLabel(label) {
  let title = label;
  let page = null;
  let section = "";
  const pageMatch = title.match(/,\s*(?:s|p)\.\s*(\d+)\s*$/);
  if (pageMatch) {
    page = pageMatch[1];
    title = title.slice(0, pageMatch.index).trim();
  }
  const sectionIdx = title.indexOf(" — ");
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
    const link = s.url
      ? `<a href="${escapeAttr(s.url)}" target="_blank" rel="noopener">${titleHtml}</a>`
      : titleHtml;
    const sectionHtml = section ? ` — ${escapeHtml(section)}` : "";
    const pageHtml = page ? `, ${pageLabel} ${escapeHtml(page)}` : "";
    out += `<div class="source-item" id="cite-${s.n}"><span class="source-num">[${s.n}]</span> ${link}${sectionHtml}${pageHtml}</div>`;
  }
  out += "</div>";
  return out;
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
    const ul = line.match(/^\s*[*-]\s+(.+)$/);
    const ol = line.match(/^\s*(\d+)\.\s+(.+)$/);

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

// Skip onboarding if name already set.
if (state.name) { el("#start").click(); }
