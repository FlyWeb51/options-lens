# Options Lens

Type a ticker, get eight tabs of analysis: what the options market is pricing,
what volatility costs, what a trade would pay off, how the business is actually
performing, who runs it, who they lobby, and what prediction markets think.

## The tabs

| Tab | What's in it | Data source |
|---|---|---|
| **Options** | Expected move, market-implied probability distribution, odds by price level, squeeze score, dealer gamma, unusual activity, skew | Massive |
| **Volatility & Decay** | IV smile by expiry, term structure, implied vs realised volatility (variance risk premium), time-decay curves with break-even and half-life | Massive + computed |
| **Strategy** | Payoff diagrams for 8 structures, priced from the live chain, with probability of profit taken from the chain's own distribution | Computed |
| **Fundamentals** | Revenue, margins, EPS, cash flow, buybacks, debt — charted as values or YoY/sequential growth, plus derived margins and returns | SEC EDGAR (free) |
| **People** | Officers and directors, insider buying and selling, 10b5-1 flags, LinkedIn search links | SEC Form 4 via Massive |
| **Politics** | Lobbying spend by year, issues pushed, firms hired, named lobbyists, revolving-door hires, bodies contacted | Senate LDA (free) |
| **Kalshi** | Real-money probabilities on Fed, CPI, jobs, GDP, recession, indices, crypto — plus keyword search | Kalshi (free) |
| **API Stock** | Every data source: cost, status, what switching it on unlocks | Live registry |

