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
  const STORE_SPLIT_MODEL = "stats.split_model";
  const STORE_LOGX_PREFIX = "stats.logx.";
  const HIST_BINS = 50;
  // Histogram keys map 1:1 to canvas ids `chart-<key>` (with underscores
  // turned into dashes for the DOM). Used by the export buttons and the
  // per-chart log-x checkboxes.
  const HIST_KEYS = ["tokens_hist", "ttft_hist", "tps_hist"];

  const CHART_COLORS = {
    total: "#004791",      // kth-blue
    answered: "#0D4A21",   // kth-darkgreen
    refused: "#78001A",    // kth-darkbrick
    prompt: "#6298D2",     // kth-skyblue
    gen: "#A65900",        // kth-darkyellow
    pos_total: "#0D4A21",  // kth-darkgreen
    neg_total: "#78001A",  // kth-darkbrick
    ratio: "#000061",      // kth-navy
    ttft: "#004791",       // kth-blue
    tps: "#0D4A21",        // kth-darkgreen
  };

  // Cycled when "split by model" is on — issue #58 caps at 5 models, so 5
  // distinct colors. Order chosen for adequate contrast against each other.
  const MODEL_PALETTE = ["#004791", "#A65900", "#0D4A21", "#78001A", "#000061"];

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
  function logXEnabled(key) {
    return localStorage.getItem(STORE_LOGX_PREFIX + key) === "1";
  }
  function splitByModelEnabled() {
    return localStorage.getItem(STORE_SPLIT_MODEL) === "1";
  }

  // Build 50 equal-width bins (linear or log10) spanning [min, max] of the
  // supplied values. Returns edges + centers (in display units) and a flag
  // so binCounts knows whether to log-transform incoming values. Empty input
  // → empty bins so callers can short-circuit rendering.
  function makeBins(values, n, logX) {
    if (!values.length) return { edges: [], centers: [], log: !!logX };
    if (logX) {
      const positive = values.filter((v) => v > 0);
      if (!positive.length) return { edges: [], centers: [], log: true };
      let lo = Math.min(...positive);
      let hi = Math.max(...positive);
      if (hi <= lo) hi = lo * 10;
      const loL = Math.log10(lo);
      const hiL = Math.log10(hi);
      const step = (hiL - loL) / n;
      const edges = new Array(n + 1);
      for (let i = 0; i <= n; i++) edges[i] = Math.pow(10, loL + i * step);
      const centers = new Array(n);
      for (let i = 0; i < n; i++) centers[i] = Math.pow(10, loL + (i + 0.5) * step);
      return { edges, centers, log: true, loL, step };
    }
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (hi <= lo) hi = lo + 1;
    const step = (hi - lo) / n;
    const edges = new Array(n + 1);
    for (let i = 0; i <= n; i++) edges[i] = lo + i * step;
    const centers = new Array(n);
    for (let i = 0; i < n; i++) centers[i] = lo + (i + 0.5) * step;
    return { edges, centers, log: false, lo, step };
  }

  function binCounts(values, bins) {
    const n = bins.centers.length;
    const counts = new Array(n).fill(0);
    if (!n) return counts;
    if (bins.log) {
      const loL = bins.loL;
      const step = bins.step;
      if (step <= 0) return counts;
      for (const v of values) {
        if (v <= 0) continue;
        let idx = Math.floor((Math.log10(v) - loL) / step);
        if (idx < 0 || idx > n) continue;
        if (idx === n) idx = n - 1;
        counts[idx] += 1;
      }
    } else {
      const lo = bins.lo;
      const step = bins.step;
      if (step <= 0) return counts;
      for (const v of values) {
        if (!Number.isFinite(v)) continue;
        let idx = Math.floor((v - lo) / step);
        if (idx < 0 || idx > n) continue;
        if (idx === n) idx = n - 1;
        counts[idx] += 1;
      }
    }
    return counts;
  }

  // Sorted, deduped list of llm_model strings present in the row data.
  // Falls back to ["(unknown)"] when every row has NULL (shouldn't happen
  // post-#58 backfill, but keeps the per-model UI from collapsing if it
  // does). Capped at 5 entries — issue #58 explicitly bounds the legend.
  function modelsIn(rows) {
    const set = new Set();
    for (const r of rows) if (r && r.llm_model) set.add(r.llm_model);
    const list = [...set].sort();
    if (!list.length) return ["(unknown)"];
    return list.slice(0, 5);
  }

  function rowsForModel(rows, model) {
    if (!model) return rows;
    return rows.filter((r) => (r.llm_model || "(unknown)") === model);
  }

  function modelColor(model, models) {
    const idx = Math.max(0, models.indexOf(model));
    return MODEL_PALETTE[idx % MODEL_PALETTE.length];
  }

  // Compact axis tick label: 1500 → "1.5k", 1_200_000 → "1.2M". Used for
  // histogram x-axis bin labels.
  function compactNumber(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return `${(v / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
    if (a >= 1e3) return `${(v / 1e3).toFixed(a >= 1e4 ? 0 : 1)}k`;
    if (a >= 10) return `${v.toFixed(0)}`;
    if (a >= 1) return `${v.toFixed(1)}`;
    return `${v.toPrecision(2)}`;
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

  let charts = {
    requests: null,
    tokens: null,
    tokens_hist: null,
    ttft_hist: null,
    tps_hist: null,
    feedback: null,
  };
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

  // Histograms use a category x-axis (bin-center labels formatted via
  // compactNumber). The y-axis honors the shared log-y toggle. Log-x is
  // expressed by re-binning in log space, not by a logarithmic x-scale,
  // since Chart.js logarithmic scales don't compose well with category data.
  function histOpts(logY, xLabel) {
    return lineLegendOpts({
      responsive: true,
      maintainAspectRatio: true,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          title: { display: true, text: xLabel, color: "#6b6b6b" },
          ticks: { autoSkip: true, maxRotation: 0, maxTicksLimit: 12 },
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
    });
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

  // Shared histogram body: bin a value array into 50 bins and turn it into a
  // line+fill dataset compatible with Chart.js. Caller supplies the bins so
  // multiple series (prompt/gen, or per-model) share a single x-axis.
  function histDataset(label, values, bins, color, fillAlpha) {
    return {
      label,
      data: binCounts(values, bins),
      borderColor: color,
      backgroundColor: withAlpha(color, fillAlpha != null ? fillAlpha : 0.35),
      tension: 0.25,
      fill: true,
      pointRadius: 0,
    };
  }

  function histLabels(bins) {
    return bins.centers.map((v) => compactNumber(v));
  }

  // Tokens histogram. Default mode (split off): prompt + gen overlaid on
  // shared bins spanning the union of both ranges. Split-by-model: total
  // tokens (prompt + gen) per model. Bounded at 5 model series.
  function makeTokensHistData(rows, splitModel, logX) {
    const FILL = 0.35;
    if (splitModel) {
      const models = modelsIn(rows);
      const totalsAll = [];
      const totalsByModel = new Map();
      for (const m of models) totalsByModel.set(m, []);
      for (const r of rows) {
        const p = r.prompt_tokens || 0;
        const g = r.gen_tokens || 0;
        const total = p + g;
        if (!total) continue;
        const m = r.llm_model || "(unknown)";
        if (!totalsByModel.has(m)) continue;
        totalsByModel.get(m).push(total);
        totalsAll.push(total);
      }
      const bins = makeBins(totalsAll, HIST_BINS, logX);
      return {
        labels: histLabels(bins),
        datasets: models.map((m) =>
          histDataset(m, totalsByModel.get(m) || [], bins, modelColor(m, models), FILL),
        ),
      };
    }
    const prompts = rows.map((r) => r.prompt_tokens).filter((v) => Number.isFinite(v));
    const gens = rows.map((r) => r.gen_tokens).filter((v) => Number.isFinite(v));
    const combined = prompts.concat(gens);
    const bins = makeBins(combined, HIST_BINS, logX);
    return {
      labels: histLabels(bins),
      datasets: [
        histDataset(T("stats.chart.tokens.prompt"), prompts, bins, CHART_COLORS.prompt, FILL),
        histDataset(T("stats.chart.tokens.gen"), gens, bins, CHART_COLORS.gen, FILL),
      ],
    };
  }

  // Single-metric histogram. Used by TTFT and TPS. `extract` pulls the
  // metric value off a row; null → row excluded. `defaultColor` is used
  // when not splitting; per-model uses MODEL_PALETTE.
  function makeMetricHistData(rows, splitModel, logX, extract, defaultLabel, defaultColor) {
    const FILL = 0.35;
    if (splitModel) {
      const models = modelsIn(rows);
      const valuesAll = [];
      const valuesByModel = new Map();
      for (const m of models) valuesByModel.set(m, []);
      for (const r of rows) {
        const v = extract(r);
        if (!Number.isFinite(v)) continue;
        const m = r.llm_model || "(unknown)";
        if (!valuesByModel.has(m)) continue;
        valuesByModel.get(m).push(v);
        valuesAll.push(v);
      }
      const bins = makeBins(valuesAll, HIST_BINS, logX);
      return {
        labels: histLabels(bins),
        datasets: models.map((m) =>
          histDataset(m, valuesByModel.get(m) || [], bins, modelColor(m, models), FILL),
        ),
      };
    }
    const values = rows.map(extract).filter((v) => Number.isFinite(v));
    const bins = makeBins(values, HIST_BINS, logX);
    return {
      labels: histLabels(bins),
      datasets: [histDataset(defaultLabel, values, bins, defaultColor, FILL)],
    };
  }

  function makeTtftHistData(rows, splitModel, logX) {
    return makeMetricHistData(
      rows,
      splitModel,
      logX,
      (r) => r.ttft_ms,
      T("stats.chart.ttft_hist.label"),
      CHART_COLORS.ttft,
    );
  }

  function makeTpsHistData(rows, splitModel, logX) {
    return makeMetricHistData(
      rows,
      splitModel,
      logX,
      (r) => r.gen_tps,
      T("stats.chart.tps_hist.label"),
      CHART_COLORS.tps,
    );
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
    charts = {
      requests: null,
      tokens: null,
      tokens_hist: null,
      ttft_hist: null,
      tps_hist: null,
      feedback: null,
    };
  }

  function render(buckets, range, rows) {
    destroyCharts();
    const logY = logYEnabled();
    const splitModel = splitByModelEnabled();
    const reqEl = document.getElementById("chart-requests");
    const tokEl = document.getElementById("chart-tokens");
    const tokHistEl = document.getElementById("chart-tokens-hist");
    const ttftHistEl = document.getElementById("chart-ttft-hist");
    const tpsHistEl = document.getElementById("chart-tps-hist");
    const fbEl = document.getElementById("chart-feedback");
    if (reqEl) charts.requests = new Chart(reqEl, { type: "line", data: makeRequestsData(buckets), options: commonOpts(range, logY) });
    if (tokEl) charts.tokens   = new Chart(tokEl, { type: "line", data: makeTokensData(buckets),   options: tokensOpts(range, logY) });
    if (tokHistEl) charts.tokens_hist = new Chart(tokHistEl, {
      type: "line",
      data: makeTokensHistData(rows, splitModel, logXEnabled("tokens_hist")),
      options: histOpts(logY, T("stats.chart.tokens_hist.xlabel")),
    });
    if (ttftHistEl) charts.ttft_hist = new Chart(ttftHistEl, {
      type: "line",
      data: makeTtftHistData(rows, splitModel, logXEnabled("ttft_hist")),
      options: histOpts(logY, T("stats.chart.ttft_hist.xlabel")),
    });
    if (tpsHistEl) charts.tps_hist = new Chart(tpsHistEl, {
      type: "line",
      data: makeTpsHistData(rows, splitModel, logXEnabled("tps_hist")),
      options: histOpts(logY, T("stats.chart.tps_hist.xlabel")),
    });
    if (fbEl)  charts.feedback = new Chart(fbEl,  { type: "line", data: makeFeedbackData(buckets), options: feedbackOpts(range) });
  }

  async function fetchAndRender(range) {
    currentRange = range;
    try {
      const resp = await fetch(seriesUrl(range), { credentials: "same-origin" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      lastData = data;
      render(data.buckets || [], range, data.rows || []);
    } catch (err) {
      console.error("stats series fetch failed:", err);
    }
  }

  function rerender() {
    if (!lastData) return;
    render(lastData.buckets || [], currentRange, lastData.rows || []);
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
      rerender();
    });
  }

  function wireSplitModelToggle() {
    const cb = document.getElementById("stats-split-model");
    if (!cb) return;
    cb.checked = splitByModelEnabled();
    cb.addEventListener("change", () => {
      localStorage.setItem(STORE_SPLIT_MODEL, cb.checked ? "1" : "0");
      rerender();
    });
  }

  function wireLogxToggles() {
    document.querySelectorAll("input[data-logx-for]").forEach((cb) => {
      const key = cb.dataset.logxFor;
      if (!HIST_KEYS.includes(key)) return;
      cb.checked = logXEnabled(key);
      cb.addEventListener("change", () => {
        localStorage.setItem(STORE_LOGX_PREFIX + key, cb.checked ? "1" : "0");
        rerender();
      });
    });
  }

  function init() {
    wireRangeButtons();
    wireLogyToggle();
    wireSplitModelToggle();
    wireLogxToggles();
    wireExportButtons();
    fetchAndRender(currentRange);
    document.addEventListener("i18n:langchange", rerender);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
