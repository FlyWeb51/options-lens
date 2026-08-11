/* Options Lens front end. Plain JS, no build step. */

const $ = (id) => document.getElementById(id);
let distChart = null;
let gammaChart = null;
let polling = null;

const fmt = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toFixed(d);
const money = (n) => (n === null || n === undefined ? "—" : "$" + Number(n).toFixed(2));
const compact = (n) => {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
};

/* ------------------------------------------------------------------ */
/* Request flow                                                        */
/* ------------------------------------------------------------------ */

$("search").addEventListener("submit", (e) => {
  e.preventDefault();
  run(false);
});
$("refresh").addEventListener("click", () => run(true));

async function run(forceRefresh) {
  const ticker = $("ticker").value.trim().toUpperCase();
  if (!ticker) return;

  clearInterval(polling);
  $("intro").classList.add("hidden");
  $("results").classList.add("hidden");
  $("error").classList.add("hidden");
  $("progress").classList.remove("hidden");
  $("go").disabled = true;
  setProgress(3, "Contacting the data provider…");
  $("progress-title").textContent = `Analysing ${ticker}`;
  $("progress-hint").textContent = "";

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, refresh: forceRefresh }),
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
          "On the free data tier this is limited to 5 requests per minute, so the " +
          "first look at a ticker can take several minutes. The result is cached " +
          "afterwards, so it will be instant next time — for you and anyone else.";
      }

      if (job.status === "done") {
        clearInterval(polling);
        finish(job.result, 0);
      } else if (job.status === "error") {
        clearInterval(polling);
        showError(job.error);
      }
    } catch (err) {
      clearInterval(polling);
      showError(err.message);
    }
  }, 1500);
}

function setProgress(pct, msg) {
  $("bar-fill").style.width = Math.max(pct, 2) + "%";
  $("progress-msg").textContent = msg || "";
}

function showError(message) {
  $("progress").classList.add("hidden");
  $("error").classList.remove("hidden");
  $("error").textContent = message || "Something went wrong.";
  $("go").disabled = false;
}

