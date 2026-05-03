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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: state.name, session_id: state.sessionId, opt_out: state.optOut }),
  }).catch(() => {});
  onboarding.classList.add("hidden");
  chat.classList.remove("hidden");
  el("#question").focus();
});

el("#reset").addEventListener("click", async () => {
  await fetch("/api/reset", {
    method: "POST",
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
  statusEl.textContent = window.t ? window.t("chat.thinking") : "tänker…";

  let buf = "";
  let finalMeta = null;
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
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
          botMsg.body.textContent += data;
          messages.scrollTop = messages.scrollHeight;
        } else if (event === "meta") {
          try { finalMeta = JSON.parse(data); } catch {}
        }
      }
    }
  } catch (err) {
    botMsg.body.textContent += `\n[stream error: ${err.message}]`;
  } finally {
    el("#send").disabled = false;
    statusEl.textContent = "";
  }

  if (finalMeta) {
    decorateBot(botMsg, finalMeta);
  }
});

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
  meta.textContent = "studybot";
  const body = document.createElement("div");
  body.className = "body";
  wrap.appendChild(meta);
  wrap.appendChild(body);
  messages.appendChild(wrap);
  return { wrap, meta, body };
}

function decorateBot(botMsg, meta) {
  // Confidence badge.
  if (meta.confidence) {
    const badge = document.createElement("span");
    badge.className = `conf ${meta.confidence_level}`;
    badge.textContent = meta.confidence;
    botMsg.meta.appendChild(badge);
  }
  // Linkify the markdown in the body. We rendered plain text during stream;
  // now replace the body with HTML that handles markdown links + bold.
  botMsg.body.innerHTML = renderMarkdown(botMsg.body.textContent);

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

function renderMarkdown(text) {
  // very small markdown subset: **bold**, _italic_, links, line breaks.
  const esc = (s) => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  let out = esc(text);
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g,
    (_, lbl, href) => `<a href="${href}" target="_blank" rel="noopener">${lbl}</a>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/_([^_]+)_/g, "<em>$1</em>");
  out = out.replace(/\n/g, "<br>");
  return out;
}

// Skip onboarding if name already set.
if (state.name) { el("#start").click(); }
