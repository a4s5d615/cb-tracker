'use strict';
// ═══════════════════════════════════════════════════════════
// CB Tracker — Frontend Application
// ═══════════════════════════════════════════════════════════

const API = '/api';
let allRows       = [];      // full premium/today response
let overboughtMap = {};      // cb_code → analysis result (loaded on demand)
let sortCol       = 'cb_price';
let sortAsc       = false;   // descending by default for price
let charts        = {};      // active Chart.js instances
let currentCB     = null;    // cb_code of open detail modal
let scanPollTimer = null;    // polling interval for discovery status

// ── API helpers ─────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  const r = await fetch(API + path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) {
    const msg = await r.text().catch(() => r.statusText);
    throw new Error(`HTTP ${r.status}: ${msg}`);
  }
  return r.json();
}

const get  = path         => apiFetch(path);
const post = (path, body) => apiFetch(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });
const put  = (path, body) => apiFetch(path, { method: 'PUT',  body: JSON.stringify(body) });
const del  = path         => apiFetch(path, { method: 'DELETE' });

// ── Format helpers ───────────────────────────────────────────

function fmtPct(v, showSign = true) {
  if (v === null || v === undefined) return '—';
  const s = showSign ? (v >= 0 ? '+' : '') : '';
  return `${s}${v.toFixed(2)}%`;
}

function fmtNum(v, dec = 2) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(dec);
}

function fmtChg(v) {
  if (v === null || v === undefined) return '<span class="text-slate-600">—</span>';
  const cls = v >= 0 ? 'text-red-400' : 'text-green-400';
  const s   = v >= 0 ? '+' : '';
  return `<span class="${cls}">${s}${v.toFixed(2)}%</span>`;
}