function finish(result, cacheAge) {
  $("progress").classList.add("hidden");
  $("go").disabled = false;
  render(result, cacheAge);
  $("results").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ------------------------------------------------------------------ */
/* Rendering                                                           */
/* ------------------------------------------------------------------ */

function render(r, cacheAge) {
  renderBanner(r, cacheAge);
  renderVerdict(r);
  renderMove(r);
  renderSqueeze(r);
  renderDistribution(r);
  renderLadder(r);
  renderGamma(r);
  renderTerms(r);
  renderFlow(r);
  renderSkew(r);
  renderChain(r);
}

function renderBanner(r, cacheAge) {
  $("headline").textContent = `${r.ticker} · ${money(r.spot)}`;
  const bits = [
    `${r.contract_count} contracts`,
    `spot from ${r.spot_source.replace("_", " ")}`,
    `risk-free ${fmt(r.risk_free_rate, 2)}%`,
  ];
  if (cacheAge > 0) bits.push(`cached ${Math.round(cacheAge / 60)} min ago`);
  $("subline").textContent = bits.join(" · ");

  const badge = $("mode-badge");
  if (r.mode === "snapshot") {
    badge.textContent = "Live chain snapshot";
    badge.className = "badge live";
  } else {
    badge.textContent = "End-of-day mode";
    badge.className = "badge eod";
  }

  $("warnings").innerHTML = (r.warnings || [])
    .map((w) => `<div class="warning">${esc(w)}</div>`)
    .join("");
}

function renderVerdict(r) {
  $("verdict-list").innerHTML = (r.verdict.lines || [])
    .map((l) => `<li>${esc(l)}</li>`)
    .join("");
  $("verdict-caveat").textContent = r.verdict.caveat;
}

function renderMove(r) {
  const m = r.expected_move;
  const box = $("move-body");
  if (!m.available) {
    box.innerHTML = `<p class="muted">Not available: ${esc(m.reason || "insufficient data")}</p>`;
    return;
  }
  $("move-expiry").textContent = `${m.expiry} · ${m.dte} days out`;

  const lo = m.two_sigma_low;
  const hi = m.two_sigma_high;
  const span = hi - lo || 1;
  const pos = (v) => ((v - lo) / span) * 100;

  box.innerHTML = `
    <div class="bigstat">±${fmt(m.move_pct, 1)}%</div>
    <p class="muted">≈ ±${money(m.move_dollars)} · at-the-money IV ${
      m.atm_iv ? fmt(m.atm_iv * 100, 1) + "%" : "—"
    } · from ${m.source === "straddle" ? "straddle price" : "at-the-money volatility"}</p>

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
  const s = r.squeeze;
  const cls = "score-" + s.label.toLowerCase();
  const comps = s.components
    .map((c) => {
      const pctFill = (c.points / c.max) * 100;
      const val =
        c.value === null || c.value === undefined
          ? "no data"
          : `${c.value} ${c.unit || ""}`;
      return `<div class="component">
        <div class="component-head"><span>${esc(c.name)}</span>
          <span class="muted">${fmt(c.points, 1)} / ${c.max}</span></div>
        <div class="component-bar"><div class="component-fill" style="width:${pctFill}%"></div></div>
        <div class="component-note"><strong>${esc(val)}</strong> — ${esc(c.note)}</div>
      </div>`;
    })
    .join("");

  $("squeeze-body").innerHTML = `
    <div class="score-row">
      <div class="score-num ${cls}">${fmt(s.score, 0)}</div>
      <div>
        <div class="score-label ${cls}">${esc(s.label)}</div>
        <div class="muted">out of 100 · ${Math.round(s.confidence * 100)}% of inputs available</div>
      </div>
    </div>
    <p class="muted">${esc(s.summary)}</p>
    ${comps}`;
}

function renderDistribution(r) {
  const d = r.distribution;
  if (!d.available) {
    $("dist-method").textContent = "Not available: " + (d.reason || "insufficient strikes");
    return;
  }
  $("dist-expiry").textContent = `${d.expiry} · ${d.dte} days out`;
  $("dist-method").textContent =
    `Method: ${d.method}. The curve is the probability density of price at ` +
    `expiration implied by the option chain.`;

  const labels = d.curve.map((p) => p.price);
  const values = d.curve.map((p) => p.density);

  if (distChart) distChart.destroy();
  distChart = new Chart($("dist-chart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          data: values,
          borderColor: "#4da3ff",
          backgroundColor: "rgba(77,163,255,.15)",
          fill: true,
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.25,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => "$" + Number(items[0].label).toFixed(2),
            label: () => "",
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: "#8b98ad",
            maxTicksLimit: 10,
            callback: function (v) {
              return "$" + Number(this.getLabelForValue(v)).toFixed(0);
            },
          },
          grid: { color: "rgba(38,48,65,.5)" },
        },
        y: { display: false, grid: { display: false } },
      },
    },
  });

  const p = d.percentiles;
  $("dist-stats").innerHTML = `
    <div class="stat"><div class="k">Implied median</div><div class="v">${money(d.median)}</div></div>
    <div class="stat"><div class="k">Mean</div><div class="v">${money(d.mean)}</div></div>
    <div class="stat"><div class="k">Above spot</div><div class="v">${fmt(d.prob_above_spot, 1)}%</div></div>
    <div class="stat"><div class="k">Skew</div><div class="v">${fmt(d.skew, 2)}</div></div>
    <div class="stat"><div class="k">5th pct</div><div class="v">${money(p.p5)}</div></div>
    <div class="stat"><div class="k">25th pct</div><div class="v">${money(p.p25)}</div></div>
    <div class="stat"><div class="k">75th pct</div><div class="v">${money(p.p75)}</div></div>
    <div class="stat"><div class="k">95th pct</div><div class="v">${money(p.p95)}</div></div>`;
}

function renderLadder(r) {
  const d = r.distribution;
  const rows = (d.available && d.strike_ladder) || [];
  $("ladder").innerHTML =
    `<thead><tr><th>Move</th><th>Price</th><th>Chance above</th><th>Chance below</th></tr></thead>
     <tbody>` +
    rows
      .map(
        (l) => `<tr class="${l.move_pct === 0 ? "here" : ""}">
        <td>${l.move_pct > 0 ? "+" : ""}${l.move_pct}%</td>
        <td>${money(l.price)}</td>
        <td class="call">${fmt(l.prob_above, 1)}%</td>
        <td class="put">${fmt(l.prob_below, 1)}%</td></tr>`
      )
      .join("") +
    "</tbody>";
}

function renderGamma(r) {
  const g = r.gamma;
  if (!g.available) {
    $("gamma-note").textContent = "Not available: " + (g.reason || "");
    if (gammaChart) gammaChart.destroy();
    return;
  }
  $("gamma-note").textContent =
    `Net ${fmt(g.net_gex_millions, 1)}M per 1% move · flip near ${
      g.flip_level ? money(g.flip_level) : "not found in range"
    } · call wall ${money(g.call_wall)} · put wall ${money(g.put_wall)}`;

  const labels = g.profile.map((p) => p.strike);
  const values = g.profile.map((p) => p.net / 1e6);

  if (gammaChart) gammaChart.destroy();
  gammaChart = new Chart($("gamma-chart"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          data: values,
          backgroundColor: values.map((v) =>
            v >= 0 ? "rgba(53,208,127,.7)" : "rgba(255,107,107,.7)"
          ),
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8b98ad", maxTicksLimit: 12 }, grid: { display: false } },
        y: { ticks: { color: "#8b98ad" }, grid: { color: "rgba(38,48,65,.5)" } },
      },
    },
  });
}

function renderTerms(r) {
  $("terms").innerHTML =
    `<thead><tr><th>Expiry</th><th>DTE</th><th>ATM IV</th><th>Exp. move</th><th>Contracts</th></tr></thead><tbody>` +
    r.term_structure
      .map(
        (t) => `<tr><td>${t.expiry}</td><td>${fmt(t.dte, 0)}</td>
        <td>${t.atm_iv ? fmt(t.atm_iv, 1) + "%" : "—"}</td>
        <td>${t.expected_move_pct ? "±" + fmt(t.expected_move_pct, 1) + "%" : "—"}</td>
        <td>${t.contracts}</td></tr>`
      )
      .join("") +
    "</tbody>";
}

function renderFlow(r) {
  const f = r.flow;
  if (!f.available) {
    $("flow-summary").textContent = "Not available: " + (f.reason || "");
    $("flow").innerHTML = "";
    return;
  }
  $("flow-summary").textContent =
    `${f.tilt_label} Call volume ${compact(f.call_volume)} vs put ${compact(
      f.put_volume
    )} (ratio ${fmt(f.call_put_volume_ratio, 2)}). Premium: ${compact(
      f.call_premium
    )} calls vs ${compact(f.put_premium)} puts.` +
    (f.has_open_interest ? "" : " Open interest unavailable on this data plan, so volume-vs-open-interest ratios are blank.");

  const rows = f.unusual || [];
  $("flow").innerHTML =
    `<thead><tr><th>Contract</th><th>Type</th><th>Strike</th><th>Exp</th><th>DTE</th>
     <th>Vol</th><th>OI</th><th>V/OI</th><th>Premium</th><th>IV</th><th>Score</th><th class="reasons">Why</th></tr></thead><tbody>` +
    (rows.length
      ? rows
          .map(
            (u) => `<tr>
        <td>${esc(u.ticker)}</td>
        <td class="${u.kind}">${u.kind}</td>
        <td>${fmt(u.strike, 2)} <span class="muted">(${u.moneyness_pct > 0 ? "+" : ""}${fmt(
              u.moneyness_pct,
              1
            )}%)</span></td>
        <td>${u.expiry}</td><td>${fmt(u.dte, 0)}</td>
        <td>${compact(u.volume)}</td>
        <td>${u.open_interest ? compact(u.open_interest) : "—"}</td>
        <td>${u.vol_oi_ratio ? fmt(u.vol_oi_ratio, 1) + "x" : "—"}</td>
        <td>${compact(u.notional)}</td>
        <td>${u.iv ? fmt(u.iv, 0) + "%" : "—"}</td>
        <td>${fmt(u.score, 0)}</td>
        <td class="reasons">${esc((u.reasons || []).join("; "))}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="12" class="muted">Nothing stood out in the fetched contracts.</td></tr>`) +
    "</tbody>";
}

function renderSkew(r) {
  const s = r.skew;
  const rows = (s.available && s.by_expiry) || [];
  $("skew").innerHTML =
    `<thead><tr><th>Expiry</th><th>DTE</th><th>25Δ call IV</th><th>25Δ put IV</th><th>Risk reversal</th><th class="reasons">Reading</th></tr></thead><tbody>` +
    (rows.length
      ? rows
          .map(
            (k) => `<tr><td>${k.expiry}</td><td>${fmt(k.dte, 0)}</td>
        <td>${fmt(k.call_25d_iv, 1)}%</td><td>${fmt(k.put_25d_iv, 1)}%</td>
        <td class="${k.risk_reversal > 0 ? "call" : "put"}">${k.risk_reversal > 0 ? "+" : ""}${fmt(
              k.risk_reversal,
              2
            )}</td>
        <td class="reasons">${esc(k.reading)}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="6" class="muted">Not enough delta coverage to compute skew.</td></tr>`) +
    "</tbody>";
}

let chainCount = 0;

function renderChain(r) {
  chainCount = r.contract_count;
  const countSpan = $("chain-count");
  if (countSpan) countSpan.textContent = r.contract_count;
  $("chain").innerHTML =
    `<thead><tr><th>Contract</th><th>Type</th><th>Strike</th><th>Exp</th><th>DTE</th>
     <th>Bid</th><th>Ask</th><th>Price</th><th>Vol</th><th>OI</th><th>IV</th>
     <th>Δ</th><th>Γ</th><th>Θ</th></tr></thead><tbody>` +
    r.chain
      .map(
        (c) => `<tr>
      <td>${esc(c.ticker)}</td><td class="${c.kind}">${c.kind}</td>
      <td>${fmt(c.strike, 2)}</td><td>${c.expiry}</td><td>${fmt(c.dte, 0)}</td>
      <td>${c.bid !== null ? fmt(c.bid, 2) : "—"}</td>
      <td>${c.ask !== null ? fmt(c.ask, 2) : "—"}</td>
      <td>${c.price !== null ? fmt(c.price, 2) : "—"}</td>
      <td>${c.volume ? compact(c.volume) : "—"}</td>
      <td>${c.open_interest ? compact(c.open_interest) : "—"}</td>
      <td>${c.iv ? fmt(c.iv * 100, 1) + "%" : "—"}</td>
      <td>${c.delta !== null ? fmt(c.delta, 3) : "—"}</td>
      <td>${c.gamma !== null ? fmt(c.gamma, 4) : "—"}</td>
      <td>${c.theta !== null ? fmt(c.theta, 3) : "—"}</td></tr>`
      )
      .join("") +
    "</tbody>";
}

$("chain-toggle").addEventListener("click", () => {
  const wrap = $("chain-wrap");
  wrap.classList.toggle("hidden");
  $("chain-toggle").textContent = wrap.classList.contains("hidden")
    ? `Show full chain (${chainCount} contracts)`
    : "Hide chain";
});

function esc(s) {
  return String(s === null || s === undefined ? "" : s).replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}

$("ticker").focus();
