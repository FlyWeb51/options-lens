/* Options Lens front end. Plain JS, no build step. */

const $ = (id) => document.getElementById(id);
const charts = {};
let polling = null;
let current = null;      // last analysis result
let fundData = null;     // last fundamentals payload
let chainCount = 0;

const AXIS = "#8b98ad";
const GRID = "rgba(38,48,65,.5)";

const fmt = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toFixed(d);
const money = (n) => (n === null || n === undefined ? "—" : "$" + Number(n).toFixed(2));
const pct = (n, d = 1) => (n === null || n === undefined ? "—" : Number(n).toFixed(d) + "%");
const compact = (n) => {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  const s = n < 0 ? "-" : "";
  if (a >= 1e12) return s + (a / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return s + (a / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return s + (a / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return s + (a / 1e3).toFixed(1) + "K";
  return s + String(Math.round(a));
};
const usd = (n) => (n === null || n === undefined ? "—" : "$" + compact(n));
const esc = (s) =>
  String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

function chart(id, config) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), config);
  return charts[id];
}

function baseScales(extra = {}) {
  return {
    x: { ticks: { color: AXIS, maxTicksLimit: 12 }, grid: { color: GRID } },
    y: { ticks: { color: AXIS }, grid: { color: GRID } },
    ...extra,
  };
}

/* ------------------------------------------------------------------ */
/* Tabs                                                                */
/* ------------------------------------------------------------------ */

$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  showTab(btn.dataset.tab);
});

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach((p) =>
    p.classList.toggle("hidden", p.id !== "pane-" + name));

  if (name === "sources") loadSources();
  if (name === "kalshi" && !$("kalshi-body").dataset.loaded) loadKalshi();
  if (name === "fundamentals" && current && !fundData) loadFundamentals();
  if (name === "people" && current && !$("people-content").dataset.loaded) loadPeople();
  if (name === "politics" && current && !$("politics-content").dataset.loaded) loadPolitics();
}

/* ------------------------------------------------------------------ */
/* Analysis request flow                                               */
/* ------------------------------------------------------------------ */

$("search").addEventListener("submit", (e) => { e.preventDefault(); run(false); });
$("refresh").addEventListener("click", () => run(true));

async function run(forceRefresh) {
  const ticker = $("ticker").value.trim().toUpperCase();
  if (!ticker) return;
  const speed = $("speed").value;

  clearInterval(polling);
  fundData = null;
  ["people-content", "politics-content", "kalshi-body"].forEach((id) => {
    const el = $(id); if (el) delete el.dataset.loaded;
  });

  $("intro").classList.add("hidden");
  $("results").classList.add("hidden");
  $("error").classList.add("hidden");
  $("progress").classList.remove("hidden");
  $("go").disabled = true;
  setProgress(3, "Contacting the data provider…");
  $("progress-title").textContent = `Analysing ${ticker}`;
  $("progress-hint").textContent = "";
  showTab("options");

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, refresh: forceRefresh, speed }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed.");
    if (data.status === "done" && data.result) {
      finish(data.result, data.cache_age_seconds);
      return;
    }
    pollJob(data.job_id);
  } catch (err) {
    showError(err.message);
  }
}

function pollJob(jobId) {
  let ticks = 0;
  polling = setInterval(async () => {
    ticks++;
    try {
      const res = await fetch(`/api/job/${jobId}`);
      const job = await res.json();
      if (!res.ok) throw new Error(job.detail || "Lost track of the job.");
      setProgress(job.progress, job.message);
      if (ticks > 6 && job.progress < 70) {
        $("progress-hint").textContent =
          "The free data tier allows 5 requests per minute, so the first look at " +
          "a ticker takes a while. It is cached afterwards and instant for everyone. " +
          "Quick mode fetches fewer contracts if you want speed over depth.";
      }
      if (job.status === "done") { clearInterval(polling); finish(job.result, 0); }
      else if (job.status === "error") { clearInterval(polling); showError(job.error); }
    } catch (err) {
      clearInterval(polling);
      showError(err.message);
    }
  }, 1500);
}

function setProgress(p, msg) {
  $("bar-fill").style.width = Math.max(p, 2) + "%";
  $("progress-msg").textContent = msg || "";
}

function showError(message) {
  $("progress").classList.add("hidden");
  $("error").classList.remove("hidden");
  $("error").textContent = message || "Something went wrong.";
  $("go").disabled = false;
}