Built on the [Massive.com](https://massive.com) market data API, plus several
free public sources.

## Speed

A cold ticker on the free Massive tier costs one API call per contract at 5
calls per minute. The speed selector controls the tradeoff:

| Mode | Contracts | Expiries | Roughly |
|---|---|---|---|
| Quick (default) | 26 | 2 | ~5 min |
| Standard | 54 | 4 | ~11 min |
| Deep | 96 | 6 | ~19 min |

Results are cached for 6 hours per ticker *per speed mode*, so the second
person to look at a ticker gets it instantly. Upgrading Massive to Options
Starter removes this entirely — the whole chain arrives in one call.

**Free Render instances sleep after 15 minutes idle** and lose their cache
when they do, which also adds ~50 seconds to the first request. That is the
main reason the site feels slow, and no amount of code fixes it.

---

## Read this first: what your free key can and cannot do

Massive splits subscriptions by asset class. A free account gives you
**Stocks Basic** and **Options Basic**. That matters here:

| Data | Free tier | Notes |
|---|---|---|
| Options chain snapshot (greeks, IV, open interest in one call) | **No** | Starts at Options Starter |
| Options contract reference data | Yes | |
| Options previous-day bars (close, volume) | Yes | End-of-day only |
| Short interest, days to cover | **Yes** | All stocks plans |
| Daily short volume | **Yes** | All stocks plans |
| Stock prices | Yes | End-of-day |
| Treasury yields | Yes | |
| Rate limit | **5 requests/minute** | Hard |

The app detects which of these your key can reach on first run and adapts.

### Live snapshot mode (paid options plan)
One paginated call returns the whole chain with greeks, implied volatility and
open interest. A ticker analyses in a few seconds. Everything works.

### End-of-day mode (free options plan)
The snapshot endpoint returns 403, so the app falls back to listing contracts
and pulling previous-day bars one at a time. It then **computes implied
volatility and greeks itself** from those closing prices using Black-Scholes
inversion — that part is fully accurate.

What you lose in this mode:

- Prices are the previous session's closes, not live quotes.
- **Open interest is not published on this tier.** Gamma exposure is estimated
  from traded volume instead, which is a weaker proxy. The app flags this
  everywhere it matters and discounts the gamma component of the squeeze score
  by 40% to reflect the lower confidence.
- At 5 calls per minute, the first look at a ticker takes roughly 15–20 minutes.
  Results are then cached for 6 hours, so it is instant afterwards — for you and
  for anyone else using your link. Lower `MAX_CONTRACTS_EOD` to trade coverage
  for speed.

**If you want this to feel like a real tool, the Options Starter plan (~$29/mo)
is what unlocks it.** Everything else — the whole squeeze engine, the maths, the
UI — works identically either way.

---

## Setup

1. Get a free API key at <https://massive.com/dashboard/keys>.
2. Copy `.env.example` to `.env` and paste the key in:

   ```
   MASSIVE_API_KEY=your_key_here
   ```

3. Start it:

   - **Windows:** double-click `run.bat`
   - **macOS / Linux:** `chmod +x run.sh && ./run.sh`
   - **Manual:** `pip install -r requirements.txt` then
     `uvicorn app.main:app --port 8000`

4. Open <http://localhost:8000>.

The key lives only on the server. It is never sent to the browser, so people
using your link cannot see or spend it.

---

## Sharing it with friends

Running on your laptop only works while your laptop is on. To put it on a real
URL, any of these work with the included `Dockerfile`:

**Render / Railway / Fly.io**

1. Push this folder to a GitHub repo (`.gitignore` already excludes `.env` and
   the cache).
2. Create a new Web Service pointing at the repo. It will detect the Dockerfile.
3. In the service's environment settings, add `MASSIVE_API_KEY`, and
   `ACCESS_PASSWORD` if you want a password gate.
4. Deploy. Send people the URL.

**Set `ACCESS_PASSWORD`** to something you share only with your friends.
Without it the URL is open to anyone who finds it, and since everyone shares
your single rate limit, one crawler could exhaust it.

Because results are cached server-side, ten friends looking at the same ticker
costs the same as one.

---

## What each number means

### Expected move
The one standard deviation range implied by the at-the-money straddle. Roughly
a two-in-three chance of finishing inside it. The straddle is scaled by 1.25
because an ATM straddle prices about 0.8 sigma.

### Implied probability distribution
The headline feature. Using Breeden-Litzenberger: the second derivative of call
price with respect to strike, discounted, *is* the probability density of the
underlying at expiration.

In practice the raw chain is too sparse and noisy to differentiate directly, so
the app:

1. Extracts implied volatility from out-of-the-money options only (they invert
   most cleanly — no intrinsic value to swamp the time value).
2. Smooths and fits a natural cubic spline through the volatility smile.
3. Reprices calls with Black-Scholes on a dense grid of 401 strikes.
4. Takes the second difference and discounts it.
5. Clips any negative density (numerical artifact) and renormalises to 1.

From the resulting density you get the median, mean, percentiles, skew, and the
odds of finishing above or below any price level.

**This is a risk-neutral distribution.** It tells you what the market *charges*
for each outcome, not what will happen. It bakes in a risk premium and
systematically overstates tail probabilities — especially downside, because
people pay up for crash protection. Treat it as a map of market pricing, not a
forecast. Where it is most useful is spotting disagreement: if you think a move
is more likely than the ladder says, that is a mispricing you can act on.

### Squeeze score (0–100)

Five components, each shown with its own inputs:

| Component | Max | What it measures |
|---|---|---|
| Short interest | 28 | Shares short as a % of shares outstanding. Above ~15% is genuinely crowded. |
| Days to cover | 20 | Short interest ÷ average daily volume. How long shorts need to buy back. |
| Daily short volume | 12 | % of recent reported volume sold short. Are shorts still pressing? |
| Dealer gamma | 25 | Whether market makers' hedging amplifies or damps moves, and how close price is to the flip. |
| Call demand | 15 | Call/put volume ratio and the share of call volume that is out of the money. |

**Gamma exposure** assumes dealers are short calls and long puts (the standard
retail-flow assumption). Negative net gamma means their hedging *adds* to
whichever direction the stock moves — that is the regime gamma squeezes happen
in. The app also solves for the gamma flip level, the price where net exposure
crosses zero and the regime changes.

A high score does **not** mean the stock goes up. It means the stock is capable
of moving much further than the option market is pricing, in either direction.
Squeezes unwind violently too.

### Unusual activity
Ranks contracts by volume relative to open interest, absolute size, premium
committed, and how far out of the money and short dated they are. Every row
shows its reasons.

Honest limitation: the data does not say whether a trade was a buy or a sell,
or who made it. "Volume is 8x open interest" means new positioning happened. It
does not tell you which way somebody is leaning.

### Skew (25-delta risk reversal)
The implied volatility of a 25-delta call minus a 25-delta put. Normally
negative for equities — puts cost more. A **positive** reading is unusual and is
a hallmark of squeeze or momentum positioning: people are paying up for upside.

---

## What is deliberately NOT here

Being straight about the gaps:

- **Analyst consensus / beat-miss.** Real consensus estimates are not
  available free anywhere reliable. The Fundamentals tab gives you actual
  reported results and year-over-year growth, which is real data. It does not
  claim to tell you whether a quarter beat expectations, because that would
  require inventing the expectation. Add a Finnhub or FMP key to fill this in.
- **LinkedIn profiles.** Scraping LinkedIn violates their terms. The People
  tab generates search links instead, which get you to the right person in one
  click without the app pretending to have data it does not.
- **A full org chart.** The People tab is built from Form 4 filers, so it
  covers Section 16 officers and directors who have transacted. Someone who
  has not filed recently will not appear.
- **Congressional trading.** Needs a paid source (QuiverQuant). Listed in the
  API Stock tab.

## Verifying it works

Three test suites, no framework required:

```bash
python -m tests.test_math      # maths against published reference values
python -m tests.test_pipeline  # full pipeline with a stubbed API, plus routes
python -m tests.test_features  # volatility, decay, strategy, SEC, Kalshi, registry
```

`test_features` covers the v2 additions: realised volatility against a known
path, decay curves that must fall monotonically, payoff maths checked against
closed-form results for calls, puts, spreads and straddles, probability of
profit that must be near zero expected value on a fairly priced option, SEC
XBRL parsing including restatement handling, and the API registry.

`test_math` checks Black-Scholes against Hull's textbook values, verifies
put-call parity to 1e-8, confirms gamma matches a finite difference of delta,
round-trips the implied volatility solver to within 3e-8, and — most
importantly — verifies that the recovered probability density integrates to 1
and that its median, mean and P(above spot) match the lognormal closed form
when fed a flat volatility surface.

`test_pipeline` runs a synthetic 136-contract chain with a realistic downside
smirk through both data paths and checks the two modes agree.

---

## Project layout

```
app/
  config.py              settings from environment
  massive.py             API client: rate limiting, caching, plan detection
  models.py              Contract and Chain - the shape everything speaks
  chain.py               fetching, both modes
  analyze.py             orchestrator
  analytics/
    bs.py                Black-Scholes, implied vol solver, greeks
    pricing.py           expected move, risk-neutral distribution, term structure
    squeeze.py           short interest, gamma exposure, composite score
    flow.py              unusual activity, skew
  main.py                FastAPI routes and background jobs
static/                  the front end (no build step)
tests/                   verification
```

### Adding your own analysis

Each analytics module takes a normalised `Chain` and returns a plain dict. To
add something new:

1. Write `app/analytics/yourthing.py` with a function taking `Chain`.
2. Call it in `analyze.py` and add the result to the returned dict.
3. Add a render function in `static/app.js` and a panel in `index.html`.

Ideas the structure already supports: earnings-date awareness (Massive has a
corporate events endpoint), IV rank versus its own history, backtesting the
squeeze score against subsequent returns, multi-ticker watchlist scanning,
alerting when net gamma flips negative.

---

## Limitations worth knowing

- Dividends are treated as zero. For high-yield names this biases put implied
  volatility slightly.
- American exercise is priced with a European model. The error is small for
  most equity options but grows for deep in-the-money puts.
- Short interest is FINRA data on a two-week lag. It is a real number but not a
  current one.
- Short interest is expressed as a share of shares *outstanding*, not free
  float, because float requires a paid add-on. For names with large insider
  holdings this understates true crowding.
- Dealer positioning is an assumption, not an observation. Nobody outside the
  market makers knows their real book.

---

## Not investment advice

This is a research tool. It shows you what the options market is charging for
each outcome and where positioning looks stretched. It does not know the
future, and neither does the market. Options can and regularly do expire
worthless.