function fmtDate(d) {
  if (!d) return '—';
  // Convert "2028/06/20" or "2028-06-20" to short form
  return d.replace(/\//g, '/').substring(0, 10);
}

function daysToMaturity(maturityDate) {
  if (!maturityDate) return null;
  const d = new Date(maturityDate.replace(/\//g, '-'));
  if (isNaN(d)) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((d - today) / (1000 * 60 * 60 * 24));
}

// ── Conversion progress cell ─────────────────────────────────

function convProgressCell(r) {
  const pct = r.outstanding_pct;   // 0-100, percentage of bonds still outstanding
  const lots = r.outstanding_lots; // integer, remaining lots
  if (pct == null) return '<span class="text-slate-600 text-xs">—</span>';

  const converted = 100 - pct;  // how much has been converted
  const lotsStr = lots != null ? lots.toLocaleString() + '張' : '';

  let colorClass, label;
  if (converted < 1) {
    colorClass = 'text-slate-500'; label = '未轉換';
  } else if (converted < 30) {
    colorClass = 'text-blue-400'; label = `已轉 ${converted.toFixed(1)}%`;
  } else if (converted < 70) {
    colorClass = 'text-amber-400'; label = `已轉 ${converted.toFixed(1)}%`;
  } else {
    colorClass = 'text-red-400'; label = `已轉 ${converted.toFixed(1)}%`;
  }

  return `<span class="font-mono text-xs ${colorClass}" title="流通餘額: ${lotsStr}&#10;已轉換: ${converted.toFixed(1)}%&#10;餘額比例: ${pct.toFixed(1)}%">
    ${label}<br><span class="text-slate-500">${lotsStr}</span>
  </span>`;
}

// ── Premium/par colour logic ─────────────────────────────────

// For parity premium (when conversion_price is known)
function parityPremiumClass(pct) {
  if (pct === null || pct === undefined) return '';
  if (pct < -20)  return 'badge-deep-discount';
  if (pct < -10)  return 'badge-discount';
  if (pct <  -3)  return 'badge-slight-disc';
  if (pct <   3)  return 'badge-flat';
  if (pct <  10)  return 'badge-slight-prem';
  if (pct <  20)  return 'badge-premium';
  return 'badge-deep-premium';
}

// For par premium (CB vs face value 100, when no conversion price)
function parPremiumClass(pct) {
  if (pct === null || pct === undefined) return 'badge-par-unknown';
  if (pct < -5)  return 'badge-par-disc';
  if (pct <  2)  return 'badge-par-flat';
  if (pct < 10)  return 'badge-par-prem';
  return 'badge-par-high';
}

function rowBgClass(r) {
  if (r.premium_pct !== null && r.premium_pct !== undefined) {
    const pct = r.premium_pct;
    if (pct < -20) return 'row-deep-discount';
    if (pct < -10) return 'row-discount';
    if (pct <  -3) return 'row-slight-disc';
    if (pct <   3) return 'row-flat';
    if (pct <  10) return 'row-slight-prem';
    if (pct <  20) return 'row-premium';
    return 'row-deep-premium';
  }
  return '';
}

// ── Filter and sort ──────────────────────────────────────────

function applyFilters(rows) {
  const search  = document.getElementById('search-input').value.trim().toLowerCase();
  const market  = document.getElementById('filter-market').value;
  const premium = document.getElementById('filter-premium').value;

  return rows.filter(r => {
    if (search) {
      const hay = `${r.cb_code} ${r.cb_name||''} ${r.stock_code} ${r.stock_name||''}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    if (market !== 'all' && r.stock_market !== market) return false;

    const p     = r.premium_pct;
    const parP  = r.par_premium_pct;
    const price = r.cb_price;

    switch (premium) {
      case 'below_par':  if (price === null || price >= 100) return false; break;
      case 'near_par':   if (price === null || price < 98 || price > 102) return false; break;
      case 'above_par':  if (price === null || price <= 100) return false; break;
      case 'deep_discount': if (p === null || p >= -10) return false; break;
      case 'discount':   if (p === null || p >= 0) return false; break;
      case 'premium':    if (p === null || p <  0) return false; break;
      case 'deep_premium': if (p === null || p <= 20) return false; break;
      case 'quality': if (p === null || p <= 10 || r.parity === null || r.parity <= 80) return false; break;
      case 'elite': {
        const converted = r.outstanding_pct != null ? (100 - r.outstanding_pct) : null;
        if (p === null || p <= 15) return false;
        if (r.parity === null || r.parity <= 85) return false;
        if (converted === null || converted >= 50) return false;
        break;
      }
      case 'overbought':
        if (!overboughtMap[r.cb_code]) return false;
        break;
    }
    return true;
  });
}

function sortRows(rows) {
  return [...rows].sort((a, b) => {
    let va = a[sortCol], vb = b[sortCol];

    // For premium_pct, use par_premium_pct as fallback
    if (sortCol === 'premium_pct') {
      va = a.premium_pct !== null ? a.premium_pct : a.par_premium_pct;
      vb = b.premium_pct !== null ? b.premium_pct : b.par_premium_pct;
    }

    if (va === null || va === undefined) va = sortAsc ? Infinity : -Infinity;
    if (vb === null || vb === undefined) vb = sortAsc ? Infinity : -Infinity;
    if (typeof va === 'string') return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortAsc ? va - vb : vb - va;
  });
}

// ── Render table ─────────────────────────────────────────────

function renderTable() {
  const filtered = applyFilters(allRows);
  const sorted   = sortRows(filtered);
  const tbody    = document.getElementById('cb-table-body');
  const empty    = document.getElementById('empty-state');
  const countEl  = document.getElementById('table-count');

  if (allRows.length === 0) {
    tbody.innerHTML = '';
    empty.classList.remove('hidden');
    empty.classList.add('flex');
    if (countEl) countEl.textContent = '';
    return;
  }
  empty.classList.add('hidden');
  empty.classList.remove('flex');

  if (countEl) countEl.textContent = `顯示 ${sorted.length} / ${allRows.length} 筆`;

  if (sorted.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12" class="text-center py-12 text-slate-500">找不到符合條件的可轉債</td></tr>`;
    return;
  }

  tbody.innerHTML = sorted.map(r => {
    const rCls  = rowBgClass(r);
    const hasParity = r.premium_pct !== null && r.premium_pct !== undefined;

    // Parity (轉換價值) — computed from latest data
    const parity = r.parity != null ? r.parity
      : (r.stock_price && r.conversion_price && r.conversion_price > 0
          ? r.stock_price * (100.0 / r.conversion_price) : null);

    const parityCell = parity != null
      ? `<span class="font-mono text-slate-200 text-xs">${fmtNum(parity)}</span>`
      : `<span class="text-slate-600 text-xs">—</span>`;

    // 轉換溢價率 column
    let premiumCell;
    if (hasParity) {
      const pct = r.premium_pct;
      const color = pct > 30 ? 'text-red-300'
                  : pct > 10 ? 'text-orange-300'
                  : pct >  0 ? 'text-yellow-300'
                  : pct > -5 ? 'text-green-300'
                  :            'text-green-400';
      const sign = pct >= 0 ? '+' : '';
      premiumCell = `<span class="font-mono font-semibold text-sm ${color}">${sign}${pct.toFixed(2)}%</span>`;
    } else {
      // No conversion price: show par premium with muted style
      const parP = r.par_premium_pct;
      if (parP != null) {
        premiumCell = `<span class="font-mono text-xs text-slate-500" title="未設轉換價，顯示CB現價相對面值100的溢折價，非轉換溢價率">
          ${parP >= 0 ? '+' : ''}${parP.toFixed(2)}%<sup class="text-amber-600 ml-0.5">?</sup></span>`;
      } else {
        premiumCell = '<span class="text-slate-600 text-xs">—</span>';
      }
    }

    // Maturity date with days remaining
    const days = daysToMaturity(r.maturity_date);
    const maturityCell = r.maturity_date
      ? `<span title="${r.maturity_date}">${fmtDate(r.maturity_date)}<br>
           <span class="text-xs ${days !== null && days < 180 ? 'text-yellow-400' : 'text-slate-500'}">${days !== null ? days + '天' : ''}</span>
         </span>`
      : '—';

    // Conversion price cell — click to quick-set without opening full modal
    const convCell = r.conversion_price
      ? `<span class="font-mono text-slate-200 text-xs">${Number(r.conversion_price).toFixed(2)}</span>`
      : `<button onclick="event.stopPropagation();quickSetConvPrice('${r.cb_code}',this)"
           class="text-xs text-amber-400 hover:text-amber-200 border border-amber-700/40 rounded px-1.5 py-0.5">設定</button>`;

    // 異常偏高標記
    const ob = overboughtMap[r.cb_code];
    const obBadge = ob
      ? `<span class="ml-1 text-[10px] bg-red-900 text-red-300 rounded px-1 py-0.5 align-middle"
             title="CB超漲 +${ob.cb_excess_pct}%&#10;股距高點 ${ob.stock_vs_high_pct}%&#10;股高日:${ob.stock_high_date}&#10;股高日CB價:${ob.cb_at_stock_high}">⚠CB偏高</span>`
      : '';

    return `<tr class="hover:bg-slate-800/30 cursor-pointer border-b border-slate-800/50 ${rCls}" onclick="openDetailModal('${r.cb_code}')">
      <td class="px-3 py-2 font-mono text-blue-300 text-sm cursor-pointer hover:text-blue-100 hover:underline"
          onclick="event.stopPropagation();openKlineModal('${r.cb_code}')"
          title="點擊查看K線圖">${r.cb_code}</td>
      <td class="px-3 py-2 max-w-[160px] truncate text-sm" title="${r.cb_name||''}">${r.cb_name || '—'}${obBadge}</td>
      <td class="px-3 py-2 text-sm">
        <span class="font-mono">${r.stock_code}</span>
        <span class="text-slate-400 text-xs ml-1">${r.stock_name || ''}</span>
      </td>
      <td class="px-3 py-2 text-right font-mono text-sm">${fmtNum(r.cb_price)}</td>
      <td class="px-3 py-2 text-right font-mono text-sm text-slate-300">${fmtNum(r.stock_price)}</td>
      <td class="px-3 py-2 text-right">${convCell}</td>
      <td class="px-3 py-2 text-right">${convProgressCell(r)}</td>
      <td class="px-3 py-2 text-right">${parityCell}</td>
      <td class="px-3 py-2 text-right">${premiumCell}</td>
      <td class="px-3 py-2 text-right text-xs">${fmtChg(r.cb_change_pct)}</td>
      <td class="px-3 py-2 text-right text-xs text-slate-400">${maturityCell}</td>
      <td class="px-3 py-2 text-right text-slate-500 text-xs">${r.cb_volume ? r.cb_volume.toLocaleString() : '—'}</td>
      <td class="px-3 py-2 text-center">
        <button onclick="event.stopPropagation();openDetailModal('${r.cb_code}')"
          class="text-xs text-blue-400 hover:text-blue-300 px-1">詳</button>
      </td>
    </tr>`;
  }).join('');

  // Update sort indicators
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('asc', 'desc');
    if (th.dataset.col === sortCol) th.classList.add(sortAsc ? 'asc' : 'desc');
  });
}

// ── Stats bar ────────────────────────────────────────────────

async function loadStats() {
  try {
    const s = await get('/stats');
    document.getElementById('stat-total').textContent      = s.total_active_cbs ?? '—';
    document.getElementById('stat-with-price').textContent = s.cbs_with_prices ?? '—';
    document.getElementById('stat-date').textContent       = s.trade_date ?? '—';

    // Compute below/above par from current allRows
    const belowPar  = allRows.filter(r => r.cb_price !== null && r.cb_price < 100).length;
    const abovePar  = allRows.filter(r => r.cb_price !== null && r.cb_price >= 100).length;
    document.getElementById('stat-below-par').textContent = belowPar || '—';
    document.getElementById('stat-above-par').textContent = abovePar || '—';
  } catch (e) {
    console.warn('Stats load error:', e);
  }
}

// ── Load main data ───────────────────────────────────────────

async function loadPremiumTable() {
  try {
    const r = await get('/premium/today');
    allRows = r.data || [];
    renderTable();
    const dateStr = r.date || '—';
    document.getElementById('last-update').textContent = `資料日期: ${dateStr}`;
    // Update par stats now that allRows is loaded
    const belowPar = allRows.filter(r => r.cb_price !== null && r.cb_price < 100).length;
    const abovePar = allRows.filter(r => r.cb_price !== null && r.cb_price >= 100).length;
    const bpEl = document.getElementById('stat-below-par');
    const apEl = document.getElementById('stat-above-par');
    if (bpEl) bpEl.textContent = belowPar || '—';
    if (apEl) apEl.textContent = abovePar || '—';
    const wpEl = document.getElementById('stat-with-price');
    if (wpEl) wpEl.textContent = allRows.length || '—';
  } catch (e) {
    console.error('Load error:', e);
    document.getElementById('cb-table-body').innerHTML =
      `<tr><td colspan="10" class="text-center py-12 text-red-400">載入失敗: ${e.message}</td></tr>`;
  }
}

// ── Refresh button ────────────────────────────────────────────

async function triggerRefresh() {
  const btn  = document.getElementById('btn-refresh');
  const icon = document.getElementById('refresh-icon');
  btn.disabled = true;
  icon.className = 'spin';
  icon.textContent = '↻';
  try {
    await post('/prices/fetch');
    await Promise.all([loadPremiumTable(), loadStats()]);
    icon.className = '';
    icon.textContent = '✓';
    setTimeout(() => { icon.textContent = '↻'; }, 2000);
  } catch (e) {
    alert('更新失敗: ' + e.message);
    icon.className = '';
    icon.textContent = '↻';
  } finally {
    btn.disabled = false;
  }
}

// ── Full market scan ─────────────────────────────────────────

async function triggerFullScan() {
  const btn = document.getElementById('btn-scan');
  btn.disabled = true;
  try {
    const r = await post('/discover');
    if (r.status === 'already_running') {
      alert('掃描已在進行中，請稍候');
      btn.disabled = false;
    } else {
      startScanPolling();
    }
  } catch (e) {
    alert('啟動掃描失敗: ' + e.message);
    btn.disabled = false;
  }
}

function startScanPolling() {
  const banner = document.getElementById('scan-banner');
  banner.classList.remove('hidden');
  if (scanPollTimer) clearInterval(scanPollTimer);

  scanPollTimer = setInterval(async () => {
    try {
      const s = await get('/discover/status');
      document.getElementById('scan-msg').textContent = s.message || '掃描中...';
      const p = Math.max(0, s.progress);
      document.getElementById('scan-bar').style.width = p + '%';
      document.getElementById('scan-pct').textContent = p + '%';

      if (!s.running) {
        clearInterval(scanPollTimer);
        scanPollTimer = null;
        document.getElementById('btn-scan').disabled = false;
        setTimeout(() => banner.classList.add('hidden'), 4000);
        await Promise.all([loadPremiumTable(), loadStats()]);
      }
    } catch (e) {
      console.warn('Poll error:', e);
    }
  }, 2000);
}

// ── Detail Modal ─────────────────────────────────────────────

function destroyCharts() {
  Object.values(charts).forEach(c => c && c.destroy());
  charts = {};
}

async function openDetailModal(cbCode) {
  currentCB = cbCode;
  destroyCharts();
  document.getElementById('detail-modal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';

  document.getElementById('modal-title').textContent    = cbCode;
  document.getElementById('modal-subtitle').textContent = '載入中...';
  document.getElementById('modal-cb-price').textContent  = '—';
  document.getElementById('modal-stock-price').textContent = '—';
  document.getElementById('modal-parity').textContent    = '—';
  document.getElementById('modal-premium').textContent   = '—';
  document.getElementById('modal-history-body').innerHTML = '';
  document.getElementById('modal-conv-status').textContent = '';

  try {
    const [detail, cbHistory, premHistory] = await Promise.all([
      get(`/cbs/${cbCode}`),
      get(`/prices/cb/${cbCode}?days=120`),
      get(`/premium/${cbCode}/history?days=120`),
    ]);

    document.getElementById('modal-title').textContent =
      `${detail.cb_code}　${detail.cb_name || ''}`;

    const maturity = detail.maturity_date ? `到期: ${detail.maturity_date}` : '';
    const days     = daysToMaturity(detail.maturity_date);
    const daysStr  = days !== null ? ` (剩${days}天)` : '';
    document.getElementById('modal-subtitle').textContent =
      `${detail.stock_code} ${detail.stock_name || ''} ｜ ${detail.stock_market || ''} ｜ ${maturity}${daysStr}`;

    document.getElementById('modal-conv-price-input').value =
      detail.conversion_price ? Number(detail.conversion_price).toFixed(2) : '';

    // Current price from either premium history or CB prices
    const lp = detail.latest_premium;
    const lc = detail.latest_price;
    if (lp && lp.cb_price) {
      document.getElementById('modal-cb-price').textContent    = fmtNum(lp.cb_price);
      document.getElementById('modal-stock-price').textContent = fmtNum(lp.stock_price);
      document.getElementById('modal-parity').textContent      = fmtNum(lp.parity);
      const pEl = document.getElementById('modal-premium');
      pEl.textContent = fmtPct(lp.premium_pct);
      pEl.className   = `text-2xl font-bold ${(lp.premium_pct ?? 0) >= 0 ? 'text-red-400' : 'text-green-400'}`;
    } else if (lc) {
      document.getElementById('modal-cb-price').textContent = fmtNum(lc.close_price);
      const parP = lc.close_price ? ((lc.close_price - 100) / 100 * 100) : null;
      if (parP !== null) {
        const pEl = document.getElementById('modal-premium');
        pEl.textContent = fmtPct(parP) + '※';
        pEl.className   = `text-2xl font-bold text-slate-400`;
        pEl.title = '相對面值(100)折溢價，非精確折溢價';
      }
      document.getElementById('modal-parity').textContent = '需設轉換價';
    }

    // Charts
    if (premHistory.length > 0) {
      buildPremiumChart(premHistory);
    } else if (cbHistory.length > 0) {
      buildParChart(cbHistory);
    }
    if (cbHistory.length > 0) {
      buildPriceChart(cbHistory, detail.conversion_price);
    }
    if (premHistory.length > 0) {
      buildHistoryTable(premHistory);
    } else if (cbHistory.length > 0) {
      buildCBPriceTable(cbHistory);
    }

    document.getElementById('modal-save-conv-price').onclick  = () => saveConversionPrice(cbCode);
    document.getElementById('modal-fetch-conv-price').onclick = () => autoFetchConvPrice(cbCode);

  } catch (e) {
    document.getElementById('modal-subtitle').textContent = '載入失敗: ' + e.message;
  }
}

function closeDetailModal() {
  document.getElementById('detail-modal').classList.add('hidden');
  document.body.style.overflow = '';
  destroyCharts();
  currentCB = null;
}

// ── Charts ────────────────────────────────────────────────────

const CHART_OPTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#1e293b' } },
    y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e293b' } },
  },
};

function buildPremiumChart(history) {
  const ctx  = document.getElementById('chart-premium').getContext('2d');
  const data = history.map(h => h.premium_pct !== null ? +h.premium_pct.toFixed(2) : null);
  charts.premium = new Chart(ctx, {
    type: 'line',
    data: {
      labels: history.map(h => h.trade_date),
      datasets: [{
        label: '折溢價%',
        data,
        borderColor: '#60a5fa',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 2,
        tension: 0.2,
        spanGaps: true,
        segment: { borderColor: ctx2 => ctx2.p0.parsed.y >= 0 ? '#f87171' : '#4ade80' },
      }],
    },
    options: { ...CHART_OPTS },
  });
}

function buildParChart(cbHistory) {
  const ctx  = document.getElementById('chart-premium').getContext('2d');
  charts.premium = new Chart(ctx, {
    type: 'line',
    data: {
      labels: cbHistory.map(h => h.trade_date),
      datasets: [{
        label: 'CB現價 vs 面值(%)',
        data: cbHistory.map(h => h.close_price !== null ? +((h.close_price - 100) / 100 * 100).toFixed(2) : null),
        borderColor: '#a78bfa',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 2,
        tension: 0.2,
        spanGaps: true,
      }],
    },
    options: { ...CHART_OPTS },
  });
}

function buildPriceChart(cbHistory, conversionPrice) {
  const ctx = document.getElementById('chart-price').getContext('2d');
  const datasets = [{
    label: 'CB現價',
    data: cbHistory.map(h => h.close_price),
    borderColor: '#60a5fa',
    backgroundColor: 'rgba(96,165,250,0.08)',
    borderWidth: 2,
    pointRadius: 2,
    tension: 0.2,
    fill: false,
  }];

  // Add face value line
  datasets.push({
    label: '面值(100)',
    data: cbHistory.map(() => 100),
    borderColor: '#475569',
    borderWidth: 1,
    borderDash: [6, 3],
    pointRadius: 0,
    fill: false,
  });

  charts.price = new Chart(ctx, {
    type: 'line',
    data: { labels: cbHistory.map(h => h.trade_date), datasets },
    options: { ...CHART_OPTS, plugins: { ...CHART_OPTS.plugins, tooltip: { mode: 'index', intersect: false } } },
  });
}

function buildHistoryTable(history) {
  const tbody = document.getElementById('modal-history-body');
  tbody.innerHTML = [...history].reverse().map(h => {
    const cls = parityPremiumClass(h.premium_pct);
    return `<tr class="hover:bg-slate-800/50">
      <td class="px-3 py-1.5">${h.trade_date}</td>
      <td class="px-3 py-1.5 text-right font-mono">${fmtNum(h.cb_price)}</td>
      <td class="px-3 py-1.5 text-right font-mono">${fmtNum(h.stock_price)}</td>
      <td class="px-3 py-1.5 text-right font-mono text-slate-400">${fmtNum(h.parity)}</td>
      <td class="px-3 py-1.5 text-right">
        <span class="px-1.5 py-0.5 rounded text-xs ${cls}">${fmtPct(h.premium_pct)}</span>
      </td>
      <td class="px-3 py-1.5 text-right text-slate-400">${h.cb_volume ?? '—'}</td>
    </tr>`;
  }).join('');
}

function buildCBPriceTable(cbHistory) {
  const tbody = document.getElementById('modal-history-body');
  tbody.innerHTML = [...cbHistory].reverse().map(h => {
    const parP = h.close_price !== null ? ((h.close_price - 100) / 100 * 100) : null;
    const cls  = parPremiumClass(parP);
    return `<tr class="hover:bg-slate-800/50">
      <td class="px-3 py-1.5">${h.trade_date}</td>
      <td class="px-3 py-1.5 text-right font-mono">${fmtNum(h.close_price)}</td>
      <td class="px-3 py-1.5 text-right font-mono text-slate-600">—</td>
      <td class="px-3 py-1.5 text-right font-mono text-slate-600">—</td>
      <td class="px-3 py-1.5 text-right">
        <span class="px-1.5 py-0.5 rounded text-xs ${cls}" title="相對面值">${fmtPct(parP)}※</span>
      </td>
      <td class="px-3 py-1.5 text-right text-slate-400">${h.volume ?? '—'}</td>
    </tr>`;
  }).join('');
}

// ── Conversion price ─────────────────────────────────────────

async function saveConversionPrice(cbCode) {
  const val = parseFloat(document.getElementById('modal-conv-price-input').value);
  if (!val || val <= 0) { alert('請輸入有效的轉換價格'); return; }
  const statusEl = document.getElementById('modal-conv-status');
  const btn      = document.getElementById('modal-save-conv-price');
  statusEl.textContent = '計算歷史折溢價中...';
  btn.disabled = true;
  try {
    const res = await put(`/cbs/${cbCode}`, { conversion_price: val });
    const days = res.backfilled_days ?? 0;
    statusEl.textContent = `✓ 已儲存，回補 ${days} 天歷史折溢價`;
    statusEl.className = 'text-xs text-green-400';

    // Update allRows so table reflects new conversion_price
    const row = allRows.find(r => r.cb_code === cbCode);
    if (row) row.conversion_price = val;

    // Reload premium history and rebuild charts in the open modal
    destroyCharts();
    document.getElementById('modal-history-body').innerHTML = '';
    const [cbHistory, premHistory, detail] = await Promise.all([
      get(`/prices/cb/${cbCode}?days=120`),
      get(`/premium/${cbCode}/history?days=120`),
      get(`/cbs/${cbCode}`),
    ]);

    // Update info cards with latest premium data
    if (premHistory.length > 0) {
      const latest = premHistory[premHistory.length - 1];
      document.getElementById('modal-cb-price').textContent    = fmtNum(latest.cb_price);
      document.getElementById('modal-stock-price').textContent = fmtNum(latest.stock_price);
      document.getElementById('modal-parity').textContent      = fmtNum(latest.parity);
      const pEl = document.getElementById('modal-premium');
      pEl.textContent = fmtPct(latest.premium_pct);
      pEl.className   = `text-2xl font-bold ${(latest.premium_pct ?? 0) >= 0 ? 'text-red-400' : 'text-green-400'}`;
      buildPremiumChart(premHistory);
      buildHistoryTable(premHistory);
    }
    if (cbHistory.length > 0) buildPriceChart(cbHistory, val);

    // Refresh main table so premium badges update
    await loadPremiumTable();
  } catch (e) {
    statusEl.textContent = '儲存失敗: ' + e.message;
    statusEl.className = 'text-xs text-red-400';
  } finally {
    btn.disabled = false;
  }
}

async function autoFetchConvPrice(cbCode) {
  const status = document.getElementById('modal-conv-status');
  status.textContent = '目前無法自動抓取，請手動輸入轉換價格';
  status.className = 'text-xs text-yellow-400';
}

// ── Sync conversion prices from CBAS ─────────────────────────

async function syncConversionPrices() {
  const btn = document.getElementById('btn-sync-conv');
  btn.disabled = true;
  btn.textContent = '⟳ 同步中...';
  try {
    const r = await post('/discover/sync-conversion-prices');
    const msg = `✓ 同步完成：${r.cbas_prices_fetched} 筆價格，更新 ${r.cbs_updated} 筆，回補 ${r.history_days_backfilled} 天歷史`;
    btn.textContent = '✓ 完成';
    setTimeout(() => { btn.textContent = '⟳ 同步轉換價'; btn.disabled = false; }, 3000);
    // Show brief notification
    const notif = document.createElement('div');
    notif.className = 'fixed bottom-4 right-4 bg-teal-800 text-white text-sm px-4 py-2 rounded-lg shadow-xl z-50';
    notif.textContent = msg;
    document.body.appendChild(notif);
    setTimeout(() => notif.remove(), 5000);
    await Promise.all([loadPremiumTable(), loadStats()]);
  } catch (e) {
    btn.textContent = '⟳ 同步轉換價';
    btn.disabled = false;
    alert('同步失敗: ' + e.message);
  }
}

// ── Historical backfill ──────────────────────────────────────

function showToast(msg, color = 'bg-slate-700') {
  const el = document.createElement('div');
  el.className = `fixed bottom-4 right-4 ${color} text-white text-sm px-4 py-2 rounded-lg shadow-xl z-[999]`;
  el.id = 'toast-notif';
  el.textContent = msg;
  document.getElementById('toast-notif')?.remove();
  document.body.appendChild(el);
  return el;
}

async function loadOverboughtData() {
  if (Object.keys(overboughtMap).length > 0) return; // already loaded
  const toast = showToast('🔍 60天歷史分析中，請稍候…', 'bg-blue-800');
  try {
    const res = await get('/analysis/cb-overbought?days=60');
    overboughtMap = {};
    (res.data || []).forEach(r => { overboughtMap[r.cb_code] = r; });
    toast.remove();
    showToast(`✅ 找到 ${res.count} 筆 CB 異常偏高`, 'bg-green-800');
    setTimeout(() => document.getElementById('toast-notif')?.remove(), 4000);
  } catch(e) {
    toast.remove();
    showToast('❌ 分析失敗: ' + e.message, 'bg-red-800');
    setTimeout(() => document.getElementById('toast-notif')?.remove(), 4000);
  }
}

async function backfillHistory() {
  const btn = document.getElementById('btn-backfill');
  if (!confirm(
    '補充歷史股價資料（過去90個交易日）\n\n' +
    '本功能會補充過去90天的股價資料，讓溢價圖表更完整。\n' +
    '每個交易日 15:45 系統會自動記錄當日資料。\n\n' +
    '背景執行，不會卡住頁面，確定繼續？'
  )) return;

  btn.disabled = true;
  btn.textContent = '📥 補充中...';

  // 通知 bar
  const notif = document.createElement('div');
  notif.id = 'backfill-notif';
  notif.className = 'fixed bottom-4 right-4 bg-purple-800 text-white text-sm px-4 py-3 rounded-lg shadow-xl z-50 max-w-sm';
  notif.innerHTML = '📥 歷史資料補充中…<br><span id="backfill-msg" class="text-purple-200 text-xs">啟動中...</span>';
  document.body.appendChild(notif);

  try {
    // 觸發後立即回傳（後台執行）
    await post('/discover/backfill-history', { days: 90 });

    // 輪詢進度
    let done = false;
    while (!done) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        const s = await get('/discover/backfill-status');
        const msgEl = document.getElementById('backfill-msg');
        if (!msgEl) break;

        if (s.running) {
          const pct = s.progress || 0;
          msgEl.textContent = `${pct}% — ${s.message}`;
          btn.textContent = `📥 ${pct}%`;
        } else if (s.progress === 100) {
          done = true;
          const r = s.last_result || {};
          notif.className = 'fixed bottom-4 right-4 bg-green-800 text-white text-sm px-4 py-3 rounded-lg shadow-xl z-50 max-w-sm';
          notif.innerHTML = `✅ 完成！補充 ${r.days_processed || 0} 天，${r.premiums_recalculated || 0} 筆溢折價已更新`;
          setTimeout(() => notif.remove(), 8000);
          await Promise.all([loadPremiumTable(), loadStats()]);
        } else if (s.progress === -1) {
          done = true;
          notif.className = 'fixed bottom-4 right-4 bg-red-800 text-white text-sm px-4 py-3 rounded-lg shadow-xl z-50 max-w-sm';
          notif.innerHTML = `❌ 失敗：${s.message}`;
          setTimeout(() => notif.remove(), 6000);
        }
      } catch (_) { /* ignore poll errors */ }
    }
  } catch (e) {
    notif.textContent = '補充失敗: ' + e.message;
    notif.className = 'fixed bottom-4 right-4 bg-red-800 text-white text-sm px-4 py-3 rounded-lg shadow-xl z-50 max-w-sm';
    setTimeout(() => notif.remove(), 5000);
  } finally {
    btn.textContent = '📥 補歷史資料';
    btn.disabled = false;
  }
}