function finish(result, cacheAge) {
  current = result;
  $("progress").classList.add("hidden");
  $("go").disabled = false;
  renderOptions(result, cacheAge);
  renderVol(result);
  setupStrategy(result);
  $("results").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ------------------------------------------------------------------ */
/* Options tab                                                         */
/* ------------------------------------------------------------------ */

function renderOptions(r, cacheAge) {
  $("headline").textContent = `${r.ticker} · ${money(r.spot)}` +
    (r.company ? ` — ${r.company}` : "");
  const bits = [
    `${r.contract_count} contracts`,
    `${r.speed} mode`,
    `spot from ${String(r.spot_source).replace("_", " ")}`,
  ];
  if (cacheAge > 0) bits.push(`cached ${Math.round(cacheAge / 60)} min ago`);
  $("subline").textContent = bits.join(" · ");

  const badge = $("mode-badge");
  badge.textContent = r.mode === "snapshot" ? "Live chain snapshot" : "End-of-day mode";
  badge.className = "badge " + (r.mode === "snapshot" ? "live" : "eod");

  $("warnings").innerHTML = (r.warnings || [])
    .map((w) => `<div class="warning">${esc(w)}</div>`).join("");

  $("verdict-list").innerHTML = (r.verdict.lines || [])
    .map((l) => `<li>${esc(l)}</li>`).join("");
  $("verdict-caveat").textContent = r.verdict.caveat;

  renderMove(r);
  renderSqueeze(r);
  renderDistribution(r);
  renderLadder(r);
  renderGamma(r);
  renderFlow(r);
  renderSkew(r);
  renderChain(r);
}

function renderMove(r) {
  const m = r.expected_move, box = $("move-body");
  if (!m.available) {
    box.innerHTML = `<p class="muted">Not available: ${esc(m.reason || "insufficient data")}</p>`;
    return;
  }
  $("move-expiry").textContent = `${m.expiry} · ${m.dte} days out`;
  const lo = m.two_sigma_low, hi = m.two_sigma_high, span = hi - lo || 1;
  const pos = (v) => ((v - lo) / span) * 100;
  box.innerHTML = `
    <div class="bigstat">±${fmt(m.move_pct, 1)}%</div>
    <p class="muted">≈ ±${money(m.move_dollars)} · at-the-money IV ${
      m.atm_iv ? fmt(m.atm_iv * 100, 1) + "%" : "—"}</p>
    <div class="range-viz">
      <div class="range-track"></div>
      <div class="range-fill" style="left:${pos(m.low)}%; width:${pos(m.high) - pos(m.low)}%"></div>
      <div class="range-mark" style="left:${pos(r.spot)}%"></div>
      <div class="range-label" style="left:${pos(m.low)}%">${money(m.low)}</div>
      <div class="range-label" style="left:${pos(r.spot)}%; color:var(--text)">now ${money(r.spot)}</div>
      <div class="range-label" style="left:${pos(m.high)}%">${money(m.high)}</div>
    </div>
    <div class="statgrid">
      <div class="stat"><div class="k">68% range</div><div class="v">${money(m.low)} – ${money(m.high)}</div></div>
      <div class="stat"><div class="k">95% range</div><div class="v">${money(m.two_sigma_low)} – ${money(m.two_sigma_high)}</div></div>
    </div>`;
}

function renderSqueeze(r) {
  const s = r.squeeze, cls = "score-" + s.label.toLowerCase();
  const comps = s.components.map((c) => {
    const val = c.value === null || c.value === undefined
      ? "no data" : `${c.value} ${c.unit || ""}`;
    return `<div class="component">
      <div class="component-head"><span>${esc(c.name)}</span>
        <span class="muted">${fmt(c.points, 1)} / ${c.max}</span></div>
      <div class="component-bar"><div class="component-fill" style="width:${(c.points / c.max) * 100}%"></div></div>
      <div class="component-note"><strong>${esc(val)}</strong> — ${esc(c.note)}</div>
    </div>`;
  }).join("");
  $("squeeze-body").innerHTML = `
    <div class="score-row">
      <div class="score-num ${cls}">${fmt(s.score, 0)}</div>
      <div><div class="score-label ${cls}">${esc(s.label)}</div>
      <div class="muted">out of 100 · ${Math.round(s.confidence * 100)}% of inputs available</div></div>
    </div>
    <p class="muted">${esc(s.summary)}</p>${comps}`;
}

function renderDistribution(r) {
  const d = r.distribution;
  if (!d.available) {
    $("dist-method").textContent = "Not available: " + (d.reason || "");
    return;
  }
  $("dist-expiry").textContent = `${d.expiry} · ${d.dte} days out`;
  $("dist-method").textContent = `Method: ${d.method}.`;
  chart("dist-chart", {
    type: "line",
    data: {
      labels: d.curve.map((p) => p.price),
      datasets: [{
        data: d.curve.map((p) => p.density),
        borderColor: "#4da3ff", backgroundColor: "rgba(77,163,255,.15)",
        fill: true, pointRadius: 0, borderWidth: 2, tension: 0.25,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { title: (i) => "$" + Number(i[0].label).toFixed(2), label: () => "" } } },
      scales: {
        x: { ticks: { color: AXIS, maxTicksLimit: 10,
              callback(v) { return "$" + Number(this.getLabelForValue(v)).toFixed(0); } },
             grid: { color: GRID } },
        y: { display: false, grid: { display: false } },
      },
    },
  });
  const p = d.percentiles;
  $("dist-stats").innerHTML = `
    <div class="stat"><div class="k">Implied median</div><div class="v">${money(d.median)}</div></div>
    <div class="stat"><div class="k">Mean</div><div class="v">${money(d.mean)}</div></div>
    <div class="stat"><div class="k">Above spot</div><div class="v">${pct(d.prob_above_spot)}</div></div>
    <div class="stat"><div class="k">Skew</div><div class="v">${fmt(d.skew, 2)}</div></div>
    <div class="stat"><div class="k">5th pct</div><div class="v">${money(p.p5)}</div></div>
    <div class="stat"><div class="k">25th pct</div><div class="v">${money(p.p25)}</div></div>
    <div class="stat"><div class="k">75th pct</div><div class="v">${money(p.p75)}</div></div>
    <div class="stat"><div class="k">95th pct</div><div class="v">${money(p.p95)}</div></div>`;
}

function renderLadder(r) {
  const rows = (r.distribution.available && r.distribution.strike_ladder) || [];
  $("ladder").innerHTML =
    `<thead><tr><th>Move</th><th>Price</th><th>Chance above</th><th>Chance below</th></tr></thead><tbody>` +
    rows.map((l) => `<tr class="${l.move_pct === 0 ? "here" : ""}">
      <td>${l.move_pct > 0 ? "+" : ""}${l.move_pct}%</td><td>${money(l.price)}</td>
      <td class="call">${pct(l.prob_above)}</td><td class="put">${pct(l.prob_below)}</td></tr>`).join("") +
    "</tbody>";
}

function renderGamma(r) {
  const g = r.gamma;
  if (!g.available) {
    $("gamma-note").textContent = "Not available: " + (g.reason || "");
    if (charts["gamma-chart"]) charts["gamma-chart"].destroy();
    return;
  }
  $("gamma-note").textContent =
    `Net ${fmt(g.net_gex_millions, 1)}M per 1% move · flip near ${
      g.flip_level ? money(g.flip_level) : "not found in range"} · call wall ${
      money(g.call_wall)} · put wall ${money(g.put_wall)}`;
  const values = g.profile.map((p) => p.net / 1e6);
  chart("gamma-chart", {
    type: "bar",
    data: { labels: g.profile.map((p) => p.strike),
      datasets: [{ data: values,
        backgroundColor: values.map((v) => v >= 0 ? "rgba(53,208,127,.7)" : "rgba(255,107,107,.7)") }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } }, scales: baseScales() },
  });
}

