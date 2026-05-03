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
      "brand.name": "Lux",
      "header.tagline": "Administrativ Q&A-bot för KTH CTFYS · baserad på officiella styrdokument och FAQ",

      "notice.title": "Experimentell testtjänst.",
      "notice.body": " Servern har begränsade resurser och språkmodellen är liten — svar kan vara långsamma och inte alltid korrekta. Första frågan kan ta extra lång tid medan modellen laddas. Använd gärna 👍 eller 👎 på svaren — feedback hjälper oss att förbättra tjänsten.",
      "notice.close.aria": "Stäng",

      "onboarding.title": "Hej!",
      "onboarding.intro": "Den här boten svarar på administrativa frågor om civilingenjörsprogrammet i Teknisk fysik, baserat på KTH:s officiella regler och utbildningsplaner. Den använder en lokal språkmodell + retrieval — alla källor kan klickas på under varje svar.",
      "onboarding.caveat": "LLM:er kan ha fel även när de låter säkra. Klicka alltid på källorna och dubbelkolla viktiga detaljer.",
      "onboarding.name.label": "Ditt namn (visas inte för andra; används för loggar):",
      "onboarding.name.placeholder": "Anonym",
      "onboarding.optout": "Logga inte mina frågor (du kan ändra detta när som helst)",
      "onboarding.start": "Starta",

      "chat.placeholder": "Skriv din fråga på svenska eller engelska — Enter skickar, Shift+Enter ny rad…",
      "chat.reset": "Ny tråd",
      "chat.send": "Fråga",
      "chat.thinking": "tänker…",
      "chat.newthread": "ny tråd",
      "answer.sources": "Källor",

      "footer.about": "Om boten",
      "footer.glossary": "Ordlista",
      "footer.stats": "Statistik",

      "about.title": "Om boten",
      "about.h2.what": "Vad är det här?",
      "about.what.body": "En lokal RAG-bot som svarar på administrativa frågor om CTFYS-programmet, grundad på de officiella dokumenten under <code>docs/corpus</code>.",
      "about.h2.tips": "Fem saker att tänka på när du använder boten",
      "about.tip1": "<strong>Verifiera källan.</strong> Klicka på källänkarna under varje svar och dubbelkolla mot dokumenten — boten kan ha fel även när den låter säker.",
      "about.tip2": "<strong>Flytande språk är inte korrekthet.</strong> Att en LLM låter övertygande betyder inte att den har rätt. Lita på källorna, inte på tonen. Konfidensbadgen vid varje svar visar hur säker retrieval-steget är.",
      "about.tip3": "<strong>Boten har gränser.</strong> Den känner bara till de indexerade dokumenten — inte ditt enskilda fall, inte aktuella personer eller datum utanför dokumenten. För personliga ärenden, kontakta studievägledaren.",
      "about.tip4": "<strong>Du blir loggad — men du kan stänga av det.</strong> Frågor och svar lagras anonymt (med saltad SHA-256 av ditt session-id) för att förbättra boten. Bocka i <em>\"Logga inte mina frågor\"</em> i onboarding-skärmen, eller kör <code>!privacy off</code> i Mattermost.",
      "about.tip5": "<strong>Komplement, inte ersättare.</strong> Boten kan ge snabba svar på välkända frågor; viktiga beslut om dina studier ska du diskutera med en människa.",
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
      "glossary.status.ok": "tack — förslaget köades för granskning",
      "glossary.status.error": "fel",
      "glossary.back": "← Tillbaka",

      "stats.title": "Statistik",
      "stats.summary": "Loggade frågor: {logged} · Besvarade: {answered} · Genomsnittlig latens: {latency} ms · Anonym räknare (opt-out): {anon}",
      "stats.th.topic": "ämne",
      "stats.th.n": "n",
      "stats.th.answered": "besvarade",
      "stats.th.avgms": "snitt ms",
      "stats.empty": "ingen data än",
      "stats.back": "← Tillbaka",
    },
    en: {
      "brand.name": "Lux",
      "header.tagline": "Administrative Q&A bot for KTH CTFYS · grounded in a corpus of official steering documents and FAQs",

      "notice.title": "Experimental test service.",
      "notice.body": " The server has limited resources and the language model is small — responses may be slow and not always correct. The first question can take noticeably longer while the model loads. Please use 👍 or 👎 on the replies — feedback helps us improve the service.",
      "notice.close.aria": "Close",

      "onboarding.title": "Hi!",
      "onboarding.intro": "This bot answers administrative questions about the Engineering Physics MSc program, grounded in KTH's official rules and curricula. It uses a local language model + retrieval — every source under each answer is clickable.",
      "onboarding.caveat": "LLMs can be wrong even when they sound confident. Always click the sources and double-check important details.",
      "onboarding.name.label": "Your name (not shown to others; used for logs):",
      "onboarding.name.placeholder": "Anonymous",
      "onboarding.optout": "Don't log my questions (you can change this at any time)",
      "onboarding.start": "Start",

      "chat.placeholder": "Type your question in Swedish or English — Enter sends, Shift+Enter for newline…",
      "chat.reset": "New thread",
      "chat.send": "Ask",
      "chat.thinking": "thinking…",
      "chat.newthread": "new thread",
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
      "about.tip4": "<strong>You're logged — but you can turn it off.</strong> Questions and answers are stored anonymously (with a salted SHA-256 of your session id) to help improve the bot. Tick <em>\"Don't log my questions\"</em> on the onboarding screen, or run <code>!privacy off</code> in Mattermost.",
      "about.tip5": "<strong>Complement, not replacement.</strong> The bot can answer well-known questions quickly; important decisions about your studies should be discussed with a human.",
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
      "stats.th.topic": "topic",
      "stats.th.n": "n",
      "stats.th.answered": "answered",
      "stats.th.avgms": "avg ms",
      "stats.empty": "no data yet",
      "stats.back": "← Back",
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