// ── Quick inline conversion price setter ─────────────────────

function quickSetConvPrice(cbCode, btnEl) {
  // Replace the "設定" button with an inline input
  const cell = btnEl.parentElement;
  cell.innerHTML = `
    <div class="flex items-center gap-1" onclick="event.stopPropagation()">
      <input id="qcp-${cbCode}" type="number" step="0.01" placeholder="轉換價"
        class="bg-slate-700 border border-amber-600 rounded px-1.5 py-0.5 text-xs w-20 text-right focus:outline-none focus:border-blue-400"
        onkeydown="if(event.key==='Enter') submitQuickConvPrice('${cbCode}'); if(event.key==='Escape') renderTable();" />
      <button onclick="submitQuickConvPrice('${cbCode}')" class="text-xs bg-blue-600 hover:bg-blue-500 text-white rounded px-1.5 py-0.5">✓</button>
      <button onclick="event.stopPropagation();renderTable()" class="text-xs text-slate-500 hover:text-slate-300 px-1">✕</button>
    </div>`;
  const inp = document.getElementById(`qcp-${cbCode}`);
  if (inp) { inp.focus(); }
}

async function submitQuickConvPrice(cbCode) {
  const inp = document.getElementById(`qcp-${cbCode}`);
  if (!inp) return;
  const val = parseFloat(inp.value);
  if (!val || val <= 0) { inp.style.borderColor = '#ef4444'; return; }

  inp.disabled = true;
  try {
    const res = await put(`/cbs/${cbCode}`, { conversion_price: val });
    // Update allRows so the table re-renders with the new price
    const row = allRows.find(r => r.cb_code === cbCode);
    if (row) {
      row.conversion_price = val;
      // Recompute premium for this row using backfilled data
    }
    await loadPremiumTable();   // refresh all rows from API
  } catch (e) {
    alert('設定失敗: ' + e.message);
    renderTable();
  }
}

