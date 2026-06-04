// Tiny client-side i18n. Loads the lang preference from localStorage
// (default sv), applies translations to elements with data-i18n /
// data-i18n-placeholder / data-i18n-aria attributes, and wires up the
// header language switch. Exposes window.t() for runtime callers
// (e.g. app.js status messages).
(() => {
  const STORE_KEY = "lang";
  const DEFAULT = "sv";

  const T = {
    sv: {
      "brand.name": "Lux - adminbot",
      "header.tagline": "Administrativ Q&A-bot för KTH · baserad på officiella styrdokument och FAQ",

      "notice.title": "Experimentell testtjänst.",
      "notice.body": " Servern har begränsade resurser och språkmodellen är liten – svar kan vara långsamma och inte alltid korrekta. Första frågan kan ta extra lång tid medan modellen laddas. Använd gärna 👍 eller 👎 på svaren – feedback hjälper oss att förbättra tjänsten.",
      "notice.close.aria": "Stäng",

      "onboarding.title": "Hej!",
      "onboarding.intro": "Den här boten svarar på administrativa frågor om civilingenjörsprogrammet i Teknisk fysik, baserat på KTH:s officiella regler och utbildningsplaner. Den använder en lokal språkmodell + retrieval – alla källor kan klickas på under varje svar.",
      "onboarding.caveat": "LLM:er kan ha fel även när de låter säkra. Klicka alltid på källorna och dubbelkolla viktiga detaljer.",
      "onboarding.name.label": "Ditt namn (visas inte för andra; används för loggar):",
      "onboarding.name.placeholder": "Anonym",
      "onboarding.optout": "Logga inte mina frågor (du kan ändra detta när som helst)",
      "onboarding.learnmore": "Visa hur boten tänker (öppnar en panel med routing, källor och språkmodellens kontext)",
      "cloud.notice.onboarding": "⚠️ <strong>Obs:</strong> svaren genereras just nu av en extern molnmodell ({provider}). Dina meddelanden och deras kontext skickas till leverantören för att svaret ska kunna skapas — <strong>skriv inte personnummer, lösenord eller annan känslig personlig information</strong>.",
      "cloud.notice.chat": "⚠️ <strong>Extern molnmodell aktiv:</strong> svar genereras av {provider}. Skriv inte känslig personlig information.",
      "onboarding.start": "Starta",

      "chat.placeholder": "Skriv din fråga på svenska eller engelska – Enter skickar, Shift+Enter ny rad…",
      "chat.reset": "Börja om",
      "chat.reset.confirm": "Vill du börja om? Den nuvarande tråden raderas.",
      "chat.send": "Skicka",
      "chat.thinking": "tänker…",
      "chat.thinking_phase": "funderar…",
      "chat.newthread": "Tråden rensad",
      "chat.logging.label": "Logga mina frågor",
      "chat.logging.tip": "Frågor lagras anonymt (saltad SHA-256 av session-id) för att förbättra boten. Bocka av för att stänga av loggning.",
      "chat.learnmore.label": "Visa hur boten tänker",
      "chat.learnmore.tip": "Öppnar en panel med routing, källor och språkmodellens kontext.",
      "chat.logging.toast.on": "Loggning är på – tack, det hjälper oss förbättra boten",
      "chat.logging.toast.off": "Loggning avstängd för dig",
      "chat.history_truncated": "Tidigare turer skickas inte längre till språkmodellen — den kommer ihåg bara de senaste turerna.",
      "chat.session_expired": "Föregående konversation har gått ut (timeout). Boten börjar från noll.",
      "chat.export": "Exportera",
      "chat.export.title": "Ladda ner konversationen som Markdown",
      "export.user_default": "Användare",
      "export.sources_heading": "Källor",
      "perf.context": "Kontext",
      "perf.tokens": "Generering",
      "perf.system": "System",
      "perf.tip.context": "Uppskattad tokenanvändning i promptkontexten jämfört med modellens context window (num_ctx).",
      "perf.tip.tokens": "Uppskattade genereringsmått: TTFT = time to first token, tok/s = ungefärlig tokens per sekund.",
      "perf.tip.system": "Visar CPU och RAM för container respektive host. GPU visas inte här.",
      "confidence.tip.high": "Hög: retrieval hittade starkt och relevant underlag. Verifiera ändå i källorna.",
      "confidence.tip.medium": "Medel: viss relevans finns, men underlaget är inte entydigt. Dubbelkolla källorna noggrant.",
      "confidence.tip.low": "Låg: svagt eller osäkert underlag. Behandla svaret som preliminärt och verifiera i källorna.",
      "answer.sources": "Källor",

      "footer.about": "Om boten",
      "footer.glossary": "Ordlista",
      "footer.stats": "Statistik",

      "about.title": "Om boten",
      "about.h2.what": "Vad är det här?",
      "about.what.body": "En lokal RAG-bot som svarar på administrativa frågor om CTFYS-programmet, grundad på de officiella dokumenten under <code>docs/corpus</code>.",
      "about.h2.tips": "Fem saker att tänka på när du använder boten",
      "about.tip1": "<strong>Verifiera källan.</strong> Klicka på källänkarna under varje svar och dubbelkolla mot dokumenten – boten kan ha fel även när den låter säker.",
      "about.tip2": "<strong>Flytande språk är inte korrekthet.</strong> Att en LLM låter övertygande betyder inte att den har rätt. Lita på källorna, inte på tonen. Konfidensbadgen vid varje svar visar hur säker retrieval-steget är.",
      "about.tip3": "<strong>Boten har gränser.</strong> Den känner bara till de indexerade dokumenten – inte ditt enskilda fall, inte aktuella personer eller datum utanför dokumenten. För personliga ärenden, kontakta studievägledaren.",
      "about.tip4": "<strong>Du blir loggad – men du kan stänga av det.</strong> Frågor och svar lagras anonymt (med saltad SHA-256 av ditt session-id) för att förbättra boten. Använd loggnings-reglaget ovanför chattfältet (eller bocka i <em>\"Logga inte mina frågor\"</em> i onboardingen). I Mattermost: <code>!logging off</code>.",
      "about.tip5": "<strong>Komplement, inte ersättare.</strong> Boten kan ge snabba svar på välkända frågor; viktiga beslut om dina studier ska du diskutera med en människa.",
      "about.h2.slides": "Hur boten fungerar",
      "about.slides.link": "Öppna presentationen →",
      "about.slides.note": "(reveal.js, öppnas i ny flik)",
      "about.back": "← Tillbaka",

      "glossary.title": "Ordlista",
      "glossary.tagline": "Slang och förkortningar boten förstår. Saknar du något? Föreslå nedan, eller öppna en PR mot <code>dictionary.json</code>.",
      "glossary.th.term": "Term",
      "glossary.th.meaning": "Betydelse",
      "glossary.th.def": "Förklaring",
      "glossary.th.lang": "Språk",
      "glossary.empty": "ingen ordlista ännu",
      "glossary.suggest.h2": "Föreslå en ny term",
      "glossary.suggest.term": "Term:",
      "glossary.suggest.expansion": "Betydelse:",
      "glossary.suggest.definition": "Förklaring (valfritt):",
      "glossary.suggest.lang": "Språk:",
      "glossary.suggest.submit": "Skicka förslag",
      "glossary.status.sending": "skickar…",
      "glossary.status.ok": "tack – förslaget köades för granskning",
      "glossary.status.error": "fel",
      "glossary.back": "← Tillbaka",

      "stats.title": "Statistik",
      "stats.summary": "Loggade frågor: {logged} · Besvarade: {answered} · Genomsnittlig latens: {latency} ms · Anonym räknare (opt-out): {anon}",
      "stats.th.topic": "Ämne",
      "stats.th.n": "N",
      "stats.th.answered": "Besvarade",
      "stats.th.avgms": "Snitt ms",
      "stats.empty": "ingen data än",
      "stats.back": "← Tillbaka",
      "stats.topics.title": "Per ämne",
      "stats.users.title": "Registrerade webbanvändare",
      "stats.users.summary": "{active} av {total} registrerade användare har testat boten",
      "stats.users.th.username": "Användare",
      "stats.users.th.n": "Frågor",
      "stats.users.th.last": "Senast",
      "stats.range.24h": "24 h",
      "stats.range.72h": "72 h",
      "stats.range.14d": "14 dagar",
      "stats.range.90d": "90 dagar",
      "stats.chart.requests": "Frågor över tid",
      "stats.chart.tokens": "Tokens (in/ut)",
      "stats.chart.tokens_hist": "Tokens per fråga",
      "stats.chart.tokens_hist.xlabel": "Tokens per fråga",
      "stats.chart.ttft_hist": "Tid till första token",
      "stats.chart.ttft_hist.xlabel": "ms",
      "stats.chart.ttft_hist.label": "Tid till första token",
      "stats.chart.tps_hist": "Tokens per sekund",
      "stats.chart.tps_hist.xlabel": "Tokens/s",
      "stats.chart.tps_hist.label": "Tokens per sekund",
      "stats.chart.feedback": "Andel 👍",
      "stats.chart.feedback.ylabel": "Andel",
      "stats.chart.requests.ylabel": "Antal frågor",
      "stats.chart.logy": "log(y)",
      "stats.chart.logx": "log(x)",
      "stats.split_by_model": "Dela upp per modell",
      "stats.chart.requests.total": "Totalt",
      "stats.chart.requests.answered": "Besvarade",
      "stats.chart.requests.off_topic": "Off-topic / låg konfidens",
      "stats.chart.requests.guardrail": "Spärrade (för lång / för många)",
      "stats.chart.requests.clarification": "Förtydligande efterfrågades",
      "stats.chart.hist.ylabel": "Antal frågor",
      "stats.unit.tokens": "Tokens",
      "stats.unit.ms": "ms",
      "stats.unit.tps": "Tokens/s",
      "stats.chart.tokens.prompt": "Prompt",
      "stats.chart.tokens.gen": "Genererat",
      "stats.chart.feedback.ratio": "👍 / (👍+👎)",
      "stats.chart.feedback.pos_total": "👍 / svar",
      "stats.chart.feedback.neg_total": "👎 / svar",
      "stats.channel.all": "Totalt",
      "stats.channel.web": "Endast webb",
      "stats.channel.mm": "Endast Mattermost",
      "stats.export.png": "↓ PNG",
      "stats.export.png.title": "Ladda ner som PNG",

      "stats.admin.title": "Adminvy: diagnostik",
      "stats.admin.intro": "Per-stegs latens och RAM-trend per request. Synligt endast för admin-användare.",
      "stats.chart.chroma_hist": "Embed + Chroma (ms)",
      "stats.chart.rerank_hist": "Cross-encoder rerank (ms)",
      "stats.chart.llm_hist": "LLM streaming (ms)",
      "stats.chart.rss_trend": "Process-RAM över tid (MiB)",
      "stats.users.th.inspect": "Inspektera",
      "stats.users.inspect": "Visa turer",
      "stats.inspector.empty": "Inga turer för {user}.",
      "stats.inspector.fetch_error": "Kunde inte hämta turer (fel {status}).",
      "stats.inspector.title": "Senaste turer – {user}",
      "stats.inspector.refused": "spärrad",
      "stats.inspector.open": "Öppna i chatten",

      "tabs.chat": "Chatt",
      "tabs.debug": "Så här tänkte boten",

      "debug.title": "Så här tänkte boten",
      "debug.intro": "Här ser du vilka dokument retrieval hittade, vad spärren beslutade, samt vilken kontext som skickades till språkmodellen. Klicka på 🔍 vid ett svar för att se den turens detaljer.",
      "debug.empty": "Ingen diagnostikdata sparades för denna tur (du var avloggad eller en kortslutning skedde tidigt).",
      "debug.section.routing": "Routing",
      "debug.section.gate": "Spärr (gate)",
      "debug.section.retrieval": "Retrieval",
      "debug.section.candidates": "Topp-N kandidater (före rerank)",
      "debug.section.reranked": "Topp-K efter rerank",
      "debug.section.llm": "Språkmodellens kontext",
      "debug.section.stages": "Tidssteg",
      "debug.section.host": "Värd",
      "debug.field.lang": "Språk",
      "debug.field.jargon": "Jargong",
      "debug.field.expanded_query": "Expanderad fråga",
      "debug.field.pass": "Passerade",
      "debug.field.reason": "Anledning",
      "debug.field.top1": "Topp-1 score",
      "debug.field.meanK": "Medel topp-K",
      "debug.field.distinct": "Distinkta källor",
      "debug.field.model": "Modell",
      "debug.field.prompt_tokens": "Promptens tokens (uppskattat)",
      "debug.field.chroma_ms": "Embed + Chroma (ms)",
      "debug.field.rerank_ms": "Cross-encoder rerank (ms)",
      "debug.field.llm_ms": "LLM streaming (ms)",
      "debug.field.rss_mb": "Process-RAM (MiB)",
      "debug.col.id": "ID",
      "debug.col.doc": "Dokument",
      "debug.col.section": "Sektion",
      "debug.col.distance": "Distance",
      "debug.col.rerank": "Rerank",
      "debug.col.snippet": "Utdrag",
      "debug.msg.details": "🔍 Detaljer",
      "debug.msg.details.title": "Visa hur boten kom fram till detta svar",
      "debug.msg.no_data": "Inga detaljer sparades för denna tur.",
      "debug.fetch_error": "Kunde inte hämta detaljer (fel {status}).",
      "debug.role.system": "System (instruktioner till modellen)",
      "debug.role.history_user": "Tidigare fråga (från användaren)",
      "debug.role.history_assistant": "Tidigare svar (från boten)",
      "debug.role.current_user_rag": "Aktuell fråga + utdrag från dokumenten",
      "debug.role.current_user_bare": "Aktuell fråga",
    },
    en: {
      "brand.name": "Lux - adminbot",
      "header.tagline": "Administrative Q&A bot for KTH · grounded in official steering documents and FAQs",

      "notice.title": "Experimental test service.",
      "notice.body": " The server has limited resources and the language model is small — responses may be slow and not always correct. The first question can take noticeably longer while the model loads. Please use 👍 or 👎 on the replies — feedback helps us improve the service.",
      "notice.close.aria": "Close",

      "onboarding.title": "Hi!",
      "onboarding.intro": "This bot answers administrative questions about the Engineering Physics MSc program, grounded in KTH's official rules and curricula. It uses a local language model + retrieval — every source under each answer is clickable.",
      "onboarding.caveat": "LLMs can be wrong even when they sound confident. Always click the sources and double-check important details.",
      "onboarding.name.label": "Your name (not shown to others; used for logs):",
      "onboarding.name.placeholder": "Anonymous",
      "onboarding.optout": "Don't log my questions (you can change this at any time)",
      "onboarding.learnmore": "Show how the bot thinks (opens a side panel with routing, sources, and the LLM context)",
      "cloud.notice.onboarding": "⚠️ <strong>Note:</strong> answers are currently generated by an external cloud model ({provider}). Your messages and their context are sent to the provider to produce the answer — <strong>don't share personal numbers, passwords, or other sensitive personal information</strong>.",
      "cloud.notice.chat": "⚠️ <strong>External cloud model active:</strong> answers generated by {provider}. Don't share sensitive personal information.",
      "onboarding.start": "Start",

      "chat.placeholder": "Type your question in Swedish or English — Enter sends, Shift+Enter for newline…",
      "chat.reset": "Start over",
      "chat.reset.confirm": "Start over? Your current conversation will be discarded.",
      "chat.send": "Submit",
      "chat.thinking": "thinking…",
      "chat.thinking_phase": "is thinking…",
      "chat.newthread": "thread reset",
      "chat.logging.label": "Log my questions",
      "chat.logging.tip": "Questions are stored anonymously (salted SHA-256 of your session id) to help improve the bot. Untick to stop logging.",
      "chat.learnmore.label": "Show how the bot thinks",
      "chat.learnmore.tip": "Opens a panel with routing, sources, and the LLM context.",
      "chat.logging.toast.on": "Logging on — thanks, that helps us improve the bot",
      "chat.logging.toast.off": "Logging turned off for you",
      "chat.history_truncated": "Earlier turns are no longer sent to the language model — it only remembers the most recent turns.",
      "chat.session_expired": "Your previous session expired (timeout). The bot is starting fresh.",
      "chat.export": "Export",
      "chat.export.title": "Download the conversation as Markdown",
      "export.user_default": "User",
      "export.sources_heading": "Sources",
      "perf.context": "Context",
      "perf.tokens": "Generation",
      "perf.system": "System",
      "perf.tip.context": "Estimated prompt-context token usage versus model context window (num_ctx).",
      "perf.tip.tokens": "Estimated generation metrics: TTFT = time to first token, tok/s = approximate tokens per second.",
      "perf.tip.system": "Shows CPU and RAM for container and host. GPU is omitted here.",
      "confidence.tip.high": "High: retrieval found strong, relevant grounding. Still verify against sources.",
      "confidence.tip.medium": "Medium: some relevant grounding exists, but evidence is mixed. Double-check sources carefully.",
      "confidence.tip.low": "Low: weak or uncertain grounding. Treat the answer as tentative and verify in sources.",
      "answer.sources": "Sources",

      "footer.about": "About",
      "footer.glossary": "Glossary",
      "footer.stats": "Stats",

      "about.title": "About",
      "about.h2.what": "What is this?",
      "about.what.body": "A local RAG bot that answers administrative questions about the CTFYS program, grounded in the official documents under <code>docs/corpus</code>.",
      "about.h2.tips": "Five things to keep in mind when using the bot",
      "about.tip1": "<strong>Verify the source.</strong> Click the source links under each answer and double-check against the documents — the bot can be wrong even when it sounds confident.",
      "about.tip2": "<strong>Fluency is not correctness.</strong> An LLM sounding convincing doesn't mean it's right. Trust the sources, not the tone. The confidence badge on each reply shows how confident the retrieval step was.",
      "about.tip3": "<strong>The bot has limits.</strong> It only knows the indexed documents — not your individual case, not current people or dates outside the documents. For personal matters, contact the study counselor.",
      "about.tip4": "<strong>You're logged — but you can turn it off.</strong> Questions and answers are stored anonymously (with a salted SHA-256 of your session id) to help improve the bot. Use the logging toggle above the chat (or tick <em>\"Don't log my questions\"</em> at onboarding). In Mattermost: <code>!logging off</code>.",
      "about.tip5": "<strong>Complement, not replacement.</strong> The bot can answer well-known questions quickly; important decisions about your studies should be discussed with a human.",
      "about.h2.slides": "How the bot works",
      "about.slides.link": "Open the presentation →",
      "about.slides.note": "(reveal.js, opens in a new tab)",
      "about.back": "← Back",

      "glossary.title": "Glossary",
      "glossary.tagline": "Slang and abbreviations the bot understands. Missing something? Suggest below, or open a PR against <code>dictionary.json</code>.",
      "glossary.th.term": "Term",
      "glossary.th.meaning": "Meaning",
      "glossary.th.def": "Definition",
      "glossary.th.lang": "Language",
      "glossary.empty": "no entries yet",
      "glossary.suggest.h2": "Suggest a new term",
      "glossary.suggest.term": "Term:",
      "glossary.suggest.expansion": "Meaning:",
      "glossary.suggest.definition": "Definition (optional):",
      "glossary.suggest.lang": "Language:",
      "glossary.suggest.submit": "Submit suggestion",
      "glossary.status.sending": "sending…",
      "glossary.status.ok": "thanks — your suggestion has been queued for review",
      "glossary.status.error": "error",
      "glossary.back": "← Back",

      "stats.title": "Stats",
      "stats.summary": "Logged questions: {logged} · Answered: {answered} · Average latency: {latency} ms · Anonymous counter (opt-out): {anon}",
      "stats.th.topic": "Topic",
      "stats.th.n": "N",
      "stats.th.answered": "Answered",
      "stats.th.avgms": "Avg ms",
      "stats.empty": "no data yet",
      "stats.back": "← Back",
      "stats.topics.title": "By topic",
      "stats.users.title": "Registered web users",
      "stats.users.summary": "{active} of {total} registered users have tried the bot",
      "stats.users.th.username": "Username",
      "stats.users.th.n": "Questions",
      "stats.users.th.last": "Last seen",
      "stats.range.24h": "24 h",
      "stats.range.72h": "72 h",
      "stats.range.14d": "14 days",
      "stats.range.90d": "90 days",
      "stats.chart.requests": "Requests over time",
      "stats.chart.tokens": "Tokens (in/out)",
      "stats.chart.tokens_hist": "Tokens per request",
      "stats.chart.tokens_hist.xlabel": "Tokens per request",
      "stats.chart.ttft_hist": "Time to first token",
      "stats.chart.ttft_hist.xlabel": "ms",
      "stats.chart.ttft_hist.label": "Time to first token",
      "stats.chart.tps_hist": "Tokens per second",
      "stats.chart.tps_hist.xlabel": "Tokens/s",
      "stats.chart.tps_hist.label": "Tokens per second",
      "stats.chart.feedback": "👍 fraction",
      "stats.chart.feedback.ylabel": "Fraction",
      "stats.chart.requests.ylabel": "Number of requests",
      "stats.chart.logy": "log(y)",
      "stats.chart.logx": "log(x)",
      "stats.split_by_model": "Split by model",
      "stats.chart.requests.total": "Total",
      "stats.chart.requests.answered": "Answered",
      "stats.chart.requests.off_topic": "Off-topic / low confidence",
      "stats.chart.requests.guardrail": "Blocked (too long / rate-limited)",
      "stats.chart.requests.clarification": "Clarification asked",
      "stats.chart.hist.ylabel": "Number of requests",
      "stats.unit.tokens": "Tokens",
      "stats.unit.ms": "ms",
      "stats.unit.tps": "Tokens/s",
      "stats.chart.tokens.prompt": "Prompt",
      "stats.chart.tokens.gen": "Generated",
      "stats.chart.feedback.ratio": "👍 / (👍+👎)",
      "stats.chart.feedback.pos_total": "👍 / answers",
      "stats.chart.feedback.neg_total": "👎 / answers",
      "stats.channel.all": "Total",
      "stats.channel.web": "Web only",
      "stats.channel.mm": "Mattermost only",
      "stats.export.png": "↓ PNG",
      "stats.export.png.title": "Download as PNG",

      "stats.admin.title": "Admin view: diagnostics",
      "stats.admin.intro": "Per-stage latency and per-request RSS trend. Visible only to admin users.",
      "stats.chart.chroma_hist": "Embed + Chroma (ms)",
      "stats.chart.rerank_hist": "Cross-encoder rerank (ms)",
      "stats.chart.llm_hist": "LLM streaming (ms)",
      "stats.chart.rss_trend": "Process RSS over time (MiB)",
      "stats.users.th.inspect": "Inspect",
      "stats.users.inspect": "Show turns",
      "stats.inspector.empty": "No turns for {user}.",
      "stats.inspector.fetch_error": "Failed to load turns (error {status}).",
      "stats.inspector.title": "Recent turns – {user}",
      "stats.inspector.refused": "refused",
      "stats.inspector.open": "Open in chat",

      "tabs.chat": "Chat",
      "tabs.debug": "How the bot thought",

      "debug.title": "How the bot thought",
      "debug.intro": "Here you can see which documents retrieval found, what the gate decided, and the exact context that was sent to the language model. Click the 🔍 next to a reply to see that turn's details.",
      "debug.empty": "No debug data was saved for this turn (you were opted out or a short-circuit happened early).",
      "debug.section.routing": "Routing",
      "debug.section.gate": "Gate",
      "debug.section.retrieval": "Retrieval",
      "debug.section.candidates": "Top-N candidates (pre-rerank)",
      "debug.section.reranked": "Top-K after rerank",
      "debug.section.llm": "LLM context",
      "debug.section.stages": "Stage timings",
      "debug.section.host": "Host",
      "debug.field.lang": "Language",
      "debug.field.jargon": "Jargon hits",
      "debug.field.expanded_query": "Expanded query",
      "debug.field.pass": "Passed",
      "debug.field.reason": "Reason",
      "debug.field.top1": "Top-1 score",
      "debug.field.meanK": "Mean top-K",
      "debug.field.distinct": "Distinct sources",
      "debug.field.model": "Model",
      "debug.field.prompt_tokens": "Prompt tokens (est.)",
      "debug.field.chroma_ms": "Embed + Chroma (ms)",
      "debug.field.rerank_ms": "Cross-encoder rerank (ms)",
      "debug.field.llm_ms": "LLM streaming (ms)",
      "debug.field.rss_mb": "Process RSS (MiB)",
      "debug.col.id": "ID",
      "debug.col.doc": "Document",
      "debug.col.section": "Section",
      "debug.col.distance": "Distance",
      "debug.col.rerank": "Rerank",
      "debug.col.snippet": "Snippet",
      "debug.msg.details": "🔍 Details",
      "debug.msg.details.title": "See how the bot arrived at this answer",
      "debug.msg.no_data": "No details were saved for this turn.",
      "debug.fetch_error": "Could not load details (error {status}).",
      "debug.role.system": "System (instructions to the model)",
      "debug.role.history_user": "Earlier question (from user)",
      "debug.role.history_assistant": "Earlier reply (from bot)",
      "debug.role.current_user_rag": "Current question + document excerpts",
      "debug.role.current_user_bare": "Current question",
    },
  };

  function currentLang() {
    const v = localStorage.getItem(STORE_KEY);
    return (v === "en" || v === "sv") ? v : DEFAULT;
  }

  function t(key) {
    const lang = currentLang();
    return T[lang]?.[key] ?? T[DEFAULT][key] ?? key;
  }

  // `{name}` tokens in a translation are replaced with the corresponding
  // data-* attribute on the element (e.g. `data-logged="42"` fills `{logged}`).
  // Lets server-rendered numbers/values flow into translatable templates.
  function interpolate(s, el) {
    return s.replace(/\{(\w+)\}/g, (_, k) => (el && el.dataset[k]) ?? "");
  }

  function applyTranslations(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      el.innerHTML = interpolate(t(el.getAttribute("data-i18n")), el);
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
    });
    root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
    });
    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    document.documentElement.lang = currentLang();
    document.title = t("brand.name");
  }

  function updateSwitchUI() {
    const lang = currentLang();
    document.querySelectorAll(".lang-switch button").forEach((btn) => {
      const active = btn.dataset.lang === lang;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setLang(lang) {
    if (lang !== "sv" && lang !== "en") return;
    localStorage.setItem(STORE_KEY, lang);
    applyTranslations();
    updateSwitchUI();
    document.dispatchEvent(new CustomEvent("i18n:langchange", { detail: { lang } }));
  }

  function wireSwitch() {
    document.querySelectorAll(".lang-switch button").forEach((btn) => {
      btn.addEventListener("click", () => setLang(btn.dataset.lang));
    });
    updateSwitchUI();
  }

  // Expose for runtime callers (status messages in app.js).
  window.t = t;
  window.setLang = setLang;
  window.applyI18n = applyTranslations;

  function init() {
    applyTranslations();
    wireSwitch();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