function renderFlow(r) {
  const f = r.flow;
  if (!f.available) {
    $("flow-summary").textContent = "Not available: " + (f.reason || "");
    $("flow").innerHTML = ""; return;
  }
  $("flow-summary").textContent =
    `${f.tilt_label} Call volume ${compact(f.call_volume)} vs put ${compact(f.put_volume)} ` +
    `(ratio ${fmt(f.call_put_volume_ratio, 2)}). Premium ${usd(f.call_premium)} calls vs ${usd(f.put_premium)} puts.` +
    (f.has_open_interest ? "" : " Open interest unavailable on this plan.");
  const rows = f.unusual || [];
  $("flow").innerHTML =
    `<thead><tr><th>Contract</th><th>Type</th><th>Strike</th><th>Exp</th><th>DTE</th><th>Vol</th>
     <th>OI</th><th>V/OI</th><th>Premium</th><th>IV</th><th>Score</th><th class="reasons">Why</th></tr></thead><tbody>` +
    (rows.length ? rows.map((u) => `<tr>
      <td>${esc(u.ticker)}</td><td class="${u.kind}">${u.kind}</td>
      <td>${fmt(u.strike, 2)} <span class="muted">(${u.moneyness_pct > 0 ? "+" : ""}${fmt(u.moneyness_pct, 1)}%)</span></td>
      <td>${u.expiry}</td><td>${fmt(u.dte, 0)}</td><td>${compact(u.volume)}</td>
      <td>${u.open_interest ? compact(u.open_interest) : "—"}</td>
      <td>${u.vol_oi_ratio ? fmt(u.vol_oi_ratio, 1) + "x" : "—"}</td>
      <td>${usd(u.notional)}</td><td>${u.iv ? fmt(u.iv, 0) + "%" : "—"}</td>
      <td>${fmt(u.score, 0)}</td><td class="reasons">${esc((u.reasons || []).join("; "))}</td></tr>`).join("")
      : `<tr><td colspan="12" class="muted">Nothing unusual in the fetched contracts.</td></tr>`) +
    "</tbody>";
}

function renderSkew(r) {
  const rows = (r.skew.available && r.skew.by_expiry) || [];
  $("skew").innerHTML =
    `<thead><tr><th>Expiry</th><th>DTE</th><th>25Δ call</th><th>25Δ put</th><th>RR</th></tr></thead><tbody>` +
    (rows.length ? rows.map((k) => `<tr><td>${k.expiry}</td><td>${fmt(k.dte, 0)}</td>
      <td>${pct(k.call_25d_iv)}</td><td>${pct(k.put_25d_iv)}</td>
      <td class="${k.risk_reversal > 0 ? "call" : "put"}">${k.risk_reversal > 0 ? "+" : ""}${fmt(k.risk_reversal, 2)}</td></tr>`).join("")
      : `<tr><td colspan="5" class="muted">Not enough delta coverage.</td></tr>`) + "</tbody>";
}

function renderChain(r) {
  chainCount = r.contract_count;
  $("chain").innerHTML =
    `<thead><tr><th>Contract</th><th>Type</th><th>Strike</th><th>Exp</th><th>DTE</th><th>Bid</th><th>Ask</th>
     <th>Price</th><th>Vol</th><th>OI</th><th>IV</th><th>Δ</th><th>Γ</th><th>Θ</th></tr></thead><tbody>` +
    r.chain.map((c) => `<tr><td>${esc(c.ticker)}</td><td class="${c.kind}">${c.kind}</td>
      <td>${fmt(c.strike, 2)}</td><td>${c.expiry}</td><td>${fmt(c.dte, 0)}</td>
      <td>${c.bid !== null ? fmt(c.bid, 2) : "—"}</td><td>${c.ask !== null ? fmt(c.ask, 2) : "—"}</td>
      <td>${c.price !== null ? fmt(c.price, 2) : "—"}</td>
      <td>${c.volume ? compact(c.volume) : "—"}</td><td>${c.open_interest ? compact(c.open_interest) : "—"}</td>
      <td>${c.iv ? pct(c.iv * 100) : "—"}</td><td>${c.delta !== null ? fmt(c.delta, 3) : "—"}</td>
      <td>${c.gamma !== null ? fmt(c.gamma, 4) : "—"}</td><td>${c.theta !== null ? fmt(c.theta, 3) : "—"}</td></tr>`).join("") +
    "</tbody>";
  $("chain-toggle").textContent = `Show full chain (${chainCount} contracts)`;
}

$("chain-toggle").addEventListener("click", () => {
  const wrap = $("chain-wrap");
  wrap.classList.toggle("hidden");
  $("chain-toggle").textContent = wrap.classList.contains("hidden")
    ? `Show full chain (${chainCount} contracts)` : "Hide chain";
});