// ── Bulk conversion price modal ───────────────────────────────

let bulkRows = [];

async function openBulkConvModal() {
  document.getElementById('bulk-modal').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  await loadBulkRows();
}

function closeBulkConvModal() {
  document.getElementById('bulk-modal').classList.add('hidden');
  document.body.style.overflow = '';
}

async function loadBulkRows() {
  const container = document.getElementById('bulk-list');
  container.innerHTML = '<div class="text-center text-slate-500 py-8">載入中...</div>';
  try {
    const cbs = await get('/cbs?active_only=true');
    // Sort: no conversion_price first
    bulkRows = cbs.sort((a, b) => {
      if (!a.conversion_price && b.conversion_price) return -1;
      if (a.conversion_price && !b.conversion_price) return  1;
      return a.cb_code.localeCompare(b.cb_code);
    });
    renderBulkList();
  } catch (e) {
    container.innerHTML = `<div class="text-red-400 py-4">載入失敗: ${e.message}</div>`;
  }
}

function renderBulkList() {
  const search = (document.getElementById('bulk-search')?.value || '').toLowerCase();
  const filtered = search
    ? bulkRows.filter(r => `${r.cb_code} ${r.cb_name||''} ${r.stock_code} ${r.stock_name||''}`.toLowerCase().includes(search))
    : bulkRows;

  const missing = filtered.filter(r => !r.conversion_price).length;
  const total   = filtered.length;
  document.getElementById('bulk-summary').textContent =
    `共 ${total} 筆，${missing} 筆尚未設定轉換價格`;

  document.getElementById('bulk-list').innerHTML = filtered.map(r => {
    const hasPrice = r.conversion_price && r.conversion_price > 0;
    return `<tr class="border-b border-slate-800 ${hasPrice ? '' : 'bg-amber-950/10'}">
      <td class="px-3 py-1.5 font-mono text-blue-300 text-sm">${r.cb_code}</td>
      <td class="px-3 py-1.5 text-sm max-w-[140px] truncate" title="${r.cb_name||''}">${r.cb_name||'—'}</td>
      <td class="px-3 py-1.5 text-sm text-slate-400">${r.stock_code} ${r.stock_name||''}</td>
      <td class="px-3 py-1.5 text-right text-xs text-slate-500">${r.maturity_date||'—'}</td>
      <td class="px-3 py-1.5 text-right">
        <input type="number" step="0.01" placeholder="${hasPrice ? r.conversion_price.toFixed(2) : '輸入轉換價'}"
          data-cb="${r.cb_code}"
          class="bulk-price-input bg-slate-800 border ${hasPrice ? 'border-slate-600' : 'border-amber-700/60'} rounded px-2 py-0.5 text-sm w-28 text-right focus:outline-none focus:border-blue-500"
          value="${hasPrice ? r.conversion_price.toFixed(2) : ''}" />
      </td>
    </tr>`;
  }).join('');
}

