/* Stats page: three Chart.js charts driven by /stats/series.
 * Range buttons (72h / 14d / 90d) and a log-y toggle re-fetch / re-render.
 * Labels re-translate when the user switches language. */
(function () {
  if (typeof Chart === "undefined") return;

  // Paint a white background under everything Chart.js draws so the
  // exported PNG captures axis labels and ticks against a solid backdrop
  // (the live page already shows white via the .card, so this is invisible
  // in the UI). Letting Chart.js handle the fill — instead of compositing
  // the canvas afterwards — avoids HiDPI / responsive-sizing edge cases
  // where the post-hoc copy clipped tick labels.
  // Match the page font and size up the chart text so axis labels and
  // legend entries are easy to read in screenshots and live. The vendored
  // Figtree @font-face in style.css is what the page uses; falling back to
  // Arial keeps charts legible if the woff2 fails to load.
  Chart.defaults.font.family = '"Figtree", Arial, sans-serif';
  Chart.defaults.font.size = 14;
  Chart.defaults.plugins.legend.labels.font = { size: 14 };
  Chart.defaults.plugins.tooltip.bodyFont = { size: 14 };
  Chart.defaults.plugins.tooltip.titleFont = { size: 14 };

  Chart.register({
    id: "white-bg",
    beforeDraw: (chart) => {
      const { ctx } = chart;
      ctx.save();
      ctx.globalCompositeOperation = "destination-over";
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, chart.width, chart.height);
      ctx.restore();
    },
  });

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

  // hex (#RRGGBB) → rgba(r,g,b,a). Used for fills so two overlapping
  // filled curves stay visible — opaque fills hide the smaller series.
  function withAlpha(hex, alpha) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const v = parseInt(m[1], 16);
    return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${alpha})`;
  }

  // Channel filter is owned by the server (URL query param). The page
  // exposes it via [data-channel] on the stats card so the chart endpoint
  // sees the same view as the server-rendered tables.
  function activeChannel() {
    const card = document.querySelector(".stats-card[data-channel]");
    const v = card && card.dataset.channel;
    return v === "web" || v === "mm" ? v : "all";
  }

  const seriesUrl = (range) => {
    const base = location.pathname.replace(/\/$/, "");
    const ch = activeChannel();
    const params = new URLSearchParams({ range });
    if (ch !== "all") params.set("ch", ch);
    return `${base}/series?${params.toString()}`;
  };

  const RANGES = ["24h", "72h", "14d", "90d"];
  function activeRange() {
    const v = localStorage.getItem(STORE_RANGE);
    return RANGES.includes(v) ? v : "72h";
  }
  function logYEnabled() {
    return localStorage.getItem(STORE_LOGY) === "1";
  }

  // Time format for the x-axis tick labels — bucket size implied by the range.
  // Buckets are dense (gap-filled by the server) so even spacing on the
  // category axis already represents real time.
  function tickFormatter(range) {
    const fmtTime = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    const fmtDayHour = new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "short", hour: "2-digit", hourCycle: "h23" });
    const fmtDay = new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "short" });
    return (value) => {
      const d = new Date(value);
      if (range === "24h") return fmtTime.format(d);
      if (range === "72h") return fmtDayHour.format(d);
      return fmtDay.format(d);
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
            maxTicksLimit: 12,
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

  // Render legend swatches as a thin colored bar (matching the line marks
  // on the chart) instead of the default filled rectangle. Chart.js's
  // built-in pointStyle:"line" renders too thinly to read as a line, so we
  // shrink the rectangle's height to ~chart line thickness instead.
  function lineLegendOpts(opts) {
    opts.plugins = opts.plugins || {};
    opts.plugins.legend = opts.plugins.legend || {};
    opts.plugins.legend.labels = {
      ...(opts.plugins.legend.labels || {}),
      boxWidth: 28,
      boxHeight: 4,
    };
    return opts;
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
    return lineLegendOpts(base);
  }

  function feedbackOpts(range) {
    const base = commonOpts(range, false);
    base.scales.y.beginAtZero = true;
    base.scales.y.suggestedMax = 1;
    base.scales.y.ticks = {
      callback: (v) => `${Math.round(v * 100)}%`,
    };
    return lineLegendOpts(base);
  }

  function buildLabels(buckets) {
    return buckets.map((b) => new Date(b.bucket_ts * 1000));
  }

  function makeRequestsData(buckets) {
    const labels = buildLabels(buckets);
    const FILL_ALPHA = 0.35;
    return {
      labels,
      datasets: [
        {
          label: T("stats.chart.requests.answered"),
          data: buckets.map((b) => b.n_answered),
          borderColor: CHART_COLORS.answered,
          backgroundColor: withAlpha(CHART_COLORS.answered, FILL_ALPHA),
          tension: 0.25,
          stack: "req",
          fill: true,
        },
        {
          label: T("stats.chart.requests.refused"),
          data: buckets.map((b) => Math.max(0, b.n - b.n_answered)),
          borderColor: CHART_COLORS.refused,
          backgroundColor: withAlpha(CHART_COLORS.refused, FILL_ALPHA),
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

  async function exportChartPng(chart, filename) {
    if (!chart) return;
    // Wait until the page's web fonts are ready before snapshotting, so
    // the export doesn't capture an Arial fallback on a cold render.
    if (document.fonts && document.fonts.ready) {
      try { await document.fonts.ready; } catch (_) { /* non-fatal */ }
    }
    // Chart.js's own serializer captures axes, tick labels, legend, and
    // datasets at whatever resolution the canvas backing store is currently
    // at. To get a high-res export (suitable for slides / posters) we
    // temporarily crank the chart's devicePixelRatio so it re-rasters at
    // ~3× display size, grab the PNG, then restore. With animation: false
    // the resize is a single frame — invisible in practice.
    const HIRES_DPR = 3;
    const oldDpr = chart.options.devicePixelRatio;
    chart.options.devicePixelRatio = HIRES_DPR;
    let url;
    try {
      chart.resize();
      url = chart.toBase64Image("image/png", 1);
    } finally {
      chart.options.devicePixelRatio = oldDpr;
      chart.resize();
    }
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function exportFilename(chartKey) {
    const date = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    return `student-bot-${chartKey}-${currentRange}-${activeChannel()}-${date}.png`;
  }

  function wireExportButtons() {
    document.querySelectorAll("[data-export-chart]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.exportChart;
        if (!charts[key]) return;
        exportChartPng(charts[key], exportFilename(key));
      });
    });
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
    wireExportButtons();
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