/* ------------------------------------------------------------------ */
/* Volatility & decay tab                                              */
/* ------------------------------------------------------------------ */

const PALETTE = ["#4da3ff", "#35d07f", "#ffb84d", "#ff6b6b", "#b48cff", "#4dd0e1"];

function renderVol(r) {
  $("vol-empty").classList.add("hidden");
  $("vol-content").classList.remove("hidden");

  const vt = r.vol_terms || {};
  $("vrp-verdict").textContent = vt.verdict || "";
  $("vrp-explainer").textContent = vt.explainer || "";
  const rv = vt.realized || {};
  const front = (vt.terms || [])[0] || {};
  $("vrp-stats").innerHTML = `
    <div class="stat"><div class="k">Front IV</div><div class="v">${pct(front.iv_pct)}</div></div>
    <div class="stat"><div class="k">Realised 10d</div><div class="v">${pct(rv.rv_10d)}</div></div>
    <div class="stat"><div class="k">Realised 21d</div><div class="v">${pct(rv.rv_21d)}</div></div>
    <div class="stat"><div class="k">Realised 63d</div><div class="v">${pct(rv.rv_63d)}</div></div>
    <div class="stat"><div class="k">Realised 126d</div><div class="v">${pct(rv.rv_126d)}</div></div>
    <div class="stat"><div class="k">Premium</div><div class="v">${
      front.premium_vs_rv_pts !== null && front.premium_vs_rv_pts !== undefined
        ? (front.premium_vs_rv_pts > 0 ? "+" : "") + fmt(front.premium_vs_rv_pts, 1) + " pts" : "—"}</div></div>
    <div class="stat"><div class="k">Implied daily</div><div class="v">${pct(front.daily_move_pct, 2)}</div></div>`;

  const surf = r.iv_surface || {};
  if (surf.available) {
    chart("smile-chart", {
      type: "line",
      data: {
        datasets: surf.layers.map((layer, i) => ({
          label: `${layer.expiry} (${layer.dte}d)`,
          data: layer.points.map((p) => ({ x: p.moneyness_pct, y: p.iv_pct })),
          borderColor: PALETTE[i % PALETTE.length],
          backgroundColor: PALETTE[i % PALETTE.length],
          pointRadius: 2, borderWidth: 2, tension: 0.3, fill: false,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: AXIS, boxWidth: 10, font: { size: 10 } } } },
        scales: {
          x: { type: "linear", title: { display: true, text: "% from spot", color: AXIS },
               ticks: { color: AXIS }, grid: { color: GRID } },
          y: { title: { display: true, text: "implied vol %", color: AXIS },
               ticks: { color: AXIS }, grid: { color: GRID } },
        },
      },
    });
  }

  const terms = vt.terms || [];
  $("term-shape").textContent = vt.shape || "";
  if (terms.length) {
    chart("term-chart", {
      type: "line",
      data: {
        labels: terms.map((t) => `${t.expiry}`),
        datasets: [
          { label: "ATM implied vol %", data: terms.map((t) => t.iv_pct),
            borderColor: "#4da3ff", backgroundColor: "rgba(77,163,255,.15)",
            fill: true, tension: 0.3, pointRadius: 3 },
          { label: "Expected move %", data: terms.map((t) => t.expected_move_pct),
            borderColor: "#ffb84d", borderDash: [5, 4], fill: false,
            tension: 0.3, pointRadius: 2 },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: AXIS, boxWidth: 10, font: { size: 10 } } } },
        scales: baseScales() },
    });
  }

  const d = r.decay || {};
  $("decay-summary").textContent = d.summary || (d.reason ? "Not available: " + d.reason : "");
  $("decay-explainer").textContent = d.explainer || "";
  if (d.available) {
    chart("decay-chart", {
      type: "line",
      data: {
        datasets: d.profiles.map((p, i) => ({
          label: `${p.kind} ${p.strike} ${p.expiry}`,
          data: p.curve.map((c) => ({ x: c.days_held, y: c.value * 100 })),
          borderColor: PALETTE[i % PALETTE.length], pointRadius: 0,
          borderWidth: 2, tension: 0.2, fill: false,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: AXIS, boxWidth: 10, font: { size: 10 } } } },
        scales: {
          x: { type: "linear", title: { display: true, text: "days held", color: AXIS },
               ticks: { color: AXIS }, grid: { color: GRID } },
          y: { title: { display: true, text: "contract value $", color: AXIS },
               ticks: { color: AXIS }, grid: { color: GRID } },
        },
      },
    });
    $("decay-table").innerHTML =
      `<thead><tr><th>Contract</th><th>DTE</th><th>Premium</th><th>IV</th><th>Δ</th>
       <th>Θ/day</th><th>% premium/day</th><th>Breakeven move/day</th><th>Half-life</th></tr></thead><tbody>` +
      d.profiles.map((p) => `<tr>
        <td class="${p.kind}">${p.kind} ${fmt(p.strike, 2)} ${p.expiry}</td>
        <td>${p.dte}</td><td>$${fmt(p.total_premium_at_risk, 0)}</td>
        <td>${pct(p.iv_pct)}</td><td>${fmt(p.delta, 3)}</td>
        <td>$${fmt(Math.abs(p.theta_per_day) * 100, 2)}</td>
        <td>${p.theta_pct_of_premium ? pct(p.theta_pct_of_premium, 2) : "—"}</td>
        <td>${p.breakeven_daily_move_pct ? pct(p.breakeven_daily_move_pct, 2) : "—"}</td>
        <td>${p.half_life_days !== null && p.half_life_days !== undefined ? p.half_life_days + "d" : "—"}</td>
      </tr>`).join("") + "</tbody>";
  }
}

/* ------------------------------------------------------------------ */
/* Strategy tab                                                        */
/* ------------------------------------------------------------------ */