async function saveBulkConvPrices() {
  const inputs = document.querySelectorAll('.bulk-price-input');
  const prices = {};
  inputs.forEach(inp => {
    const v = parseFloat(inp.value);
    if (v && v > 0) prices[inp.dataset.cb] = v;
  });
  if (Object.keys(prices).length === 0) {
    alert('請至少輸入一個轉換價格');
    return;
  }
  const btn = document.getElementById('bulk-save-btn');
  btn.disabled = true;
  btn.textContent = '儲存中...';
  try {
    const res = await post('/cbs/bulk-conversion-price', { prices });
    const saved   = Object.values(res.results).filter(r => r.ok).length;
    const backfilled = Object.values(res.results).reduce((s, r) => s + (r.backfilled||0), 0);
    alert(`✓ 儲存 ${saved} 筆，回補 ${backfilled} 天歷史資料`);
    closeBulkConvModal();
    await loadPremiumTable();
    await loadStats();
  } catch (e) {
    alert('儲存失敗: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '保存全部';
  }
}

// ── Add CB modal ─────────────────────────────────────────────

function openAddModal() {
  document.getElementById('add-modal').classList.remove('hidden');
  document.getElementById('add-error').classList.add('hidden');
  document.getElementById('add-cb-code').focus();
}

function closeAddModal() {
  document.getElementById('add-modal').classList.add('hidden');
  ['add-cb-code','add-cb-name','add-stock-code','add-stock-name','add-conv-price','add-notes']
    .forEach(id => { document.getElementById(id).value = ''; });
}

async function submitAddCB() {
  const errEl = document.getElementById('add-error');
  errEl.classList.add('hidden');
  const cbCode    = document.getElementById('add-cb-code').value.trim();
  const cbName    = document.getElementById('add-cb-name').value.trim();
  const stockCode = document.getElementById('add-stock-code').value.trim();
  const stockName = document.getElementById('add-stock-name').value.trim();
  const market    = document.getElementById('add-market').value;
  const convPrice = parseFloat(document.getElementById('add-conv-price').value) || null;
  const notes     = document.getElementById('add-notes').value.trim();
  if (!cbCode || !cbName || !stockCode) {
    errEl.textContent = 'CB代號、CB名稱、股票代號為必填';
    errEl.classList.remove('hidden');
    return;
  }
  try {
    await post('/cbs', { cb_code: cbCode, cb_name: cbName, stock_code: stockCode,
      stock_name: stockName, stock_market: market, conversion_price: convPrice, notes });
    closeAddModal();
    await Promise.all([loadPremiumTable(), loadStats()]);
  } catch (e) {
    errEl.textContent = '新增失敗: ' + e.message;
    errEl.classList.remove('hidden');
  }
}

// ── K-Line Modal ─────────────────────────────────────────────

let klineCbChart    = null;
let klineStockChart = null;
let klineCurrentCb  = null;
let klineDays       = 60;

async function openKlineModal(cbCode) {
  klineCurrentCb = cbCode;
  document.getElementById('kline-modal').classList.remove('hidden');
  document.getElementById('kline-loading').classList.remove('hidden');
  document.getElementById('kline-title').textContent = cbCode;
  document.getElementById('kline-subtitle').textContent = '';
  await loadKlineData(cbCode, klineDays);
}

function closeKlineModal() {
  document.getElementById('kline-modal').classList.add('hidden');
  if (klineCbChart)    { klineCbChart.remove();   klineCbChart    = null; }
  if (klineStockChart) { klineStockChart.remove(); klineStockChart = null; }
}

async function loadKlineData(cbCode, days) {
  document.getElementById('kline-loading').classList.remove('hidden');
  try {
    const data = await get(`/cbs/${cbCode}/kline-data?days=${days}`);
    const cb   = data.cb;

    document.getElementById('kline-title').textContent =
      `${cb.cb_code}　${cb.cb_name}`;
    document.getElementById('kline-subtitle').textContent =
      `標的股票：${cb.stock_code} ${cb.stock_name || ''}`;
    document.getElementById('kline-cb-label').textContent =
      `${cb.cb_code} ${cb.cb_name} — CB收盤價`;
    document.getElementById('kline-stock-label').textContent =
      `${cb.stock_code} ${cb.stock_name || ''} — 股票K線`;

    // Destroy old charts
    if (klineCbChart)    { klineCbChart.remove();   klineCbChart    = null; }
    if (klineStockChart) { klineStockChart.remove(); klineStockChart = null; }

    const chartOpts = {
      layout: { background: { color: '#0f172a' }, textColor: '#94a3b8' },
      grid:   { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      crosshair: { mode: 1 },
      timeScale: { borderColor: '#334155', timeVisible: true, rightOffset: 5 },
      rightPriceScale: { borderColor: '#334155' },
      handleScroll: true,
      handleScale: true,
    };

    // 上圖：CB 收盤價折線
    const cbEl = document.getElementById('kline-cb-chart');
    cbEl.innerHTML = '';
    klineCbChart = LightweightCharts.createChart(cbEl, { ...chartOpts, width: cbEl.clientWidth, height: cbEl.clientHeight });
    const cbSeries = klineCbChart.addLineSeries({ color: '#60a5fa', lineWidth: 2 });
    if (data.cb_prices.length) {
      cbSeries.setData(data.cb_prices.map(d => ({ time: d.date, value: d.value })));
    }

    // 下圖：股票 K 線
    const stEl = document.getElementById('kline-stock-chart');
    stEl.innerHTML = '';
    klineStockChart = LightweightCharts.createChart(stEl, { ...chartOpts, width: stEl.clientWidth, height: stEl.clientHeight });
    const candleSeries = klineStockChart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    if (data.stock_ohlc.length) {
      // 只顯示 CB 有資料的那段時間
      const cbFirst = data.cb_prices[0]?.date ?? '0000-00-00';
      const cbLast  = data.cb_prices[data.cb_prices.length - 1]?.date ?? '9999-99-99';
      const ohlcFiltered = data.stock_ohlc.filter(d => d.date >= cbFirst && d.date <= cbLast);
      candleSeries.setData(ohlcFiltered.map(d => ({
        time: d.date, open: d.open, high: d.high, low: d.low, close: d.close,
      })));
    }

    // 以股票日期範圍為準，兩圖對齊相同時間軸
    // 股票資料最完整，先 fit；CB 圖也套用相同 visible range
    klineStockChart.timeScale().fitContent();
    klineCbChart.timeScale().fitContent();
    // 稍等 layout 完成再同步，確保兩圖顯示完全相同的時間段
    setTimeout(() => {
      const stockRange = klineStockChart.timeScale().getVisibleLogicalRange();
      const cbRange    = klineCbChart.timeScale().getVisibleLogicalRange();
      // 取兩者中較寬的範圍（涵蓋所有資料）
      if (stockRange && cbRange) {
        const merged = {
          from: Math.min(stockRange.from, cbRange.from),
          to:   Math.max(stockRange.to,   cbRange.to),
        };
        klineCbChart.timeScale().setVisibleLogicalRange(merged);
        klineStockChart.timeScale().setVisibleLogicalRange(merged);
      } else if (stockRange) {
        klineCbChart.timeScale().setVisibleLogicalRange(stockRange);
      }
    }, 80);

    // 互相同步 zoom/pan
    let syncing = false;
    klineCbChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (syncing || !range) return;
      syncing = true;
      klineStockChart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    });
    klineStockChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (syncing || !range) return;
      syncing = true;
      klineCbChart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    });

  } catch(e) {
    document.getElementById('kline-title').textContent = '載入失敗: ' + e.message;
  } finally {
    document.getElementById('kline-loading').classList.add('hidden');
  }
}

