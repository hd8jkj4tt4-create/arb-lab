'use strict';

/* ------------------------------------------------------------------ state */
const S = {
  data: null,
  bankroll: 1000,
  threshold: 0.985,
  league: 'all',
  search: '',
  notify: localStorage.getItem('arblab.notify') === '1',
  alertKeys: new Set(),
  lastAlertId: 0,
  booted: false,
  refreshing: false,
  error: null,
  byId: new Map(),
  predictions: null,
  predComp: 'all',
  predLoading: false,
  predError: null,
  predRetry: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------------------------------------------------------------- helpers */
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const round2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100;
const money = (n) => '€' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (n, digits = 2) => (n >= 0 ? '+' : '−') + Math.abs(n * 100).toFixed(digits) + '%';
const plainPct = (n, digits = 2) => (n * 100).toFixed(digits) + '%';
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

function when(iso) {
  if (!iso) return 'kickoff TBD';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'kickoff TBD';
  return d.toLocaleString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function timeOnly(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function countdown(iso) {
  if (!iso) return '—';
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return '—';
  const seconds = Math.max(0, Math.round((target - Date.now()) / 1000));
  if (seconds === 0) return 'scanning…';
  if (seconds >= 3600) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${String(m).padStart(2, '0')}m`;
  }
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function every(seconds) {
  const total = Number(seconds) || 0;
  if (total >= 3600) {
    const h = total / 3600;
    return `every ${Number.isInteger(h) ? h : h.toFixed(1)} h`;
  }
  return `every ${Math.max(1, Math.round(total / 60))} min`;
}

function stakeMath(opp, bankroll) {
  const odds = opp.legs.map((l) => l.odds);
  const s = opp.s;
  if (!(s > 0) || !(bankroll > 0) || odds.length < 2) return null;
  const raw = odds.map((o) => bankroll * (1 / o) / s);
  let scale = 1;
  let capped = false;
  opp.legs.forEach((leg, i) => {
    if (leg.max_stake && raw[i] > 0) {
      const k = leg.max_stake / raw[i];
      if (k < scale) { scale = k; capped = true; }
    }
  });
  const stakes = raw.map((x) => Math.floor(x * scale * 100 + 1e-9) / 100);
  const total = round2(stakes.reduce((a, b) => a + b, 0));
  const payouts = stakes.map((x, i) => x * odds[i]);
  const minPay = Math.min(...payouts);
  return { stakes, payouts, total, minPay, profit: minPay - total, capped };
}

function api(path, options) {
  return fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}))
    .then(async (res) => {
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      return body;
    });
}

/* --------------------------------------------------------------- render */
function applyBanners(d) {
  const demo = $('#demoBanner');
  demo.classList.toggle('hidden', !d.demo);

  const source = $('#sourceNote');
  if (d.provider === 'theoddsapi') {
    source.innerHTML = '<b>Live EU odds.</b> The Odds API does not include Sisal or Snai, and not every bookmaker shown is ADM-licensed in Italy. Confirm availability before staking.';
    source.classList.remove('hidden');
  } else if (d.provider === 'oddsapiio') {
    source.innerHTML = '<b>Live feed.</b> Confirm your odds-api.io plan includes every bookmaker shown and that each is ADM-licensed in Italy.';
    source.classList.remove('hidden');
  } else {
    source.classList.add('hidden');
  }

  const err = $('#errorBanner');
  const message = S.error || d.error;
  if (message) {
    err.textContent = `Scanner error: ${message}`;
    err.classList.remove('hidden');
  } else {
    err.classList.add('hidden');
  }
  $('#live').style.opacity = message ? '.35' : '1';
}

function renderStats(d) {
  const stats = d.stats || {};
  $('#statEvents').textContent = stats.events ?? '—';
  $('#statArbs').textContent = stats.arbs ?? '—';
  $('#statArbs').className = (stats.arbs || 0) > 0 ? 'pos' : '';
  $('#arbsHint').textContent = stats.rounding
    ? `${stats.rounding} lost to rounding`
    : 'S < 1.000';
  const best = stats.best_margin;
  $('#statBest').textContent = best == null ? '—' : pct(best);
  $('#statBest').className = best == null ? '' : (best >= 0 ? 'pos' : 'neg');
  $('#statNext').textContent = countdown(d.next_run);
  $('#pollLabel').textContent = every(d.poll_seconds || 300);
  const q = d.quota || {};
  if (q.remaining == null) {
    $('#statQuota').textContent = '—';
    $('#quotaHint').textContent = d.demo ? 'demo provider' : 'not reported';
  } else {
    $('#statQuota').textContent = String(q.remaining);
    $('#statQuota').className = Number(q.remaining) < 50 ? 'neg' : Number(q.remaining) < 150 ? '' : 'pos';
    $('#quotaHint').textContent = 'API requests left';
  }
  $('#lastUpdate').textContent = d.generated_at ? `updated ${timeOnly(d.generated_at)}` : 'waiting…';
  $('#provider').textContent = d.provider_label || d.provider || '—';
}

function renderLeagueChips(items) {
  const counts = new Map();
  for (const opp of items) {
    const key = opp.league || 'Other';
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const leagues = Array.from(counts.keys()).sort();
  const chips = [`<button class="lchip ${S.league === 'all' ? 'on' : ''}" data-league="all">All <small>${items.length}</small></button>`];
  for (const league of leagues) {
    chips.push(
      `<button class="lchip ${S.league === league ? 'on' : ''}" data-league="${esc(league)}">${esc(league)} <small>${counts.get(league)}</small></button>`
    );
  }
  $('#leagueChips').innerHTML = chips.join('');
}

function matchFilter(opp) {
  if (S.league !== 'all' && opp.league !== S.league) return false;
  if (!S.search) return true;
  const hay = `${opp.home} ${opp.away} ${opp.league} ${opp.market}`.toLowerCase();
  return hay.includes(S.search);
}

function statusOf(opp) {
  if (opp.s < 1 && opp.profit > 0 && opp.s <= S.threshold) return { cls: 'arb', pill: 'green', label: 'ARB · TAKE' };
  if (opp.s < 1 && opp.profit > 0) return { cls: 'thin', pill: 'amber', label: 'THIN · BUFFER' };
  if (opp.s <= 1.02) return { cls: 'near', pill: 'blue', label: 'NEAR' };
  return { cls: '', pill: '', label: 'NO ARB' };
}

function oddsRow(opp, math) {
  const two = opp.legs.length === 2;
  const bestIndex = math && opp.best_pick ? opp.best_pick.index : -1;
  return `<div class="odds-row ${two ? 'two' : ''}">` + opp.legs.map((leg, i) => `
    <div class="odd ${i === bestIndex ? 'best' : ''}">
      <span class="code">Pick ${esc(leg.code)}${i === bestIndex ? '<span class="best-tag">Best</span>' : ''}</span>
      <span class="pick-label" title="${esc(leg.label)}">${esc(leg.label)}</span>
      <span class="price">${leg.odds.toFixed(2)}</span>
      <span class="book" title="${esc(leg.book)}">${esc(leg.book)}</span>
      ${math ? `<span class="stake" title="Stake for this pick"><span>Stake</span><b>${money(math.stakes[i])}</b></span>
      <span class="win" title="Return if this pick wins"><span>Pays</span><b>${money(math.payouts[i])}</b></span>` : ''}
    </div>`).join('') + '</div>';
}

function legsTable(opp, math) {
  return `<table class="legs">
    <thead><tr><th>Pick</th><th>Book</th><th>Odds</th><th>Stake</th><th>Return if wins</th></tr></thead>
    <tbody>${opp.legs.map((leg, i) => `
      <tr>
        <td><b>${esc(leg.code)}</b> <span class="muted">${esc(leg.label)}</span></td>
        <td>${leg.href ? `<a href="${esc(leg.href)}" target="_blank" rel="noopener noreferrer">${esc(leg.book)}</a>` : esc(leg.book)}</td>
        <td>${leg.odds.toFixed(2)}</td>
        <td><b>${money(math.stakes[i])}</b></td>
        <td>${money(math.payouts[i])}</td>
      </tr>`).join('')}
    </tbody>
  </table>`;
}

function card(opp) {
  const status = statusOf(opp);
  const math = opp.s < 1 ? stakeMath(opp, S.bankroll) : null;
  const margin = opp.margin;
  const summary = math
    ? `Stake split · ${money(math.total)} → ${math.profit >= 0 ? '+' : '−'}${money(Math.abs(math.profit))}`
    : `Needs S ≤ ${S.threshold.toFixed(3)} — now ${opp.s.toFixed(4)}`;
  const warn = (opp.warnings || []).filter(Boolean);
  const copyKey = `${opp.id}::${opp.market}`;
  const picks = opp.legs.map((leg) => leg.code).join(' + ');
  const backHint = math
    ? `<div class="back-hint">Back ${opp.legs.length === 2 ? 'both' : 'all 3'} outcomes — <b>${esc(picks)}</b> — to lock the return.</div>`
    : '';
  const best = math ? opp.best_pick : null;
  const bestLine = best ? `<div class="best-line" title="Highest market-implied chance. A single pick is not risk-free — the arb needs all outcomes.">
    <span class="best-label">Best single</span>
    <span class="best-pick"><b>Pick ${esc(best.code)} · ${esc(best.label)}</b> @ ${best.odds.toFixed(2)} · ${esc(best.book)}</span>
    <span class="best-prob">${plainPct(best.implied_probability, 0)} implied</span>
  </div>` : '';

  return `<article class="card ${status.cls}" data-key="${esc(copyKey)}">
    <div class="card-top">
      <div>
        <div class="league">${esc(opp.league)}</div>
        <div class="kick">${esc(when(opp.commence))}${opp.market && opp.market !== '1X2' ? ` · ${esc(opp.market)}` : ''}</div>
      </div>
      <div class="margin">
        <b class="${margin >= 0 ? 'pos' : 'neg'}">${pct(margin)}</b>
        <small>${opp.s < 1 ? 'locked' : 'no edge'}</small>
      </div>
    </div>
    <div class="match">${esc(opp.home)}<span>–</span>${esc(opp.away)}</div>
    ${backHint}
    ${bestLine}
    ${oddsRow(opp, math)}
    <div class="card-foot">
      <div class="s-line">S <b>${opp.s.toFixed(5)}</b>${math ? ` · worst return ${money(math.minPay)} · <b class="${math.profit >= 0 ? 'pos' : 'neg'}">${math.profit >= 0 ? '+' : '−'}${money(Math.abs(math.profit))}</b>` : ''}</div>
      <span class="pill ${status.pill}">${status.label}</span>
    </div>
    <details class="detail">
      <summary>${summary}</summary>
      <div class="detail-body">
        ${math ? legsTable(opp, math) : ''}
        <div class="detail-total">
          ${math ? `<span>Total <b>${money(math.total)}</b></span>
          <span>Min payout <b>${money(math.minPay)}</b></span>
          <span>Profit <b class="${math.profit >= 0 ? 'pos' : 'neg'}">${math.profit >= 0 ? '+' : '−'}${money(Math.abs(math.profit))}</b></span>
          ${math.capped ? '<span class="pill amber">capped by max stake</span>' : ''}`
          : `<span>S = <b>${opp.s.toFixed(5)}</b></span><span>Buffer needed: <b>${S.threshold.toFixed(3)}</b></span>`}
        </div>
        ${warn.length ? `<div class="warns">${warn.map((w) => `<div class="warn">${esc(w)}</div>`).join('')}</div>` : ''}
        ${math ? `<button class="copy" type="button" data-copy="${esc(copyKey)}">Copy ticket</button>` : ''}
      </div>
    </details>
  </article>`;
}

function renderCards(d) {
  const all = d.opportunities || [];
  S.byId = new Map();
  all.forEach((opp) => S.byId.set(`${opp.id}::${opp.market}`, opp));

  const arbs = all
    .filter((o) => o.s < 1 && o.profit > 0)
    .filter(matchFilter)
    .sort((a, b) => a.s - b.s);
  const near = all
    .filter((o) => o.s >= 1 && o.s <= 1.02)
    .filter(matchFilter)
    .sort((a, b) => a.s - b.s)
    .slice(0, 6);

  renderLeagueChips(all.filter((o) => o.s < 1.02 && (o.s >= 1 || o.profit > 0)));

  $('#cards').innerHTML = arbs.map(card).join('');
  $('#countArbs').textContent = `${arbs.length} shown${all.filter((o) => o.s < 1 && o.profit > 0).length !== arbs.length ? ` of ${all.filter((o) => o.s < 1 && o.profit > 0).length}` : ''}`;
  const empty = $('#empty');
  const rounding = (d.stats && d.stats.rounding) || 0;
  empty.querySelector('b').textContent = rounding > 0 ? 'No profitable arbitrage right now.' : 'No arbitrage right now.';
  empty.querySelector('span').textContent = rounding > 0
    ? `${rounding} price gap${rounding === 1 ? '' : 's'} disappeared after cent rounding at the current bankroll. Increase the bankroll or wait for a wider gap.`
    : 'The market is efficient at the moment. The scanner keeps polling; S must drop below 1.000 across different books.';
  empty.classList.toggle('hidden', arbs.length > 0);
  $('#loading').classList.add('hidden');

  const nearSection = $('#nearSection');
  if (!near.length) {
    nearSection.classList.add('hidden');
  } else {
    nearSection.classList.remove('hidden');
    $('#nearCards').innerHTML = near.map(card).join('');
  }
}

function renderHistory(d) {
  const alerts = d.alerts || [];
  if (!alerts.length) {
    $('#history').innerHTML = '<div class="h-empty">No alerts yet. An alert fires when a new arb (S ≤ threshold) appears.</div>';
    return;
  }
  $('#history').innerHTML = alerts.map((a) => {
    const legs = (a.legs || []).map((l) => `${l.code} ${l.book} ${Number(l.odds).toFixed(2)}`).join(' · ');
    return `<div class="hitem">
      <time>${esc(timeOnly(a.ts))}</time>
      <div class="h-match">${esc(a.home)} – ${esc(a.away)}
        <small>${esc(a.league)} · ${esc(legs)}</small>
      </div>
      <div class="h-margin">${pct(a.margin)}</div>
    </div>`;
  }).join('');
}

/* ---------------------------------------------------------- predictions */
function formBadges(form) {
  if (!form) return '<span class="form-empty">—</span>';
  return form.split('').map((result) => {
    const cls = result === 'W' ? 'w' : result === 'D' ? 'd' : 'l';
    return `<i class="f ${cls}">${esc(result)}</i>`;
  }).join('');
}

function probClass(code) {
  if (code === '1') return 'one';
  if (code === 'X') return 'draw';
  return 'two';
}

function predRow(fixture) {
  const outcomes = fixture.outcomes || [];
  const bar = outcomes.map((outcome) => {
    const width = clamp(outcome.probability * 100, 0, 100);
    return `<span class="pseg ${probClass(outcome.code)}" style="width:${width.toFixed(2)}%" title="${esc(outcome.label)} ${plainPct(outcome.probability, 1)}"></span>`;
  }).join('');
  const labels = outcomes.map((outcome) => (
    `<span class="plabel"><i class="pdot ${probClass(outcome.code)}"></i>${esc(outcome.code)} <b>${plainPct(outcome.probability, 0)}</b></span>`
  )).join('');
  const detail = fixture.expected_goals
    ? `xG ${fixture.expected_goals[0].toFixed(2)}–${fixture.expected_goals[1].toFixed(2)}`
    : `proj. margin ${fixture.projected_margin > 0 ? '+' : ''}${Number(fixture.projected_margin).toFixed(1)}`;
  const likely = fixture.likely_score
    ? ` · likely ${fixture.likely_score.score} (${plainPct(fixture.likely_score.probability, 0)})`
    : '';
  const sample = fixture.sample ? `${fixture.sample.home}/${fixture.sample.away} matches` : '';
  const projected = fixture.projected || {};
  const corners = projected.corners && projected.corners.total != null
    ? `corners ~${Number(projected.corners.total).toFixed(1)}`
    : '';
  const fouls = projected.fouls && projected.fouls.total != null
    ? `fouls ~${Number(projected.fouls.total).toFixed(1)}`
    : '';
  const markets = [corners, fouls].filter(Boolean).join(' · ');
  const recent = fixture.recent || {};
  const recentList = (rows) => (rows && rows.length
    ? `<ul>${rows.map((row) => `<li>${esc(row)}</li>`).join('')}</ul>`
    : '<p class="recent-empty">No finished matches with stats.</p>');
  const formTitle = (label, rows) => rows && rows.length
    ? `${label} recent results (newest first):\n${rows.join('\n')}`
    : `${label} recent results: no finished matches in the training window`;
  return `<div class="pred-row">
    <div class="pred-when">${esc(when(fixture.commence))}</div>
    <div class="pred-teams">
      <div class="pred-match"><a href="${esc(fixture.espn_url || '#')}" target="_blank" rel="noopener noreferrer">${esc(fixture.home)} <span>vs</span> ${esc(fixture.away)}</a></div>
      <div class="pred-sub">${esc(detail)}${esc(likely)}${sample ? ` · ${esc(sample)}` : ''}</div>
      ${markets ? `<div class="pred-markets">${esc(markets)}</div>` : ''}
    </div>
    <div class="pred-probs">
      <div class="prob-bar">${bar}</div>
      <div class="prob-labels">${labels}</div>
    </div>
    <div class="pred-form">
      <span title="${esc(formTitle(fixture.home, recent.home))}"><em>H</em>${formBadges(fixture.form && fixture.form.home)}</span>
      <span title="${esc(formTitle(fixture.away, recent.away))}"><em>A</em>${formBadges(fixture.form && fixture.form.away)}</span>
    </div>
    <details class="pred-recent">
      <summary>Exact results, corners &amp; fouls</summary>
      <div class="pred-recent-grid">
        <div><b>${esc(fixture.home)}</b>${recentList(recent.home)}</div>
        <div><b>${esc(fixture.away)}</b>${recentList(recent.away)}</div>
      </div>
    </details>
  </div>`;
}

function predGroup(comp) {
  const validation = comp.validation;
  const holdout = validation
    ? `holdout ${plainPct(validation.accuracy, 0)} · ${validation.log_loss.toFixed(3)} log loss · ${validation.matches} matches`
    : 'holdout not available';
  const through = comp.trained_through ? ` through ${comp.trained_through}` : '';
  const meta = `${comp.model} · ${comp.trained_matches} finished matches${through} · goals counted`;
  const fixtures = comp.fixtures || [];
  const warnings = (comp.warnings || []).filter(Boolean);
  const empty = `<div class="pred-empty">${comp.error ? esc(comp.error) : 'No upcoming fixtures published yet.'}</div>`;
  return `<div class="pred-group${comp.stale ? ' stale' : ''}">
    <div class="pred-group-head">
      <div>
        <h3>${esc(comp.name)}${comp.stale ? '<span class="pred-badge amber">cached</span>' : ''}</h3>
        <p>${esc(meta)}</p>
      </div>
      <span class="pred-badge">${esc(holdout)}</span>
    </div>
    ${fixtures.length ? `<div class="pred-list">${fixtures.map(predRow).join('')}</div>` : empty}
    ${warnings.length ? `<div class="pred-warns">${warnings.map((warning) => `<span>${esc(warning)}</span>`).join('')}</div>` : ''}
  </div>`;
}

function renderPredictions() {
  const data = S.predictions;
  const groups = $('#predGroups');
  const empty = $('#predEmpty');
  const updated = $('#predUpdated');

  if (S.predLoading && (!data || data.loading)) {
    groups.innerHTML = '';
    $('#predChips').innerHTML = '';
    empty.classList.remove('hidden');
    empty.querySelector('b').textContent = 'Loading the win model…';
    empty.querySelector('span').textContent = 'Downloading previous results from ESPN. This can take a few seconds on first start.';
    updated.textContent = 'loading model…';
    return;
  }

  if (S.predError || !data || data.loading) {
    groups.innerHTML = '';
    $('#predChips').innerHTML = '';
    empty.classList.remove('hidden');
    empty.querySelector('b').textContent = S.predError ? 'Win model unavailable.' : 'Win model is loading…';
    empty.querySelector('span').textContent = S.predError || 'The first ESPN download is still running. The page will update automatically.';
    updated.textContent = 'not ready';
    if (data && data.loading && !S.predRetry) {
      S.predRetry = setTimeout(() => {
        S.predRetry = null;
        loadPredictions(false);
      }, 5000);
    }
    return;
  }

  const competitions = data.competitions || [];
  const shown = S.predComp === 'all'
    ? competitions
    : competitions.filter((comp) => comp.key === S.predComp);

  const chips = [`<button class="lchip ${S.predComp === 'all' ? 'on' : ''}" data-pred="all">All <small>${competitions.length}</small></button>`];
  competitions.forEach((comp) => {
    chips.push(
      `<button class="lchip ${S.predComp === comp.key ? 'on' : ''}" data-pred="${esc(comp.key)}">${esc(comp.name)} <small>${(comp.fixtures || []).length}</small></button>`
    );
  });
  $('#predChips').innerHTML = chips.join('');
  groups.innerHTML = shown.map(predGroup).join('');

  const hasFixtures = shown.some((comp) => (comp.fixtures || []).length);
  empty.classList.toggle('hidden', hasFixtures);
  if (!hasFixtures) {
    empty.querySelector('b').textContent = 'No upcoming fixtures.';
    empty.querySelector('span').textContent = 'The model has results, but the feed has not published the next round yet.';
  }

  const generated = data.generated_at ? timeOnly(data.generated_at) : '—';
  updated.textContent = `updated ${generated}${data.stale ? ' · stale' : ''}`;
}

async function loadPredictions(force = false) {
  if (force) {
    try {
      await api('/api/predictions/refresh', { method: 'POST' });
    } catch (err) {
      S.predError = err.message;
    }
  }
  S.predLoading = true;
  renderPredictions();
  try {
    S.predictions = await api('/api/predictions');
    S.predError = null;
    if (S.predRetry) {
      clearTimeout(S.predRetry);
      S.predRetry = null;
    }
  } catch (err) {
    S.predError = err.message;
  } finally {
    S.predLoading = false;
    renderPredictions();
  }
}

function render(d) {
  renderStats(d);
  renderCards(d);
  renderHistory(d);
  applyBanners(d);
}

/* ----------------------------------------------------------- settings */
let saveTimer = null;
function queueSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ bankroll: S.bankroll, threshold: S.threshold }),
    }).catch(() => {});
  }, 600);
}

function bindControls() {
  const bankroll = $('#bankroll');
  const threshold = $('#threshold');

  bankroll.addEventListener('input', () => {
    const v = parseFloat(bankroll.value);
    S.bankroll = Number.isFinite(v) && v > 0 ? v : 1000;
    if (S.data) render(S.data);
    queueSave();
  });

  threshold.addEventListener('input', () => {
    const v = parseFloat(threshold.value);
    if (Number.isFinite(v)) {
      S.threshold = clamp(v, 0.90, 1.20);
      $$('#presets .chip').forEach((c) => c.classList.remove('on'));
      if (S.data) render(S.data);
      queueSave();
    }
  });

  $('#presets').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    S.threshold = parseFloat(chip.dataset.s);
    threshold.value = S.threshold;
    $$('#presets .chip').forEach((c) => c.classList.toggle('on', c === chip));
    if (S.data) render(S.data);
    queueSave();
  });

  $('#search').addEventListener('input', (event) => {
    S.search = event.target.value.trim().toLowerCase();
    if (S.data) renderCards(S.data);
  });

  $('#leagueChips').addEventListener('click', (event) => {
    const chip = event.target.closest('.lchip');
    if (!chip) return;
    S.league = chip.dataset.league || 'all';
    if (S.data) renderCards(S.data);
  });

  $('#predChips').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-pred]');
    if (!chip) return;
    S.predComp = chip.dataset.pred || 'all';
    renderPredictions();
  });

  $('#predRefresh').addEventListener('click', async () => {
    const button = $('#predRefresh');
    button.disabled = true;
    button.textContent = 'Refreshing…';
    await loadPredictions(true);
    setTimeout(() => loadPredictions(false), 4000);
    setTimeout(() => loadPredictions(false), 15000);
    setTimeout(() => {
      button.disabled = false;
      button.textContent = 'Refresh model';
    }, 16000);
  });

  $('#notify').checked = S.notify;
  $('#notify').addEventListener('change', async (event) => {
    S.notify = event.target.checked;
    localStorage.setItem('arblab.notify', S.notify ? '1' : '0');
    if (S.notify && 'Notification' in window && Notification.permission === 'default') {
      await Notification.requestPermission();
    }
    if (S.notify && 'Notification' in window && Notification.permission === 'denied') {
      event.target.checked = false;
      S.notify = false;
      localStorage.setItem('arblab.notify', '0');
    }
  });

  $('#refresh').addEventListener('click', refreshNow);
}

async function refreshNow() {
  if (S.refreshing) return;
  S.refreshing = true;
  const btn = $('#refresh');
  const before = S.data && S.data.generated_at;
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    await api('/api/refresh', { method: 'POST' });
    for (let i = 0; i < 20; i += 1) {
      await new Promise((r) => setTimeout(r, 800));
      const d = await api('/api/state');
      if (d.generated_at && d.generated_at !== before) {
        applyData(d);
        break;
      }
    }
  } catch (err) {
    S.error = err.message;
  } finally {
    S.refreshing = false;
    btn.disabled = false;
    btn.textContent = 'Refresh';
  }
}

/* ------------------------------------------------------------- polling */
function notifyNew(d) {
  const fresh = d.new_alerts || [];
  if (!fresh.length || !S.notify) return;
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  fresh.slice(0, 3).forEach((a) => {
    const key = `${a.id}::${a.market}`;
    if (S.alertKeys.has(key)) return;
    S.alertKeys.add(key);
    const body = `${a.league} · S ${Number(a.s).toFixed(4)} · ${pct(a.margin)}`;
    try {
      new Notification(`${a.home} – ${a.away}`, { body, tag: key });
    } catch (err) { /* ignore */ }
  });
}

function applyData(d) {
  if (d.loading) {
    $('#loading').classList.remove('hidden');
    $('#demoBanner').classList.toggle('hidden', !d.demo);
    $('#provider').textContent = d.provider_label || '—';
    if (d.settings) {
      S.bankroll = d.settings.bankroll;
      S.threshold = d.settings.threshold;
      $('#bankroll').value = S.bankroll;
      $('#threshold').value = S.threshold;
    }
    if (d.error) {
      S.error = d.error;
      const err = $('#errorBanner');
      err.textContent = `Scanner error: ${d.error}`;
      err.classList.remove('hidden');
    }
    return;
  }

  S.data = d;
  S.error = null;

  if (!S.booted) {
    S.booted = true;
    const settings = d.settings || {};
    S.bankroll = Number(settings.bankroll) || S.bankroll;
    S.threshold = Number(settings.threshold) || S.threshold;
    $('#bankroll').value = S.bankroll;
    $('#threshold').value = S.threshold;
    $$('#presets .chip').forEach((c) => c.classList.toggle('on', Math.abs(parseFloat(c.dataset.s) - S.threshold) < 1e-9));
  }

  notifyNew(d);
  render(d);
}

async function poll() {
  try {
    const d = await api('/api/state');
    applyData(d);
  } catch (err) {
    S.error = err.message;
    const errBox = $('#errorBanner');
    errBox.textContent = `Cannot reach the scanner: ${err.message}`;
    errBox.classList.remove('hidden');
  }
}

function tick() {
  if (S.data && S.data.next_run) {
    $('#statNext').textContent = countdown(S.data.next_run);
  }
}

/* ---------------------------------------------------------------- events */
document.addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-copy]');
  if (!btn) return;
  const opp = S.byId.get(btn.dataset.copy);
  if (!opp) return;
  const math = stakeMath(opp, S.bankroll);
  if (!math) return;
  const lines = [
    `Arb Lab — ${opp.home} – ${opp.away} (${opp.league})`,
    `${opp.market} · S ${opp.s.toFixed(5)} · margin ${pct(opp.margin)}`,
  ];
  opp.legs.forEach((leg, i) => lines.push(`${leg.code} ${leg.book} @ ${leg.odds.toFixed(2)} → ${money(math.stakes[i])}`));
  if (opp.best_pick) {
    const best = opp.best_pick;
    lines.push(`Best single: ${best.code} ${best.label} @ ${best.odds.toFixed(2)} (${best.book}) — ${plainPct(best.implied_probability, 0)} implied, not risk-free`);
  }
  lines.push(`Total ${money(math.total)} · worst return ${money(math.minPay)} · profit ${math.profit >= 0 ? '+' : '−'}${money(Math.abs(math.profit))}`);
  try {
    await navigator.clipboard.writeText(lines.join('\n'));
    const old = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = old; }, 1300);
  } catch (err) {
    btn.textContent = 'Copy failed';
  }
});

/* ------------------------------------------------------------------ boot */
bindControls();
poll();
loadPredictions();
setInterval(poll, 10000);
setInterval(() => loadPredictions(false), 60000);
setInterval(tick, 1000);