async function setupStrategy(r) {
  $("strategy-empty").classList.add("hidden");
  $("strategy-content").classList.remove("hidden");

  if (!$("preset").dataset.loaded) {
    try {
      const res = await fetch("/api/strategy/presets");
      const data = await res.json();
      $("preset").innerHTML = data.presets
        .map((p) => `<option value="${p.key}">${esc(p.label)}</option>`).join("");
      $("preset").dataset.loaded = "1";
    } catch (e) { /* leave empty */ }
  }
  $("strategy-expiry").innerHTML = (r.expiries || [])
    .map((e) => `<option value="${e}"${e === r.primary_expiry ? " selected" : ""}>${e}</option>`).join("");
  buildStrategy();
}

$("build").addEventListener("click", buildStrategy);

async function buildStrategy() {
  if (!current) return;
  const body = {
    preset: $("preset").value,
    expiry: $("strategy-expiry").value,
    spot: current.spot,
    chain: current.chain,
    density: current.distribution && current.distribution.available
      ? current.distribution.curve : null,
  };
  try {
    const res = await fetch("/api/strategy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    renderPayoff(data);
  } catch (err) {
    $("payoff-note").textContent = err.message;
  }
}

function renderPayoff(s) {
  if (!s.available) {
    $("payoff-label").textContent = "";
    $("payoff-stats").innerHTML = `<p class="muted">${esc(s.reason || "Could not build.")}</p>`;
    $("strategy-legs").innerHTML = "";
    return;
  }
  $("payoff-label").textContent = s.label || "";
  $("strategy-legs").innerHTML = `<div class="legrow">` + (s.legs || []).map((l) =>
    `<span>${l.qty > 0 ? "+" : ""}${l.qty} ${esc(l.kind)}${
      l.strike ? " @ " + fmt(l.strike, 2) : ""}${
      l.premium !== undefined && l.premium !== null ? " for " + fmt(l.premium, 2) : ""}</span>`).join("") + `</div>`;

  const pts = s.points;
  chart("payoff-chart", {
    type: "line",
    data: {
      labels: pts.map((p) => p.price),
      datasets: [{
        data: pts.map((p) => p.pnl),
        borderColor: "#4da3ff", borderWidth: 2, pointRadius: 0, tension: 0,
        segment: { borderColor: (ctx) => ctx.p1.parsed.y >= 0 ? "#35d07f" : "#ff6b6b" },
        fill: { target: { value: 0 }, above: "rgba(53,208,127,.12)", below: "rgba(255,107,107,.12)" },
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: {
          title: (i) => "Stock at $" + Number(i[0].label).toFixed(2),
          label: (i) => "P&L $" + Number(i.parsed.y).toFixed(0) } } },
      scales: {
        x: { ticks: { color: AXIS, maxTicksLimit: 10,
              callback(v) { return "$" + Number(this.getLabelForValue(v)).toFixed(0); } },
             grid: { color: GRID } },
        y: { ticks: { color: AXIS }, grid: { color: GRID } },
      },
    },
  });

  $("payoff-stats").innerHTML = `
    <div class="stat"><div class="k">Net ${esc(s.direction || "")}</div><div class="v">$${fmt(Math.abs(s.net_cost), 0)}</div></div>
    <div class="stat"><div class="k">Max profit</div><div class="v">${s.max_profit === null ? "Unlimited" : "$" + fmt(s.max_profit, 0)}</div></div>
    <div class="stat"><div class="k">Max loss</div><div class="v">$${fmt(Math.abs(s.max_loss), 0)}</div></div>
    <div class="stat"><div class="k">Break-even</div><div class="v">${
      (s.breakevens || []).length ? s.breakevens.map((b) => "$" + fmt(b, 2)).join(", ") : "—"}</div></div>
    ${s.probability_of_profit_pct !== undefined ? `
    <div class="stat"><div class="k">Chance of profit</div><div class="v">${pct(s.probability_of_profit_pct)}</div></div>
    <div class="stat"><div class="k">Expected value</div><div class="v">$${fmt(s.expected_value, 0)}</div></div>` : ""}`;
  $("payoff-note").textContent = s.probability_note || "";
}

/* ------------------------------------------------------------------ */
/* Fundamentals tab                                                    */
/* ------------------------------------------------------------------ */

$("fund-timeframe").addEventListener("change", () => { fundData = null; loadFundamentals(); });
$("metric-select").addEventListener("change", drawFundChart);
$("fund-view").addEventListener("change", drawFundChart);

async function loadFundamentals() {
  if (!current) return;
  const tf = $("fund-timeframe").value;
  $("fund-status").classList.remove("hidden");
  $("fund-status").innerHTML = `<p class="muted">Loading fundamentals from SEC EDGAR…</p>`;
  try {
    const res = await fetch(`/api/fundamentals/${current.ticker}?timeframe=${tf}`);
    const data = await res.json();
    if (!data.available) {
      $("fund-status").innerHTML = `<p class="muted">${esc(data.reason)}</p>`;
      $("fund-content").classList.add("hidden");
      return;
    }
    fundData = data;
    $("fund-status").classList.add("hidden");
    $("fund-content").classList.remove("hidden");
    renderFundamentals(data);
  } catch (err) {
    $("fund-status").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function renderFundamentals(d) {
  const all = [...d.metrics, ...(d.ratios || [])];
  $("metric-select").innerHTML = all
    .map((m, i) => `<option value="${i}">${esc(m.label)}</option>`).join("");
  $("fund-source").textContent =
    `${d.entity || d.ticker} · CIK ${d.cik} · ${d.timeframe} · ${d.source}`;
  drawFundChart();

  $("growth-table").innerHTML =
    `<thead><tr><th>Metric</th><th>Latest</th><th>Period</th><th>YoY growth</th></tr></thead><tbody>` +
    d.metrics.map((m) => {
      const g = m.yoy_pct;
      const cls = g === null || g === undefined ? "" : (g >= 0 ? "call" : "put");
      return `<tr><td>${esc(m.label)}</td>
        <td>${m.unit === "USD" ? usd(m.latest) : fmt(m.latest, 2)}</td>
        <td>${m.latest_period}</td>
        <td class="${cls}">${g === null || g === undefined ? "—" : (g > 0 ? "+" : "") + fmt(g, 1) + "%"}</td></tr>`;
    }).join("") + "</tbody>";

  const ratios = d.ratios || [];
  $("ratio-table").innerHTML =
    `<thead><tr><th>Ratio</th><th>Latest</th><th>Period</th><th>Change YoY</th></tr></thead><tbody>` +
    (ratios.length ? ratios.map((m) => {
      const g = m.yoy_pct;
      const cls = g === null || g === undefined ? "" : (g >= 0 ? "call" : "put");
      return `<tr><td>${esc(m.label)}</td><td>${fmt(m.latest, 2)}${m.unit === "%" ? "%" : "x"}</td>
        <td>${m.latest_period}</td>
        <td class="${cls}">${g === null || g === undefined ? "—" : (g > 0 ? "+" : "") + fmt(g, 1) + "%"}</td></tr>`;
    }).join("") : `<tr><td colspan="4" class="muted">Not enough overlapping data to derive ratios.</td></tr>`) +
    "</tbody>";
}

function drawFundChart() {
  if (!fundData) return;
  const all = [...fundData.metrics, ...(fundData.ratios || [])];
  const m = all[Number($("metric-select").value) || 0];
  if (!m) return;
  const view = $("fund-view").value;

  const series = m.series;
  const key = view === "yoy" ? "yoy_pct" : view === "qoq" ? "qoq_pct" : "value";
  const rows = series.filter((s) => s[key] !== null && s[key] !== undefined);
  const isGrowth = view !== "value";

  chart("fund-chart", {
    type: isGrowth ? "bar" : "line",
    data: {
      labels: rows.map((s) => s.period_end),
      datasets: [{
        label: m.label + (isGrowth ? " growth %" : ""),
        data: rows.map((s) => s[key]),
        borderColor: "#4da3ff",
        backgroundColor: isGrowth
          ? rows.map((s) => s[key] >= 0 ? "rgba(53,208,127,.7)" : "rgba(255,107,107,.7)")
          : "rgba(77,163,255,.15)",
        fill: !isGrowth, tension: 0.25, pointRadius: 2, borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (i) =>
          isGrowth ? fmt(i.parsed.y, 1) + "%"
                   : (m.unit === "USD" ? usd(i.parsed.y) : fmt(i.parsed.y, 2)) } } },
      scales: {
        x: { ticks: { color: AXIS, maxTicksLimit: 12 }, grid: { color: GRID } },
        y: { ticks: { color: AXIS, callback: (v) =>
              isGrowth ? v + "%" : (m.unit === "USD" ? compact(v) : v) },
             grid: { color: GRID } },
      },
    },
  });
}

/* ------------------------------------------------------------------ */
/* People tab                                                          */
/* ------------------------------------------------------------------ */

async function loadPeople() {
  if (!current) return;
  $("people-status").classList.remove("hidden");
  $("people-status").innerHTML = `<p class="muted">Loading Form 4 filings…</p>`;
  try {
    const res = await fetch(`/api/people/${current.ticker}`);
    const d = await res.json();
    $("people-content").dataset.loaded = "1";
    renderPeople(d);
  } catch (err) {
    $("people-status").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function renderPeople(d) {
  const ins = d.insiders || {};
  const ov = d.overview;
  $("company-panel").innerHTML = ov ? `
    <h3>${esc(ov.name || d.ticker)}</h3>
    <p class="muted">${esc(ov.sector || "")}${ov.employees ? " · " + compact(ov.employees) + " employees" : ""}</p>
    ${ov.description ? `<p class="fineprint">${esc(String(ov.description).slice(0, 600))}</p>` : ""}
    <div class="legrow">
      ${ov.homepage ? `<span><a href="${esc(ov.homepage)}" target="_blank" rel="noopener">Website</a></span>` : ""}
      ${(d.leadership && d.leadership.linkedin_company)
        ? `<span><a href="${esc(d.leadership.linkedin_company)}" target="_blank" rel="noopener">LinkedIn company search</a></span>` : ""}
    </div>` : `<p class="muted">No company profile available.</p>`;

  if (!ins.available) {
    $("people-status").classList.remove("hidden");
    $("people-status").innerHTML = `<p class="muted">${esc(ins.reason || "No insider data.")}</p>`;
    $("people-content").classList.remove("hidden");
    $("insider-verdict").textContent = "";
    $("people-table").innerHTML = "";
    $("insider-table").innerHTML = "";
    $("insider-stats").innerHTML = "";
    return;
  }

  $("people-status").classList.add("hidden");
  $("people-content").classList.remove("hidden");

  const s = ins.summary;
  $("insider-verdict").textContent = s.verdict;
  $("insider-stats").innerHTML = `
    <div class="stat"><div class="k">Bought</div><div class="v call">${usd(s.bought_value)}</div></div>
    <div class="stat"><div class="k">Sold</div><div class="v put">${usd(s.sold_value)}</div></div>
    <div class="stat"><div class="k">Net</div><div class="v ${s.net_value >= 0 ? "call" : "put"}">${usd(s.net_value)}</div></div>
    <div class="stat"><div class="k">Buys / sells</div><div class="v">${s.buy_count} / ${s.sell_count}</div></div>
    <div class="stat"><div class="k">Insiders</div><div class="v">${s.insiders_tracked}</div></div>
    <div class="stat"><div class="k">Scheduled (10b5-1)</div><div class="v">${s.planned_10b5_1}</div></div>`;

  $("people-table").innerHTML =
    `<thead><tr><th>Name</th><th>Role</th><th>Bought</th><th>Sold</th><th>Net</th>
     <th>Shares held</th><th>Last filing</th><th>Links</th></tr></thead><tbody>` +
    ins.people.map((p) => `<tr>
      <td>${esc(p.name)}</td><td>${esc(p.role)}</td>
      <td class="call">${p.bought_value ? usd(p.bought_value) : "—"}</td>
      <td class="put">${p.sold_value ? usd(p.sold_value) : "—"}</td>
      <td class="${p.net_value >= 0 ? "call" : "put"}">${p.net_value ? usd(p.net_value) : "—"}</td>
      <td>${p.shares_owned ? compact(p.shares_owned) : "—"}</td>
      <td>${p.last_activity || "—"}</td>
      <td><a href="${esc(p.linkedin_search)}" target="_blank" rel="noopener">LinkedIn</a>${
        p.sec_url ? ` · <a href="${esc(p.sec_url)}" target="_blank" rel="noopener">SEC</a>` : ""}</td>
    </tr>`).join("") + "</tbody>";

  $("insider-table").innerHTML =
    `<thead><tr><th>Date</th><th>Name</th><th>Action</th><th>Shares</th><th>Price</th>
     <th>Value</th><th>Planned</th><th></th></tr></thead><tbody>` +
    ins.transactions.map((t) => `<tr>
      <td>${t.date || "—"}</td><td>${esc(t.name)}</td>
      <td class="${t.bucket === "buy" ? "call" : t.bucket === "sell" ? "put" : ""}">${esc(t.action)}</td>
      <td>${compact(t.shares)}</td><td>${t.price ? money(t.price) : "—"}</td>
      <td>${usd(t.value)}</td><td>${t.planned_10b5_1 ? "10b5-1" : "—"}</td>
      <td>${t.url ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">filing</a>` : ""}</td>
    </tr>`).join("") + "</tbody>";
  $("insider-caveat").textContent = ins.caveat || "";
}

/* ------------------------------------------------------------------ */
/* Politics tab                                                        */
/* ------------------------------------------------------------------ */

async function loadPolitics() {
  if (!current) return;
  $("politics-status").classList.remove("hidden");
  $("politics-status").innerHTML = `<p class="muted">Searching lobbying disclosures…</p>`;
  try {
    const res = await fetch(`/api/politics/${current.ticker}`);
    const d = await res.json();
    $("politics-content").dataset.loaded = "1";
    renderPolitics(d);
  } catch (err) {
    $("politics-status").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function renderPolitics(d) {
  const L = d.lobbying || {};
  if (!L.available) {
    $("politics-status").classList.remove("hidden");
    $("politics-status").innerHTML =
      `<p class="muted">${esc(L.reason || "No lobbying data.")}</p>`;
    $("politics-content").classList.add("hidden");
    return;
  }
  $("politics-status").classList.add("hidden");
  $("politics-content").classList.remove("hidden");

  $("lobby-summary").textContent =
    `${L.company} reported ${usd(L.total_reported)} across ${L.filing_count} filings ` +
    `covering ${L.years_covered.join(", ")}. Filed under: ${L.matched_clients.slice(0, 3).join("; ")}.`;

  chart("lobby-chart", {
    type: "bar",
    data: {
      labels: L.by_year.map((y) => y.year).reverse(),
      datasets: [{ data: L.by_year.map((y) => y.amount).reverse(),
        backgroundColor: "rgba(77,163,255,.7)" }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (i) => usd(i.parsed.y) } } },
      scales: { x: { ticks: { color: AXIS }, grid: { color: GRID } },
                y: { ticks: { color: AXIS, callback: (v) => compact(v) }, grid: { color: GRID } } } },
  });

  const tbl = (id, head, rows, render) => {
    $(id).innerHTML = `<thead><tr>${head}</tr></thead><tbody>` +
      (rows.length ? rows.map(render).join("")
        : `<tr><td colspan="4" class="muted">Nothing reported.</td></tr>`) + "</tbody>";
  };

  tbl("issue-table", "<th>Issue</th><th>Approx. spend</th>", L.by_issue,
    (r) => `<tr><td>${esc(r.issue)}</td><td>${usd(r.amount)}</td></tr>`);
  tbl("target-table", "<th>Body contacted</th><th>Mentions</th>", L.targets,
    (r) => `<tr><td>${esc(r.entity)}</td><td>${r.mentions}</td></tr>`);
  tbl("firm-table", "<th>Firm</th><th>Paid</th>", L.by_registrant,
    (r) => `<tr><td>${esc(r.firm)}</td><td>${usd(r.amount)}</td></tr>`);
  tbl("revolving-table", "<th>Name</th><th>Previously</th><th></th>", L.revolving_door,
    (r) => `<tr><td>${esc(r.name)}</td><td class="reasons">${esc(r.covered_position)}</td>
      <td><a href="${esc(r.linkedin_search)}" target="_blank" rel="noopener">LinkedIn</a></td></tr>`);
  tbl("lobbyist-table", "<th>Name</th><th>Filings</th><th>Firms</th><th></th>", L.lobbyists,
    (r) => `<tr><td>${esc(r.name)}</td><td>${r.filings}</td>
      <td class="reasons">${esc(r.firms.join(", "))}</td>
      <td><a href="${esc(r.linkedin_search)}" target="_blank" rel="noopener">LinkedIn</a></td></tr>`);

  $("lobby-caveat").textContent = (L.caveat || "") + " Source: " + (L.source || "");

  const C = d.committees || {};
  if (!C.available) {
    $("fec-body").innerHTML = `<p class="muted">${esc(C.reason || "Not available.")}</p>` +
      (C.needs_key ? `<p class="fineprint">Add <span class="env">FEC_API_KEY</span> in Render's
        Environment tab to switch this on. See the API Stock tab.</p>` : "");
  } else {
    $("fec-body").innerHTML =
      `<div class="table-wrap"><table><thead><tr><th>Committee</th><th>Type</th>
       <th>Last filed</th><th></th></tr></thead><tbody>` +
      C.committees.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.type || "—")}</td>
        <td>${c.last_filed || "—"}</td>
        <td><a href="${esc(c.url)}" target="_blank" rel="noopener">FEC</a></td></tr>`).join("") +
      "</tbody></table></div>";
  }
}

/* ------------------------------------------------------------------ */
/* Kalshi tab                                                          */
/* ------------------------------------------------------------------ */

$("kalshi-search").addEventListener("click", () => {
  const q = $("kalshi-q").value.trim();
  if (q) loadKalshi(q);
});
$("kalshi-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("kalshi-search").click(); }
});
$("kalshi-reset").addEventListener("click", () => { $("kalshi-q").value = ""; loadKalshi(); });

async function loadKalshi(query) {
  const body = $("kalshi-body");
  body.innerHTML = `<section class="panel"><p class="muted">Loading Kalshi markets…</p></section>`;
  try {
    const url = query ? `/api/kalshi/search?q=${encodeURIComponent(query)}` : "/api/kalshi";
    const res = await fetch(url);
    const d = await res.json();
    body.dataset.loaded = "1";
    if (!d.available) {
      body.innerHTML = `<section class="panel"><p class="muted">${esc(d.reason)}</p></section>`;
      return;
    }
    const groups = query
      ? d.events.map((e) => ({ label: e.title, sub: e.sub_title, markets: e.markets, url: e.url }))
      : d.groups.map((g) => ({ label: g.label, sub: g.category, markets: g.markets }));

    body.innerHTML = groups.map((g) => `
      <section class="panel kgroup">
        <h3>${esc(g.label)}</h3>
        ${g.sub ? `<p class="muted">${esc(g.sub)}</p>` : ""}
        <div class="table-wrap"><table>
          <thead><tr><th>Outcome</th><th>Market probability</th><th>Bid</th><th>Ask</th>
          <th>Volume</th><th>Closes</th><th></th></tr></thead><tbody>` +
      g.markets.map((m) => `<tr>
            <td>${esc(m.title || m.ticker)}</td>
            <td class="prob">${pct(m.probability_pct)}</td>
            <td>${m.yes_bid !== null ? (m.yes_bid * 100).toFixed(0) + "¢" : "—"}</td>
            <td>${m.yes_ask !== null ? (m.yes_ask * 100).toFixed(0) + "¢" : "—"}</td>
            <td>${compact(m.volume)}</td>
            <td>${(m.close_time || "").slice(0, 10)}</td>
            <td><a href="${esc(m.url)}" target="_blank" rel="noopener">open</a></td>
          </tr>`).join("") +
      `</tbody></table></div>
      </section>`).join("") +
      `<section class="panel"><p class="fineprint">${esc(d.note || "")} ${esc(d.source || "")}</p></section>`;
  } catch (err) {
    body.innerHTML = `<section class="panel error">${esc(err.message)}</section>`;
  }
}

/* ------------------------------------------------------------------ */
/* API Stock tab                                                       */
/* ------------------------------------------------------------------ */

async function loadSources() {
  const body = $("sources-body");
  if (body.dataset.loaded) return;
  try {
    const res = await fetch("/api/sources");
    const d = await res.json();
    body.dataset.loaded = "1";
    const c = d.counts || {};
    $("sources-counts").innerHTML = `
      <div class="stat"><div class="k">Active</div><div class="v call">${c.active || 0}</div></div>
      <div class="stat"><div class="k">Needs a key</div><div class="v" style="color:var(--warn)">${c.needs_key || 0}</div></div>
      <div class="stat"><div class="k">Not wired up</div><div class="v muted">${c.planned || 0}</div></div>
      <div class="stat"><div class="k">Total tracked</div><div class="v">${d.sources.length}</div></div>`;
    $("sources-how").textContent = d.how_to_add + " " + d.massive_mode_note;

    const byCat = {};
    d.sources.forEach((s) => { (byCat[s.category] = byCat[s.category] || []).push(s); });

    body.innerHTML = Object.entries(byCat).map(([cat, list]) => `
      <section class="panel">
        <h3>${esc(cat)}</h3>
        <div class="source-grid">` +
      list.map((s) => `
          <div class="source-card">
            <div class="source-head">
              <h4>${esc(s.name)}</h4>
              <span class="pill ${s.state}">${esc(s.state_label)}</span>
            </div>
            <div class="legrow"><span class="pill ${s.cost_tier}">${esc(s.cost)}</span></div>
            <p>${esc(s.unlocks)}</p>
            ${s.upgrade_note ? `<p class="fineprint">${esc(s.upgrade_note)}</p>` : ""}
            ${s.env ? `<p class="fineprint">Env var: <span class="env">${esc(s.env)}</span></p>` : ""}
            <a href="${esc(s.url)}" target="_blank" rel="noopener">Open →</a>
          </div>`).join("") +
      `</div></section>`).join("");
  } catch (err) {
    body.innerHTML = `<section class="panel error">${esc(err.message)}</section>`;
  }
}

$("ticker").focus();