// ── Event listeners ──────────────────────────────────────────

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function resizeTableWrap() {
  const wrap = document.getElementById('table-wrap');
  if (!wrap) return;
  const top = wrap.getBoundingClientRect().top;
  wrap.style.height = (window.innerHeight - top) + 'px';
}

document.addEventListener('DOMContentLoaded', async () => {
  resizeTableWrap();
  window.addEventListener('resize', resizeTableWrap);

  // K-line day buttons
  document.querySelectorAll('.kline-days-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      document.querySelectorAll('.kline-days-btn').forEach(b => {
        b.className = 'kline-days-btn text-xs px-2 py-1 rounded bg-slate-700 text-slate-300';
      });
      btn.className = 'kline-days-btn text-xs px-2 py-1 rounded bg-blue-700 text-white';
      klineDays = parseInt(btn.dataset.days);
      if (klineCurrentCb) await loadKlineData(klineCurrentCb, klineDays);
    });
  });

  // Escape to close K-line modal
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeKlineModal();
  });
  document.getElementById('search-input').addEventListener('input',  debounce(renderTable, 250));
  document.getElementById('filter-market').addEventListener('change', renderTable);
  document.getElementById('filter-premium').addEventListener('change', async () => {
    if (document.getElementById('filter-premium').value === 'overbought') {
      await loadOverboughtData();
    }
    renderTable();
  });

  document.querySelectorAll('th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortCol === col) {
        sortAsc = !sortAsc;
      } else {
        sortCol = col;
        sortAsc = (col === 'cb_code' || col === 'stock_code' || col === 'maturity_date');
      }
      renderTable();
    });
  });

  document.getElementById('btn-refresh').addEventListener('click', triggerRefresh);
  document.getElementById('btn-scan').addEventListener('click', triggerFullScan);
  document.getElementById('btn-add-cb').addEventListener('click', openAddModal);
  document.getElementById('btn-bulk-conv').addEventListener('click', openBulkConvModal);
  document.getElementById('btn-sync-conv').addEventListener('click', syncConversionPrices);
  document.getElementById('btn-backfill').addEventListener('click', backfillHistory);

  document.getElementById('bulk-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeBulkConvModal();
  });

  document.getElementById('detail-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeDetailModal();
  });
  document.getElementById('add-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeAddModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDetailModal(); closeAddModal(); }
  });

  await Promise.all([loadPremiumTable(), loadStats()]);

  // Check if discovery is already running
  try {
    const s = await get('/discover/status');
    if (s.running) startScanPolling();
  } catch (e) { /* ignore */ }
});
