/* Stats page: three Chart.js charts driven by /stats/series.
 * Range buttons (72h / 14d / 90d) and a log-y toggle re-fetch / re-render.
 * Labels re-translate when the user switches language. */
(function () {
  if (typeof Chart === "undefined") return;

  const STORE_RANGE = "stats.range";
  const STORE_LOGY = "stats.logy";

  const CHART_COLORS = {
    total: "#004791",      // kth-blue
    answered: "#0D4A21",   // kth-darkgreen
    refused: "#78001A",    // kth-darkbrick
    prompt: "#6298D2",     // kth-skyblue
    gen: "#A65900",        // kth-darkyellow
    pos_total: "#0D4A21",  // kth-darkgreen
    neg_total: "#78001A",  // kth-darkbrick
    ratio: "#000061",      // kth-navy
  };

  const seriesUrl = (range) => {
    const base = location.pathname.replace(/\/$/, "");
    return `${base}/series?range=${encodeURIComponent(range)}`;
  };

  function activeRange() {
    const v = localStorage.getItem(STORE_RANGE);
    return v === "14d" || v === "90d" ? v : "72h";
  }
  function logYEnabled() {
    return localStorage.getItem(STORE_LOGY) === "1";
  }

  // Time format for the x-axis tick labels — bucket size implied by the range.
  function tickFormatter(range) {
    const fmt72 = new Intl.DateTimeFormat(undefined, { hour: "2-digit", day: "2-digit", month: "short" });
    const fmtDay = new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "short" });
    return (value) => {
      const d = new Date(value);
      return range === "72h" ? fmt72.format(d) : fmtDay.format(d);
    };
  }

  const T = (k) => (window.t ? window.t(k) : k);

  let charts = { requests: null, tokens: null, feedback: null };
  let lastData = null;
  let currentRange = activeRange();

  function commonOpts(range, logY) {
    return {
      responsive: true,
      maintainAspectRatio: true,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          ticks: {
            autoSkip: true,
            maxRotation: 0,
            callback: function (val) {
              return tickFormatter(range)(this.getLabelForValue(val));
            },
          },
          grid: { display: false },
        },
        y: {
          beginAtZero: !logY,
          type: logY ? "logarithmic" : "linear",
          ticks: { precision: 0 },
        },
      },
      plugins: {
        legend: { position: "bottom" },
        tooltip: { mode: "index", intersect: false },
      },
    };
  }

  function tokensOpts(range, logY) {
    const base = commonOpts(range, logY);
    base.scales.y.position = "left";
    base.scales.y1 = {
      position: "right",
      beginAtZero: !logY,
      type: logY ? "logarithmic" : "linear",
      grid: { drawOnChartArea: false },
      ticks: { precision: 0 },
    };
    return base;
  }

  function feedbackOpts(range) {
    const base = commonOpts(range, false);
    base.scales.y.beginAtZero = true;
    base.scales.y.suggestedMax = 1;
    base.scales.y.ticks = {
      callback: (v) => `${Math.round(v * 100)}%`,
    };
    return base;
  }

  function buildLabels(buckets) {
    return buckets.map((b) => new Date(b.bucket_ts * 1000));
  }

  function makeRequestsData(buckets) {
    const labels = buildLabels(buckets);
    return {
      labels,
      datasets: [
        {
          label: T("stats.chart.requests.answered"),
          data: buckets.map((b) => b.n_answered),
          borderColor: CHART_COLORS.answered,
          backgroundColor: CHART_COLORS.answered,
          tension: 0.25,
          stack: "req",
          fill: true,
        },
        {
          label: T("stats.chart.requests.refused"),
          data: buckets.map((b) => Math.max(0, b.n - b.n_answered)),
          borderColor: CHART_COLORS.refused,
          backgroundColor: CHART_COLORS.refused,
          tension: 0.25,
          stack: "req",
          fill: true,
        },
      ],
    };
  }

  function makeTokensData(buckets) {
    const labels = buildLabels(buckets);
    return {
      labels,
      datasets: [
        {
          label: T("stats.chart.tokens.prompt"),
          data: buckets.map((b) => b.prompt_tokens),
          borderColor: CHART_COLORS.prompt,
          backgroundColor: CHART_COLORS.prompt,
          tension: 0.25,
          yAxisID: "y",
        },
        {
          label: T("stats.chart.tokens.gen"),
          data: buckets.map((b) => b.gen_tokens),
          borderColor: CHART_COLORS.gen,
          backgroundColor: CHART_COLORS.gen,
          tension: 0.25,
          yAxisID: "y1",
        },
      ],
    };
  }

  function makeFeedbackData(buckets) {
    const labels = buildLabels(buckets);
    // Three views of feedback per bucket:
    //   pos/total — share of answered questions that got a 👍
    //   neg/total — share of answered questions that got a 👎
    //   pos/(pos+neg) — quality signal among reacted answers only
    // "total" = n_answered (bot actually produced an answer); refusals are
    // not really reactable, so the denominator excludes them.
    const posTotal = buckets.map((b) => (b.n_answered > 0 ? b.thumbs_up / b.n_answered : null));
    const negTotal = buckets.map((b) => (b.n_answered > 0 ? b.thumbs_down / b.n_answered : null));
    const ratio = buckets.map((b) => {
      const tot = b.thumbs_up + b.thumbs_down;
      return tot > 0 ? b.thumbs_up / tot : null;
    });
    const ds = (key, color, data) => ({
      label: T(key),
      data,
      spanGaps: true,
      borderColor: color,
      backgroundColor: color,
      tension: 0.25,
      pointRadius: (ctx) => (ctx.raw == null ? 0 : 3),
    });
    return {
      labels,
      datasets: [
        ds("stats.chart.feedback.pos_total", CHART_COLORS.pos_total, posTotal),
        ds("stats.chart.feedback.neg_total", CHART_COLORS.neg_total, negTotal),
        ds("stats.chart.feedback.ratio", CHART_COLORS.ratio, ratio),
      ],
    };
  }

  function destroyCharts() {
    Object.values(charts).forEach((c) => { if (c) c.destroy(); });
    charts = { requests: null, tokens: null, feedback: null };
  }

  function render(buckets, range) {
    destroyCharts();
    const logY = logYEnabled();
    const reqEl = document.getElementById("chart-requests");
    const tokEl = document.getElementById("chart-tokens");
    const fbEl = document.getElementById("chart-feedback");
    if (reqEl) charts.requests = new Chart(reqEl, { type: "line", data: makeRequestsData(buckets), options: commonOpts(range, logY) });
    if (tokEl) charts.tokens   = new Chart(tokEl, { type: "line", data: makeTokensData(buckets),   options: tokensOpts(range, logY) });
    if (fbEl)  charts.feedback = new Chart(fbEl,  { type: "line", data: makeFeedbackData(buckets), options: feedbackOpts(range) });
  }

  async function fetchAndRender(range) {
    currentRange = range;
    try {
      const resp = await fetch(seriesUrl(range), { credentials: "same-origin" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      lastData = data;
      render(data.buckets || [], range);
    } catch (err) {
      console.error("stats series fetch failed:", err);
    }
  }

  function wireRangeButtons() {
    const buttons = document.querySelectorAll(".stats-ranges button[data-range]");
    function refresh() {
      buttons.forEach((b) => b.classList.toggle("active", b.dataset.range === currentRange));
    }
    buttons.forEach((b) => {
      b.addEventListener("click", () => {
        const r = b.dataset.range;
        if (!r || r === currentRange) return;
        localStorage.setItem(STORE_RANGE, r);
        currentRange = r;
        refresh();
        fetchAndRender(r);
      });
    });
    refresh();
  }

  function wireLogyToggle() {
    const cb = document.getElementById("stats-logy");
    if (!cb) return;
    cb.checked = logYEnabled();
    cb.addEventListener("change", () => {
      localStorage.setItem(STORE_LOGY, cb.checked ? "1" : "0");
      if (lastData) render(lastData.buckets || [], currentRange);
    });
  }

  function init() {
    wireRangeButtons();
    wireLogyToggle();
    fetchAndRender(currentRange);
    document.addEventListener("i18n:langchange", () => {
      if (lastData) render(lastData.buckets || [], currentRange);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
