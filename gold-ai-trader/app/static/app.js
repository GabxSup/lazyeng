/* AURUM AI · dashboard client
   Zero dependencies. All user-visible market/account values are rendered as
   text nodes or escaped template strings; no server value is trusted as HTML. */
(() => {
  'use strict';

  const CSRF_HEADER = 'X-AURUM-CSRF';
  const CSRF_VALUE = '1';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const state = {
    data: null,
    bootstrap: null,
    candles: [],
    timeframe: 'M5',
    chartLoading: false,
    stateLoading: false,
    pollTimer: null,
    candleTimer: null,
    loaded: false,
    networkDown: false,
    bootRetry: null,
    auth: null,
    authEnabled: false,
    modalOnClose: null,
    drawerOpen: false,
    lastFocus: null,
    currentBacktest: null,
    backtestAutoLoaded: false,
    activeTab: 'positions',
    reportPoints: null,
    settingsSaving: false,
    loginPending: false,
  };

  const el = {};
  const fmtUSD = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const fmtUSD0 = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0,
  });
  const fmtNumber = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const RISK_LABELS = {
    kill_switch: 'Halt switch',
    connection: 'Broker link',
    market_hours: 'Market hours',
    session: 'Session',
    live_arm: 'Live arm',
    spread: 'Spread',
    slots: 'Position slot',
    setup: 'Setup',
    freshness: 'Signal age',
    daily_loss: 'Daily loss',
    drawdown: 'Drawdown',
    cooldown: 'Cooldown',
    sizing: 'Position size',
    reward_risk: 'Reward / risk',
  };
  const SOFT_CHECKS = new Set(['setup', 'freshness', 'sizing', 'reward_risk']);

  // ---------------------------------------------------------------- //
  // Helpers                                                           //
  // ---------------------------------------------------------------- //
  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function brokerName(mode) {
    if (mode === 'mt4') return 'MetaTrader 4';
    if (mode === 'mt5') return 'MetaTrader 5';
    return 'Paper simulator';
  }

  function brokerShort(mode) {
    if (mode === 'mt4') return 'MT4';
    if (mode === 'mt5') return 'MT5';
    return 'Paper';
  }

  function icon(name, cls = 'icon') {
    return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  function money(value, fallback = '—') {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return fallback;
    return fmtUSD.format(Number(value));
  }

  function signedMoney(value) {
    if (!Number.isFinite(Number(value))) return '—';
    const n = Number(value);
    return `${n > 0 ? '+' : n < 0 ? '−' : ''}${fmtUSD.format(Math.abs(n))}`;
  }

  function pct(value, digits = 2, signed = false) {
    if (!Number.isFinite(Number(value))) return '—';
    const n = Number(value);
    const prefix = signed ? (n > 0 ? '+' : n < 0 ? '−' : '') : (n < 0 ? '−' : '');
    return `${prefix}${Math.abs(n).toFixed(digits)}%`;
  }

  function price(value) {
    if (!Number.isFinite(Number(value))) return '—';
    return fmtNumber.format(Number(value));
  }

  function compact(value) {
    if (!Number.isFinite(Number(value))) return '—';
    const n = Number(value);
    if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}m`;
    if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
    return String(Math.round(n));
  }

  function isoToDate(value) {
    if (!value) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function fmtTime(value, seconds = false) {
    const d = value instanceof Date ? value : isoToDate(value);
    if (!d) return '—';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) });
  }

  function fmtDate(value) {
    const d = value instanceof Date ? value : isoToDate(value);
    if (!d) return '—';
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function fmtClockUTC() {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(new Date());
  }

  function timeAgo(value) {
    const d = isoToDate(value);
    if (!d) return '—';
    const seconds = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (seconds < 10) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  function setText(target, value) {
    const node = typeof target === 'string' ? $(target) : target;
    if (!node) return;
    const next = String(value);
    if (node.textContent !== next) node.textContent = next;
  }

  function setClass(target, className, on) {
    const node = typeof target === 'string' ? $(target) : target;
    if (!node) return;
    node.classList.toggle(className, !!on);
  }

  function toast(title, message = '', type = 'info', timeout = 4800) {
    const host = el.toasts;
    if (!host) return;
    const node = document.createElement('div');
    node.className = `toast ${type}`;
    const iconName = type === 'ok' ? 'check' : type === 'error' ? 'alert' : 'info';
    node.innerHTML = `<span class="toast-mark">${icon(iconName, 'icon')}</span><div><strong>${esc(title)}</strong>${message ? esc(message) : ''}</div>`;
    host.appendChild(node);
    const remove = () => {
      if (!node.isConnected) return;
      node.classList.add('leaving');
      setTimeout(() => node.remove(), 200);
    };
    node.addEventListener('click', remove);
    setTimeout(remove, timeout);
  }

  class ApiError extends Error {
    constructor(message, status = 0, code = 'error') {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
    }
  }

  async function api(path, options = {}) {
    const { method = 'GET', body = null, suppressAuth = false, timeout = 45000 } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const headers = { Accept: 'application/json', [CSRF_HEADER]: CSRF_VALUE };
    if (body !== null) headers['Content-Type'] = 'application/json';
    try {
      const response = await fetch(path, {
        method,
        headers,
        credentials: 'same-origin',
        body: body === null ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      let payload = null;
      const raw = await response.text();
      if (raw) {
        try { payload = JSON.parse(raw); } catch { payload = null; }
      }
      if (response.status === 401 && !suppressAuth) {
        handleSessionExpired();
        throw new ApiError('Your session has expired.', 401, 'unauthorized');
      }
      if (!response.ok) {
        throw new ApiError((payload && payload.error) || `Request failed (${response.status})`, response.status, (payload && payload.code) || 'error');
      }
      return payload || {};
    } catch (error) {
      if (error.name === 'AbortError') throw new ApiError('The request timed out. The engine may be busy.', 0, 'timeout');
      if (error instanceof ApiError) throw error;
      throw new ApiError('Cannot reach the trading engine.', 0, 'network');
    } finally {
      clearTimeout(timer);
    }
  }

  let toastNetwork = false;
  function networkFailure(error) {
    if (state.networkDown) return;
    state.networkDown = true;
    if (!toastNetwork) {
      toastNetwork = true;
      toast('Engine unreachable', error.message || 'Retrying automatically.', 'error');
    }
    document.body.dataset.network = 'down';
    setText('#conn-label', 'OFFLINE');
    const dot = $('#conn-dot');
    if (dot) dot.className = 'dot danger';
    setText('#market-label', 'RECONNECTING');
    setText('#footer-mode', 'OFFLINE');
  }

  function networkRestored() {
    if (!state.networkDown) return;
    state.networkDown = false;
    toastNetwork = false;
    delete document.body.dataset.network;
    toast('Engine reconnected', 'Dashboard data is flowing again.', 'ok', 2600);
  }

  function applyState(next) {
    if (!next) return;
    const previous = state.data;
    state.data = next;
    if (!previous) {
      state.timeframe = next.settings && next.settings.timeframe ? next.settings.timeframe : 'M5';
    }
    state.authEnabled = !!(next.meta && next.meta.auth_enabled);
    networkRestored();
    renderAll();
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(refreshState, 2500);
    state.candleTimer = setInterval(() => fetchCandles({ silent: true }), 12000);
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    if (state.candleTimer) clearInterval(state.candleTimer);
    state.pollTimer = null;
    state.candleTimer = null;
  }

  async function refreshState() {
    if (state.stateLoading || document.hidden || !state.loaded) return;
    state.stateLoading = true;
    try {
      const payload = await api('/api/state');
      if (payload && payload.state) applyState(payload.state);
    } catch (error) {
      if (error.status !== 401) networkFailure(error);
    } finally {
      state.stateLoading = false;
    }
  }

  async function fetchCandles(options = {}) {
    if (state.chartLoading || !state.data) return;
    state.chartLoading = true;
    if (!options.silent && !state.candles.length) showChartEmpty('Loading candles…');
    try {
      const payload = await api(`/api/candles?timeframe=${encodeURIComponent(state.timeframe)}&limit=200`);
      state.candles = Array.isArray(payload.candles) ? payload.candles : [];
      updateChartTimeframeButtons();
      renderMarketHeader();
      if (state.chart) {
        state.chart.setData(state.candles);
        updateChartOverlays();
      }
      if (!state.candles.length) showChartEmpty('No candles available from the feed.');
      else hideChartEmpty();
      if (state.data) fetchLatestBacktestOnce();
    } catch (error) {
      if (error.status !== 401) {
        showChartEmpty('Chart feed unavailable — retrying…');
        if (!options.silent) toast('Chart unavailable', error.message, 'error');
      }
    } finally {
      state.chartLoading = false;
    }
  }

  function showChartEmpty(message) {
    if (!el.chartEmpty) return;
    el.chartEmpty.textContent = message;
    el.chartEmpty.hidden = false;
  }

  function hideChartEmpty() {
    if (el.chartEmpty) el.chartEmpty.hidden = true;
  }

  // ---------------------------------------------------------------- //
  // Canvas charts                                                     //
  // ---------------------------------------------------------------- //
  function calcEma(values, period) {
    const out = new Array(values.length).fill(null);
    if (values.length < period || period < 1) return out;
    let sum = 0;
    for (let i = 0; i < period; i += 1) sum += values[i];
    let prev = sum / period;
    out[period - 1] = prev;
    const k = 2 / (period + 1);
    for (let i = period; i < values.length; i += 1) {
      prev = (values[i] - prev) * k + prev;
      out[i] = prev;
    }
    return out;
  }

  class PriceChart {
    constructor(canvas, tooltip, onHover) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.tooltip = tooltip;
      this.data = [];
      this.positions = [];
      this.signal = null;
      this.hover = null;
      this.mouse = null;
      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this.onHover = onHover || (() => {});
      this.onLeave = null;
      const target = canvas.parentElement;
      const resize = () => this.resize();
      if (window.ResizeObserver) {
        this.observer = new ResizeObserver(resize);
        this.observer.observe(target);
      }
      window.addEventListener('resize', resize);
      canvas.addEventListener('mousemove', (event) => this.handleMove(event));
      canvas.addEventListener('mouseleave', () => {
        this.hover = null;
        this.mouse = null;
        this.tooltip.hidden = true;
        this.draw();
        this.onHover(null);
      });
    }

    resize() {
      const rect = this.canvas.parentElement.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 20) return;
      this.width = rect.width;
      this.height = rect.height;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(this.width * this.dpr);
      this.canvas.height = Math.round(this.height * this.dpr);
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      this.draw();
    }

    setData(data) {
      this.data = Array.isArray(data) ? data : [];
      this.resize();
    }

    setOverlays(positions, signal) {
      this.positions = Array.isArray(positions) ? positions : [];
      this.signal = signal || null;
      this.draw();
    }

    handleMove(event) {
      if (!this.geom || !this.data.length) return;
      const rect = this.canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      this.mouse = { x, y };
      const { left, step, visible } = this.geom;
      const index = Math.max(0, Math.min(visible.length - 1, Math.floor((x - left) / step)));
      const candle = visible[index];
      this.hover = index;
      this.showTooltip(candle, x, y);
      this.onHover(candle);
      this.draw();
    }

    showTooltip(candle, x, y) {
      if (!candle) { this.tooltip.hidden = true; return; }
      const d = new Date(candle.time * 1000);
      const dir = candle.close >= candle.open ? 'up' : 'down';
      const change = candle.open ? ((candle.close - candle.open) / candle.open) * 100 : 0;
      this.tooltip.innerHTML = `
        <div class="tt-time">${esc(d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</div>
        <div class="tt-row"><span>Open</span><b>${price(candle.open)}</b></div>
        <div class="tt-row"><span>High</span><b>${price(candle.high)}</b></div>
        <div class="tt-row"><span>Low</span><b>${price(candle.low)}</b></div>
        <div class="tt-row"><span>Close</span><b class="${dir}">${price(candle.close)}</b></div>
        <div class="tt-row"><span>Change</span><b class="${dir}">${pct(change, 2, true)}</b></div>`;
      this.tooltip.hidden = false;
      const w = this.tooltip.offsetWidth || 150;
      const h = this.tooltip.offsetHeight || 110;
      let left = x + 14;
      let top = y + 12;
      if (left + w > this.width - 4) left = x - w - 14;
      if (top + h > this.height - 4) top = Math.max(4, y - h - 12);
      this.tooltip.style.left = `${Math.max(4, left)}px`;
      this.tooltip.style.top = `${Math.max(4, top)}px`;
    }

    draw() {
      const ctx = this.ctx;
      if (!this.width || !this.height) return;
      const w = this.width;
      const h = this.height;
      ctx.clearRect(0, 0, w, h);
      const plot = { left: 4, right: 61, top: 8, bottom: 20 };
      const pw = w - plot.left - plot.right;
      const ph = h - plot.top - plot.bottom;
      if (pw < 30 || ph < 30) return;
      const visibleCount = Math.max(24, Math.min(120, Math.floor(pw / 7.2)));
      const start = Math.max(0, this.data.length - visibleCount);
      const all = this.data;
      const visible = all.slice(start);
      if (!visible.length) return;
      const closes = all.map((c) => Number(c.close));
      const e20 = calcEma(closes, 20).slice(start);
      const e50 = calcEma(closes, 50).slice(start);
      let min = Math.min(...visible.map((c) => Number(c.low)));
      let max = Math.max(...visible.map((c) => Number(c.high)));
      const levels = [];
      const position = this.positions && this.positions[0];
      if (position) {
        if (position.entry_price) levels.push(Number(position.entry_price));
        if (position.sl) levels.push(Number(position.sl));
        if (position.tp) levels.push(Number(position.tp));
      } else if (this.signal && (this.signal.action === 'BUY' || this.signal.action === 'SELL')) {
        if (this.signal.entry) levels.push(Number(this.signal.entry));
        if (this.signal.sl) levels.push(Number(this.signal.sl));
        if (this.signal.tp) levels.push(Number(this.signal.tp));
      }
      levels.forEach((value) => {
        if (!Number.isFinite(value)) return;
        min = Math.min(min, value);
        max = Math.max(max, value);
      });
      const rangeRaw = max - min;
      const pad = Math.max(rangeRaw * 0.12, 0.35);
      min -= pad;
      max += pad;
      const range = Math.max(0.01, max - min);
      const plotRight = plot.left + pw;
      const plotBottom = plot.top + ph;
      const step = pw / visible.length;
      const candleWidth = Math.max(2, Math.min(8, step * 0.58));
      const y = (value) => plot.top + ((max - value) / range) * ph;
      const x = (index) => plot.left + (index + 0.5) * step;
      this.geom = { left: plot.left, step, visible, plotRight, plotBottom };

      // Background.
      const bg = ctx.createLinearGradient(0, 0, 0, h);
      bg.addColorStop(0, 'rgba(255,255,255,0.012)');
      bg.addColorStop(1, 'rgba(0,0,0,0.03)');
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, w, h);

      // Horizontal grid + labels.
      ctx.font = '9px "SFMono-Regular", "SF Mono", ui-monospace, Menlo, monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      for (let i = 0; i <= 4; i += 1) {
        const py = plot.top + (ph * i) / 4;
        const value = max - (range * i) / 4;
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(255,255,255,0.047)';
        ctx.lineWidth = 0.5;
        ctx.moveTo(plot.left, Math.round(py) + 0.5);
        ctx.lineTo(plotRight, Math.round(py) + 0.5);
        ctx.stroke();
        ctx.fillStyle = 'rgba(136,145,159,0.7)';
        ctx.fillText(value.toFixed(2), w - 8, py);
      }

      // Vertical grid + time labels.
      const vTicks = w < 460 ? 3 : 5;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      for (let i = 0; i <= vTicks; i += 1) {
        const px = plot.left + (pw * i) / vTicks;
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(255,255,255,0.032)';
        ctx.moveTo(Math.round(px) + 0.5, plot.top);
        ctx.lineTo(Math.round(px) + 0.5, plotBottom);
        ctx.stroke();
        const idx = Math.min(visible.length - 1, Math.max(0, Math.round((visible.length - 1) * (i / vTicks))));
        const d = new Date(visible[idx].time * 1000);
        ctx.fillStyle = 'rgba(136,145,159,0.6)';
        const label = w < 460 ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` :
          `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        ctx.textAlign = i === 0 ? 'left' : i === vTicks ? 'right' : 'center';
        const labelX = i === 0 ? plot.left : i === vTicks ? plotRight : px;
        ctx.fillText(label, labelX, plotBottom + 5);
      }
      ctx.textAlign = 'right';

      // Clip plot content.
      ctx.save();
      ctx.beginPath();
      ctx.rect(plot.left, plot.top - 2, pw, ph + 4);
      ctx.clip();

      // Candles.
      visible.forEach((candle, i) => {
        const isUp = Number(candle.close) >= Number(candle.open);
        const color = isUp ? 'rgba(61,219,160,0.92)' : 'rgba(255,100,119,0.9)';
        const wick = isUp ? 'rgba(61,219,160,0.55)' : 'rgba(255,100,119,0.55)';
        const cx = x(i);
        const highY = y(Number(candle.high));
        const lowY = y(Number(candle.low));
        const openY = y(Number(candle.open));
        const closeY = y(Number(candle.close));
        ctx.beginPath();
        ctx.strokeStyle = wick;
        ctx.lineWidth = 1;
        ctx.moveTo(Math.round(cx) + 0.5, highY);
        ctx.lineTo(Math.round(cx) + 0.5, lowY);
        ctx.stroke();
        const top = Math.min(openY, closeY);
        const height = Math.max(1, Math.abs(closeY - openY));
        ctx.fillStyle = color;
        ctx.fillRect(cx - candleWidth / 2, top, candleWidth, height);
      });

      // EMA lines.
      const plotSeries = (series, color, width) => {
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        let started = false;
        series.forEach((value, i) => {
          if (value === null || !Number.isFinite(value)) return;
          const px = x(i);
          const py = y(value);
          if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
        });
        ctx.stroke();
      };
      plotSeries(e20, 'rgba(230,184,106,0.72)', 1.1);
      plotSeries(e50, 'rgba(118,167,255,0.64)', 1.1);

      // Price levels: dashed guides make the tags unambiguous even when the
      // labels are shifted vertically to avoid collisions.
      const lastPrice = Number(visible[visible.length - 1].close);
      const tagDefs = [{
        value: lastPrice,
        label: 'LAST',
        fill: 'rgba(23,31,43,0.97)',
        border: 'rgba(255,255,255,0.2)',
        text: '#eef1f6',
        line: 'rgba(235,240,248,0.19)',
        dash: [3, 4],
      }];
      if (position) {
        tagDefs.push(
          { value: Number(position.sl), label: 'SL', fill: 'rgba(43,20,26,0.97)', border: 'rgba(255,100,119,0.45)', text: '#ff9ca8', line: 'rgba(255,100,119,0.36)', dash: [4, 4] },
          { value: Number(position.tp), label: 'TP', fill: 'rgba(15,35,29,0.97)', border: 'rgba(61,219,160,0.42)', text: '#83e7c0', line: 'rgba(61,219,160,0.32)', dash: [4, 4] },
        );
      } else if (this.signal && (this.signal.action === 'BUY' || this.signal.action === 'SELL')) {
        tagDefs.push(
          { value: Number(this.signal.sl), label: 'SL', fill: 'rgba(43,20,26,0.9)', border: 'rgba(255,100,119,0.28)', text: 'rgba(255,156,168,0.82)', line: 'rgba(255,100,119,0.2)', dash: [3, 4] },
          { value: Number(this.signal.tp), label: 'TP', fill: 'rgba(15,35,29,0.9)', border: 'rgba(61,219,160,0.25)', text: 'rgba(131,231,192,0.8)', line: 'rgba(61,219,160,0.18)', dash: [3, 4] },
        );
      }
      tagDefs.forEach((item) => {
        if (!Number.isFinite(item.value)) return;
        ctx.beginPath();
        ctx.setLineDash(item.dash);
        ctx.strokeStyle = item.line;
        ctx.lineWidth = 0.8;
        ctx.moveTo(plot.left, y(item.value));
        ctx.lineTo(plotRight, y(item.value));
        ctx.stroke();
      });
      if (position && Number.isFinite(Number(position.entry_price))) {
        ctx.beginPath();
        ctx.setLineDash([2, 4]);
        ctx.strokeStyle = 'rgba(230,184,106,0.28)';
        ctx.lineWidth = 0.8;
        ctx.moveTo(plot.left, y(Number(position.entry_price)));
        ctx.lineTo(plotRight, y(Number(position.entry_price)));
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.restore();

      // Price tags: lay them out along the right axis with a 14px minimum gap
      // so close levels never render on top of one another.
      ctx.font = '8px "SFMono-Regular", "SF Mono", ui-monospace, Menlo, monospace';
      const tagItems = tagDefs
        .filter((item) => Number.isFinite(item.value))
        .map((item) => ({
          ...item,
          desiredY: Math.max(plot.top + 7, Math.min(plotBottom - 7, y(item.value))),
          width: ctx.measureText(`${item.label} ${item.value.toFixed(2)}`).width + 10,
        }))
        .sort((a, b) => a.desiredY - b.desiredY);
      const tagGap = 14.5;
      let cursorY = plot.top + 7;
      tagItems.forEach((item) => {
        item.tagY = Math.max(item.desiredY, cursorY);
        cursorY = item.tagY + tagGap;
      });
      let limitY = plotBottom - 7;
      for (let i = tagItems.length - 1; i >= 0; i -= 1) {
        tagItems[i].tagY = Math.min(tagItems[i].tagY, limitY);
        limitY = tagItems[i].tagY - tagGap;
      }
      tagItems.forEach((item) => {
        const text = `${item.label} ${item.value.toFixed(2)}`;
        const tx = w - item.width - 4;
        const ty = item.tagY;
        if (Math.abs(ty - item.desiredY) > 0.5) {
          ctx.beginPath();
          ctx.strokeStyle = 'rgba(255,255,255,0.12)';
          ctx.lineWidth = 0.6;
          ctx.moveTo(plotRight, item.desiredY);
          ctx.lineTo(tx, ty);
          ctx.stroke();
        }
        ctx.fillStyle = item.fill;
        ctx.beginPath();
        if (typeof ctx.roundRect === 'function') ctx.roundRect(tx, ty - 6.5, item.width, 13, 4);
        else {
          const rr = 4;
          ctx.moveTo(tx + rr, ty - 6.5);
          ctx.lineTo(tx + item.width - rr, ty - 6.5);
          ctx.quadraticCurveTo(tx + item.width, ty - 6.5, tx + item.width, ty - 6.5 + rr);
          ctx.lineTo(tx + item.width, ty + 6.5 - rr);
          ctx.quadraticCurveTo(tx + item.width, ty + 6.5, tx + item.width - rr, ty + 6.5);
          ctx.lineTo(tx + rr, ty + 6.5);
          ctx.quadraticCurveTo(tx, ty + 6.5, tx, ty + 6.5 - rr);
          ctx.lineTo(tx, ty - 6.5 + rr);
          ctx.quadraticCurveTo(tx, ty - 6.5, tx + rr, ty - 6.5);
          ctx.closePath();
        }
        ctx.fill();
        ctx.strokeStyle = item.border;
        ctx.lineWidth = 0.8;
        ctx.stroke();
        ctx.fillStyle = item.text;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, tx + 5, ty + 0.5);
      });

      // Crosshair.
      if (this.mouse && this.hover !== null) {
        const mx = Math.max(plot.left, Math.min(plotRight, this.mouse.x));
        const my = Math.max(plot.top, Math.min(plotBottom, this.mouse.y));
        ctx.save();
        ctx.setLineDash([3, 4]);
        ctx.strokeStyle = 'rgba(255,255,255,0.16)';
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        ctx.moveTo(mx, plot.top);
        ctx.lineTo(mx, plotBottom);
        ctx.moveTo(plot.left, my);
        ctx.lineTo(plotRight, my);
        ctx.stroke();
        ctx.restore();
      }
    }
  }

  function drawSimpleLine(canvas, values, color) {
    if (!canvas) return;
    const parent = canvas.parentElement;
    const rect = parent.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = rect.width;
    const h = rect.height;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const nums = values.filter((v) => Number.isFinite(Number(v))).map(Number);
    if (!nums.length) return;
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    if (max - min < 0.0000001) {
      ctx.beginPath();
      ctx.setLineDash([2, 5]);
      ctx.strokeStyle = 'rgba(255,255,255,0.12)';
      ctx.lineWidth = 1;
      ctx.moveTo(0, h / 2);
      ctx.lineTo(w, h / 2);
      ctx.stroke();
      return;
    }
    const span = Math.max(max - min, Math.max(0.01, Math.abs(max) * 0.0005));
    const padX = 1;
    const padY = 4;
    const x = (i) => padX + (i / Math.max(1, nums.length - 1)) * (w - padX * 2);
    const y = (v) => padY + ((max - v) / span) * (h - padY * 2);
    if (nums.length < 2) {
      ctx.beginPath();
      ctx.setLineDash([2, 4]);
      ctx.strokeStyle = 'rgba(255,255,255,0.13)';
      ctx.moveTo(0, h / 2);
      ctx.lineTo(w, h / 2);
      ctx.stroke();
      return;
    }
    ctx.beginPath();
    nums.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    const fill = ctx.createLinearGradient(0, 0, 0, h);
    fill.addColorStop(0, `${color}28`);
    fill.addColorStop(1, `${color}00`);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.beginPath();
    nums.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.beginPath();
    ctx.fillStyle = color;
    ctx.arc(x(nums.length - 1), y(nums[nums.length - 1]), 1.8, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawReportLine(canvas, points, color) {
    if (!canvas || !points || !points.length) return;
    const rect = canvas.parentElement.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = rect.width;
    const h = rect.height;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const nums = points.map((p) => Number(p.equity)).filter(Number.isFinite);
    if (nums.length < 2) return;
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const pad = Math.max((max - min) * 0.12, 1);
    const lo = min - pad;
    const hi = max + pad;
    const left = 45;
    const right = 10;
    const top = 10;
    const bottom = 22;
    const pw = w - left - right;
    const ph = h - top - bottom;
    const x = (i) => left + (i / (nums.length - 1)) * pw;
    const y = (v) => top + ((hi - v) / (hi - lo)) * ph;
    ctx.font = '8.5px "SFMono-Regular", "SF Mono", ui-monospace, Menlo, monospace';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i += 1) {
      const py = top + (ph * i) / 4;
      const val = hi - ((hi - lo) * i) / 4;
      ctx.beginPath();
      ctx.strokeStyle = 'rgba(255,255,255,0.05)';
      ctx.lineWidth = 0.5;
      ctx.moveTo(left, py + 0.5);
      ctx.lineTo(w - right, py + 0.5);
      ctx.stroke();
      ctx.fillStyle = 'rgba(139,147,163,0.75)';
      ctx.fillText(`$${Math.round(val).toLocaleString()}`, left - 7, py);
    }
    ctx.textBaseline = 'top';
    for (let i = 0; i <= 3; i += 1) {
      const idx = Math.min(nums.length - 1, Math.round(((nums.length - 1) * i) / 3));
      const d = new Date(points[idx].time * 1000);
      ctx.fillStyle = 'rgba(139,147,163,0.6)';
      ctx.textAlign = i === 0 ? 'left' : i === 3 ? 'right' : 'center';
      ctx.fillText(d.toLocaleDateString([], { month: 'short', day: 'numeric' }), x(idx), h - bottom + 6);
    }
    ctx.beginPath();
    nums.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.lineTo(x(nums.length - 1), top + ph);
    ctx.lineTo(x(0), top + ph);
    ctx.closePath();
    const fill = ctx.createLinearGradient(0, top, 0, top + ph);
    fill.addColorStop(0, `${color}34`);
    fill.addColorStop(1, `${color}00`);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.beginPath();
    nums.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();
  }

  // ---------------------------------------------------------------- //
  // Rendering                                                         //
  // ---------------------------------------------------------------- //
  function renderAll() {
    if (!state.data) return;
    renderHeader();
    renderAccount();
    renderMarketHeader();
    renderSignal();
    renderRisk();
    renderPositions();
    renderPerformance();
    renderBacktestCard();
    renderActivity();
    renderFooter();
    updateChartOverlays();
    renderSparks();
  }

  function renderHeader() {
    const data = state.data;
    const conn = data.connection || {};
    const meta = data.meta || {};
    const bot = data.bot || {};
    const market = data.market || {};
    const live = meta.is_live;
    const terminalName = brokerName(conn.mode);
    const terminalShort = brokerShort(conn.mode);
    const armed = !!conn.armed;

    const autoButton = el.btnAuto;
    if (autoButton) {
      const running = !!bot.running;
      const halted = !!bot.kill_switch;
      autoButton.classList.toggle('running', running);
      autoButton.classList.toggle('halted', halted);
      autoButton.disabled = halted || !!state.networkDown;
      let main = 'Start auto';
      let short = 'Start';
      if (running) { main = 'Pause auto'; short = 'Pause'; }
      else if (halted) { main = 'Halted'; short = 'Halted'; }
      else if (live && !armed) { main = 'Arm to trade'; short = 'Arm'; }
      else if (state.networkDown) { main = 'Offline'; short = 'Offline'; }
      el.autoLabel.innerHTML = `<span class="auto-full">${esc(main)}</span><span class="auto-short">${esc(short)}</span>`;
      autoButton.setAttribute('aria-label', running ? 'Pause autonomous trading' : 'Start autonomous trading');
      autoButton.title = halted
        ? 'Kill switch active — clear the halt in the risk card'
        : !conn.connected
          ? 'Broker disconnected — open connection settings'
          : running ? 'Pause the autonomous engine' : 'Start autonomous trading';
    }

    if (el.btnHalt) {
      el.btnHalt.classList.toggle('halted', !!bot.kill_switch);
      el.btnHalt.title = bot.kill_switch ? 'Trading is halted — clear the halt in the risk card' : 'Emergency halt — stop all new orders';
    }

    // Market session.
    const isOpen = market.session === 'open';
    setText('#market-label', state.networkDown ? 'NO LINK' : isOpen ? 'MARKET OPEN' : 'MARKET CLOSED');
    const marketDot = $('#market-dot');
    if (marketDot) marketDot.className = `dot ${state.networkDown ? 'danger' : isOpen ? 'live' : 'warn'}`;
    $('#market-pill').title = `${data.meta.symbol || 'XAUUSD'} · ${isOpen ? 'market open' : 'market closed'}`;

    // Connection pill.
    let connLabel = 'PAPER · SIM';
    let connDot = 'gold';
    if (live) {
      if (!conn.connected) { connLabel = `${terminalShort} · OFFLINE`; connDot = 'danger'; }
      else if (armed) { connLabel = `${terminalShort} · ARMED`; connDot = 'danger'; }
      else { connLabel = `${terminalShort} · VIEW`; connDot = 'warn'; }
    }
    if (state.networkDown) { connLabel = 'ENGINE OFFLINE'; connDot = 'danger'; }
    setText('#conn-label', connLabel);
    const connDotEl = $('#conn-dot');
    if (connDotEl) connDotEl.className = `dot ${connDot}`;
    const connButton = el.btnConnection;
    if (connButton) connButton.title = live
      ? `${terminalName} · ${armed ? 'armed for live execution' : 'view-only'}`
      : 'Paper simulator · click to connect MetaTrader 4 or 5';

    // Environment banner.
    const banner = el.envBanner;
    if (banner) {
      banner.classList.remove('live', 'offline');
      if (!live) {
        setText(el.envTitle, 'Simulation');
        setText(el.envText, 'Synthetic XAU/USD feed. No real capital is at risk.');
        el.envAction.innerHTML = 'Connect terminal ' + icon('chevron-right', 'icon icon-sm');
      } else if (!conn.connected) {
        banner.classList.add('offline');
        setText(el.envTitle, `${terminalShort} offline`);
        setText(el.envText, 'Account data is stale. Reconnect before starting the engine.');
        el.envAction.innerHTML = 'Reconnect ' + icon('chevron-right', 'icon icon-sm');
      } else if (armed) {
        banner.classList.add('live');
        setText(el.envTitle, 'Live capital');
        setText(el.envText, 'Real orders are enabled. The risk governor still applies to every trade.');
        el.envAction.innerHTML = 'Manage connection ' + icon('chevron-right', 'icon icon-sm');
      } else {
        banner.classList.add('live');
        setText(el.envTitle, 'View only');
        setText(el.envText, `${terminalName} connected. Arm the session to enable execution.`);
        el.envAction.innerHTML = 'Arm session ' + icon('chevron-right', 'icon icon-sm');
      }
    }

    if (el.btnAnalyze) el.btnAnalyze.disabled = !conn.connected || !!state.networkDown;
  }

  function renderAccount() {
    const account = state.data.account;
    const risk = state.data.risk || {};
    const meta = state.data.meta || {};
    const conn = state.data.connection || {};
    if (!account) {
      setText('#account-equity', '—');
      setText('#account-pnl', '—');
      setText('#account-pnl-pct', 'today');
      setText('#account-balance', '—');
      setText('#account-free-margin', '—');
      setText('#account-margin-level', '—');
      setText('#account-server', `Awaiting ${brokerName(conn.mode)} connection`);
      setText('#account-login', '—');
      setText('#account-sub', meta.is_live ? `${brokerName(conn.mode)} · offline` : 'Paper · AURUM Simulator');
      const dot = el.accountDot;
      if (dot) dot.className = 'dot danger';
      drawSimpleLine(el.equitySpark, [], '#e6b86a');
      return;
    }
    setText('#account-equity', money(account.equity));
    setText('#account-sub', `${account.account_type === 'real' ? 'Live account' : account.account_type === 'demo' ? 'Demo account' : 'Account'} · ${account.broker || account.server || 'Broker'}`);
    setText('#account-balance', money(account.balance));
    setText('#account-free-margin', money(account.free_margin));
    setText('#account-margin-level', account.margin_level > 0 ? `${account.margin_level.toFixed(1)}%` : '—');
    setText('#account-server', account.server || '—');
    setText('#account-login', account.login ? `#${account.login}` : '—');

    const dailyPnl = Number(risk.daily_pnl || 0);
    const dayStart = Number(account.equity) - dailyPnl;
    const dailyPct = dayStart > 0 ? (dailyPnl / dayStart) * 100 : 0;
    setText('#account-pnl', `${dailyPnl >= 0 ? '+' : '−'}${money(Math.abs(dailyPnl))}`);
    setText('#account-pnl-pct', `${pct(dailyPct, 2, true)} today`);
    const delta = el.accountDelta;
    if (delta) {
      delta.classList.remove('up', 'down', 'flat');
      delta.classList.add(dailyPnl > 0 ? 'up' : dailyPnl < 0 ? 'down' : 'flat');
    }
    const type = el.accountType;
    if (type) {
      type.textContent = account.account_type === 'real' ? 'LIVE' : account.account_type === 'demo' ? 'DEMO' : String(account.account_type || '—').toUpperCase();
      type.className = `badge ${account.account_type === 'real' ? 'badge-sell' : ''}`;
    }
    const dot = el.accountDot;
    if (dot) dot.className = `dot ${conn.connected ? 'live' : 'danger'}`;
    const series = (state.data.equity || []).map((p) => Number(p.equity));
    if (series.length < 2) series.push(Number(account.balance));
    drawSimpleLine(el.equitySpark, series, dailyPnl >= 0 ? '#3ddba0' : '#ff6477');
  }

  function latestCandle() {
    return state.candles.length ? state.candles[state.candles.length - 1] : null;
  }

  function updateOhlc(candle) {
    const c = candle || latestCandle();
    if (!c) return;
    setText('#o-value', price(c.open));
    setText('#h-value', price(c.high));
    setText('#l-value', price(c.low));
    setText('#c-value', price(c.close));
  }

  function dayChange() {
    const market = state.data.market || {};
    if (Number.isFinite(Number(market.change_pct)) && market.change_pct !== null) {
      return { pct: Number(market.change_pct), change: Number(market.change || 0) };
    }
    if (!state.candles.length) return null;
    const todayKey = new Date().toISOString().slice(0, 10);
    const todayCandles = state.candles.filter((c) => new Date(c.time * 1000).toISOString().slice(0, 10) === todayKey);
    const first = todayCandles.length ? todayCandles[0] : state.candles[0];
    const quote = state.data.quote;
    const last = quote && Number.isFinite(Number(quote.mid))
      ? Number(quote.mid) : Number(state.candles[state.candles.length - 1].close);
    const open = Number(first.open);
    if (!Number.isFinite(open) || !open) return null;
    return { change: last - open, pct: ((last - open) / open) * 100 };
  }

  function renderMarketHeader() {
    const quote = state.data.quote;
    const market = state.data.market || {};
    const conn = state.data.connection || {};
    const c = latestCandle();
    const mid = quote && Number.isFinite(Number(quote.mid)) ? Number(quote.mid) : c ? Number(c.close) : null;
    setText('#hero-price', price(mid));
    if (c && quote && Number.isFinite(Number(quote.mid))) {
      c.close = Number(quote.mid);
      c.high = Math.max(Number(c.high), c.close);
      c.low = Math.min(Number(c.low), c.close);
    }
    const change = dayChange();
    const changeEl = el.heroChange;
    if (changeEl) {
      changeEl.classList.remove('up', 'down', 'flat');
      if (!change) {
        changeEl.classList.add('flat');
        changeEl.textContent = '—';
      } else {
        const up = change.pct >= 0;
        changeEl.classList.add(up ? 'up' : 'down');
        changeEl.textContent = `${up ? '▲' : '▼'} ${pct(Math.abs(change.pct), 2)}`;
        changeEl.title = `${change.change >= 0 ? '+' : ''}${price(change.change)} today`;
      }
    }
    setText('#quote-spread', quote ? `bid ${price(quote.bid)} · ask ${price(quote.ask)} · spread ${price(quote.spread)}` : 'spread —');
    setText('#chart-session', market.session === 'open' ? 'OPEN' : 'CLOSED');
    setText('#chart-feed-type', state.data.meta && state.data.meta.is_live ? 'MT5 feed' : 'Simulated feed');
    setText('#chart-source', state.data.meta && state.data.meta.is_live ? 'MetaTrader 5' : 'Simulated feed');
    setText('#chart-updated', fmtTime(quote ? quote.time : c ? new Date(c.time * 1000) : null, true));
    const latency = Number(conn.latency_ms || 0);
    setText('#chart-latency', `${latency}ms`);
    const latencyDot = $('#chart-latency-pill .dot');
    if (latencyDot) latencyDot.className = `dot ${conn.connected ? (latency > 500 ? 'warn' : 'live') : 'danger'}`;
    const high = state.candles.length ? Math.max(...state.candles.map((x) => Number(x.high))) : null;
    const low = state.candles.length ? Math.min(...state.candles.map((x) => Number(x.low))) : null;
    setText('#chart-range', high !== null ? `H ${price(high)} · L ${price(low)}` : '—');
    if (!state.hoverCandle) updateOhlc(c);
    if (state.chart) updateChartOverlays();
  }

  function updateChartOverlays() {
    if (!state.chart || !state.data) return;
    state.chart.setOverlays(state.data.positions || [], state.data.signal || null);
  }

  function renderSignal() {
    const signal = state.data.signal;
    const card = el.signalCard;
    if (!signal) {
      if (card) card.dataset.side = 'wait';
      if (el.signalContent) el.signalContent.hidden = true;
      if (el.signalEmpty) el.signalEmpty.hidden = false;
      setText('#signal-status', 'WAITING');
      const badge = $('#signal-status');
      if (badge) badge.className = 'badge badge-wait';
      setText('#signal-sub', `${state.data.meta.symbol || 'XAUUSD'} · scanning`);
      return;
    }
    if (el.signalContent) el.signalContent.hidden = false;
    if (el.signalEmpty) el.signalEmpty.hidden = true;
    const action = (signal.action || 'WAIT').toUpperCase();
    const side = action === 'BUY' ? 'buy' : action === 'SELL' ? 'sell' : 'wait';
    if (card) card.dataset.side = side;
    setText('#signal-action', action);
    setText('#signal-sub', `${signal.symbol || 'XAU/USD'} · ${signal.timeframe || 'M5'}`);
    setText('#signal-symbol', signal.symbol || 'XAU/USD');
    setText('#signal-regime', signal.regime || '—');
    setText('#signal-age', timeAgo(signal.created_at));
    const confidence = Math.max(0, Math.min(1, Number(signal.confidence || 0)));
    setText('#signal-conf', Math.round(confidence * 100));
    if (el.signalRing) el.signalRing.style.setProperty('--value', String(Math.round(confidence * 100)));
    const status = String(signal.status || 'wait').toLowerCase();
    const statusMap = {
      executed: ['EXECUTED', 'badge badge-buy'],
      ready: ['READY', 'badge badge-gold'],
      blocked: ['BLOCKED', 'badge badge-sell'],
      error: ['REJECTED', 'badge badge-sell'],
      wait: ['WAIT', 'badge badge-wait'],
      new: ['READY', 'badge badge-gold'],
    };
    const info = statusMap[status] || ['WAIT', 'badge badge-wait'];
    setText('#signal-status', info[0]);
    const statusBadge = $('#signal-status');
    if (statusBadge) statusBadge.className = info[1];
    const reasons = Array.isArray(signal.reasons) && signal.reasons.length ? signal.reasons : ['No qualifying setup.'];
    if (el.signalReasons) {
      el.signalReasons.innerHTML = reasons.slice(0, 3).map((reason) => `<li>${esc(reason)}</li>`).join('');
    }
    setText('#signal-entry', action === 'WAIT' ? '—' : price(signal.entry));
    setText('#signal-sl', action === 'WAIT' ? '—' : price(signal.sl));
    setText('#signal-tp', action === 'WAIT' ? '—' : price(signal.tp));
    let rr = null;
    if (signal.entry && Number(signal.sl) && Number(signal.tp)) {
      rr = Math.abs(Number(signal.tp) - Number(signal.entry)) / Math.max(0.0001, Math.abs(Number(signal.entry) - Number(signal.sl)));
    }
    setText('#signal-rr', rr ? `${rr.toFixed(1)}R` : '—');
    const features = signal.features || {};
    setText('#signal-adx', Number.isFinite(Number(features.adx)) ? Number(features.adx).toFixed(1) : '—');
    setText('#signal-atr', Number.isFinite(Number(features.atr)) ? Number(features.atr).toFixed(2) : '—');
    setText('#signal-spread', Number.isFinite(Number(features.spread)) ? Number(features.spread).toFixed(2) : '—');
    const ai = signal.ai || {};
    let aiLabel = 'Quant core';
    if (ai.used) {
      const rationale = String(ai.rationale || '').trim();
      aiLabel = rationale ? `AI ${ai.bias || 'review'} · ${rationale}` : `AI ${ai.bias || 'review'}`;
    }
    else if (ai.status === 'not_configured') aiLabel = 'Quant core · AI offline';
    else if (ai.status === 'disabled') aiLabel = 'Quant core · AI off';
    else if (ai.status === 'error') aiLabel = 'Quant core · AI error';
    setText('#signal-ai', aiLabel);
    const aiNode = $('#signal-ai');
    if (aiNode) {
      aiNode.title = [ai.rationale, ai.detail, ai.status].filter(Boolean).join(' · ') || 'Deterministic technical model';
    }
  }

  function renderRisk() {
    const risk = state.data.risk || {};
    const checks = Array.isArray(risk.checks) ? risk.checks : [];
    const failures = checks.filter((c) => !c.passed);
    const hardFailures = failures.filter((c) => !SOFT_CHECKS.has(c.key));
    const live = state.data.meta && state.data.meta.is_live;
    const armed = state.data.connection && state.data.connection.armed;
    const kill = !!(state.data.bot && state.data.bot.kill_switch);
    const allowed = !!risk.allowed;
    const card = el.riskCard;
    if (card) card.dataset.allowed = allowed ? 'true' : 'false';

    let badgeText = 'CLEAR';
    let badgeClass = 'badge badge-buy';
    let title = 'Guardrails clear';
    let detail = 'All execution checks pass. The engine may size a position.';
    if (kill) {
      badgeText = 'HALTED'; badgeClass = 'badge badge-sell'; title = 'Emergency halt active';
      detail = 'No new orders will be accepted until the halt is cleared.';
    } else if (!state.data.connection || !state.data.connection.connected) {
      badgeText = 'OFFLINE'; badgeClass = 'badge badge-sell'; title = 'Broker link offline';
      detail = 'Reconnect before the engine can evaluate a setup.';
    } else if (live && !armed) {
      badgeText = 'VIEW ONLY'; badgeClass = 'badge badge-wait'; title = 'Execution is disarmed';
      detail = `This ${brokerName((state.data.connection || {}).mode)} connection is read-only. Arm the session to enable execution.`;
    } else if (!allowed && hardFailures.length === 0) {
      badgeText = 'STANDBY'; badgeClass = 'badge badge-wait'; title = 'Standing by';
      detail = failures.length ? `${labelForCheck(failures[0])} — ${failures[0].detail}` : 'No executable setup is present.';
    } else if (!allowed && hardFailures.every((check) => check.key === 'slots') && (state.data.positions || []).length > 0) {
      badgeText = 'IN POSITION'; badgeClass = 'badge badge-gold'; title = 'Managing open position';
      detail = `${(state.data.positions || []).length} position(s) open. New entries wait until the book is flat.`;
    } else if (!allowed) {
      const first = hardFailures[0] || failures[0];
      badgeText = 'BLOCKED'; badgeClass = 'badge badge-sell'; title = 'Entries blocked';
      detail = first ? `${labelForCheck(first)} — ${first.detail}` : 'A risk guardrail is blocking execution.';
    }
    setText('#risk-badge', badgeText);
    const badge = $('#risk-badge');
    if (badge) badge.className = badgeClass;
    setText('#risk-title', title);
    setText('#risk-detail', detail);
    const dailyPct = Number(risk.daily_loss_pct || 0);
    const dailyLimit = Number(risk.daily_limit_pct || 0);
    const ddPct = Number(risk.drawdown_pct || 0);
    const ddLimit = Number(risk.drawdown_limit_pct || 0);
    setText('#risk-daily', `${dailyPct.toFixed(2)} / ${dailyLimit.toFixed(2)}%`);
    setText('#risk-dd', `${ddPct.toFixed(2)} / ${ddLimit.toFixed(2)}%`);
    const dailyFill = $('#risk-daily-fill');
    if (dailyFill) {
      dailyFill.style.width = `${Math.min(100, dailyLimit ? (dailyPct / dailyLimit) * 100 : 0)}%`;
      const meter = dailyFill.parentElement;
      if (meter) meter.className = `meter ${dailyPct / Math.max(0.001, dailyLimit) > 0.8 ? 'danger' : 'green'}`;
    }
    const ddFill = $('#risk-dd-fill');
    if (ddFill) {
      ddFill.style.width = `${Math.min(100, ddLimit ? (ddPct / ddLimit) * 100 : 0)}%`;
      const meter = ddFill.parentElement;
      if (meter) meter.className = `meter ${ddPct / Math.max(0.001, ddLimit) > 0.55 ? 'danger' : ''}`;
    }
    setText('#risk-budget', money(risk.risk_budget));
    if (el.riskChecks) {
      const order = live
        ? ['kill_switch', 'connection', 'live_arm', 'market_hours', 'spread', 'setup', 'slots', 'daily_loss', 'drawdown', 'cooldown', 'session', 'sizing', 'reward_risk', 'freshness']
        : ['kill_switch', 'connection', 'market_hours', 'spread', 'setup', 'slots', 'daily_loss', 'drawdown', 'cooldown', 'session', 'sizing', 'reward_risk', 'freshness'];
      const sorted = checks.filter((check) => live || check.key !== 'live_arm')
        .slice().sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
      const visible = sorted.slice(0, 10);
      el.riskChecks.innerHTML = visible.map((check) => `
        <div class="risk-check ${check.passed ? '' : 'fail'}" title="${esc(check.detail)}">
          <span class="check-dot">${icon(check.passed ? 'check' : 'x', 'icon')}</span>
          <span class="check-name">${esc(labelForCheck(check))}</span>
        </div>`).join('');
    }
    if (el.btnClearHalt) el.btnClearHalt.hidden = !kill;
  }

  function labelForCheck(check) {
    return (check && (RISK_LABELS[check.key] || check.label)) || 'Check';
  }

  function renderPositions() {
    const positions = Array.isArray(state.data.positions) ? state.data.positions : [];
    const rows = positions.map((p, index) => {
      const pnl = Number(p.pnl || 0);
      const up = pnl >= 0;
      const side = String(p.side || '').toUpperCase();
      const closeDisabled = false;
      return `<tr>
        <td><span class="side-cell ${side === 'BUY' ? 'side-buy' : 'side-sell'}">${side === 'BUY' ? '▲' : '▼'} ${esc(side)}</span><div class="ticket">#${esc(p.ticket || '')}</div></td>
        <td class="num">${Number(p.lots || 0).toFixed(2)}</td>
        <td><div class="level-pair"><span>${price(p.entry_price)}</span><span>${price(p.current_price)}</span></div></td>
        <td><div class="level-pair"><span><em>SL</em> ${price(p.sl)}</span><span><em>TP</em> ${price(p.tp)}</span></div></td>
        <td class="num pnl-strong ${up ? 'up' : 'down'}">${signedMoney(pnl)}</td>
        <td><button class="close-trade" type="button" data-close-ticket="${esc(p.ticket || '')}" ${closeDisabled ? 'disabled' : ''}>Close</button></td>
      </tr>`;
    }).join('');
    if (el.positionsBody) el.positionsBody.innerHTML = rows;
    if (el.positionsTable) el.positionsTable.style.display = positions.length ? '' : 'none';
    if (el.positionsEmpty) el.positionsEmpty.hidden = positions.length > 0;
    setText('#positions-count', positions.length);
    setText('#positions-pnl', signedMoney(positions.reduce((sum, p) => sum + Number(p.pnl || 0), 0)));
    const exposure = positions.reduce((sum, p) => sum + Number(p.lots || 0) * 100 * Number(p.current_price || 0), 0);
    setText('#positions-exposure', money(exposure));
    const riskAtStop = positions.reduce((sum, p) => sum + Math.abs(Number(p.entry_price || 0) - Number(p.sl || 0)) * Number(p.lots || 0) * 100, 0);
    setText('#positions-risk', money(riskAtStop));
    setText('#positions-open-risk', money(riskAtStop));
    const autoBrackets = state.data.settings && state.data.settings.auto_adjust_brackets;
    setText('#bracket-mode', autoBrackets ? 'Auto SL/TP active' : 'Static SL/TP');
    const bracketMode = $('#bracket-mode');
    if (bracketMode) {
      bracketMode.title = autoBrackets
        ? 'Trailing stop and trend-confirmed target extension are enabled'
        : 'Stops and targets stay at their entry values';
    }
    if (el.btnFlatten) {
      el.btnFlatten.disabled = positions.length === 0;
      const liveUnarmed = state.data.meta && state.data.meta.is_live && !(state.data.connection && state.data.connection.armed);
      el.btnFlatten.title = liveUnarmed ? 'Arm the live session before flattening' : 'Close every open position';
    }

    const trades = Array.isArray(state.data.trades) ? state.data.trades : [];
    const historyRows = trades.map((t) => {
      const side = String(t.side || '').toUpperCase();
      const pnl = Number(t.pnl || 0);
      return `<tr>
        <td><div class="level-pair"><span>${esc(fmtDate(t.closed_at))}</span><span>${esc(fmtTime(t.closed_at))}</span></div></td>
        <td><span class="side-cell ${side === 'BUY' ? 'side-buy' : 'side-sell'}">${side === 'BUY' ? '▲' : '▼'} ${esc(side)}</span></td>
        <td class="num">${Number(t.lots || 0).toFixed(2)}</td>
        <td class="num">${price(t.entry_price)} → ${price(t.exit_price)}</td>
        <td class="num pnl-strong ${pnl >= 0 ? 'up' : 'down'}">${signedMoney(pnl)}</td>
        <td style="font-size:9.5px;color:var(--muted);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.close_reason || '')}">${esc(t.close_reason || '—')}</td>
      </tr>`;
    }).join('');
    if (el.historyBody) el.historyBody.innerHTML = historyRows;
    if (el.historyTable) el.historyTable.style.display = trades.length ? '' : 'none';
    if (el.historyEmpty) el.historyEmpty.hidden = trades.length > 0;
    setText('#history-count', trades.length);
    setPanelVisibility();
  }

  function setPanelVisibility() {
    const positionsActive = state.activeTab === 'positions';
    if (el.panelPositions) el.panelPositions.hidden = !positionsActive;
    if (el.panelHistory) el.panelHistory.hidden = positionsActive;
    if (el.tabPositions) {
      el.tabPositions.classList.toggle('active', positionsActive);
      el.tabPositions.setAttribute('aria-selected', positionsActive ? 'true' : 'false');
    }
    if (el.tabHistory) {
      el.tabHistory.classList.toggle('active', !positionsActive);
      el.tabHistory.setAttribute('aria-selected', !positionsActive ? 'true' : 'false');
    }
  }

  function renderPerformance() {
    const perf = state.data.performance || {};
    const count = Number(perf.trades || 0);
    const net = Number(perf.net_pnl || 0);
    const netEl = el.perfPnl;
    if (netEl) {
      netEl.textContent = count ? signedMoney(net) : '—';
      netEl.classList.remove('up', 'down', 'flat');
      netEl.classList.add(count ? (net > 0 ? 'up' : net < 0 ? 'down' : 'flat') : 'flat');
    }
    setText('#perf-win', count ? `${Number(perf.win_rate_pct || 0).toFixed(1)}%` : '—');
    setText('#perf-pf', count ? (Number(perf.profit_factor) >= 999 ? '∞' : Number(perf.profit_factor || 0).toFixed(2)) : '—');
    setText('#perf-trades', count || '—');
    setText('#perf-avg-win', count ? money(perf.avg_win) : '—');
    setText('#perf-avg-loss', count ? money(perf.avg_loss) : '—');
    setText('#perf-expectancy', count ? signedMoney(perf.expectancy) : '—');
    const mode = state.data.meta.is_live ? 'live account' : 'paper';
    setText('#perf-source', `Lifetime · ${mode}${count ? ` · ${pct(perf.return_pct, 2, true)}` : ''}`);
    setText('#perf-mode', state.data.meta.is_live ? 'LIVE' : 'PAPER');
    const badge = $('#perf-mode');
    if (badge) badge.className = `badge ${state.data.meta.is_live ? 'badge-sell' : 'badge-gold'}`;
    if (el.perfPnl) el.perfPnl.title = count ? `Net realised P&L across ${count} closed trades` : 'No closed trades yet';
  }

  function backtestSummary() {
    if (state.currentBacktest && state.currentBacktest.summary) return state.currentBacktest.summary;
    return state.data && state.data.backtest ? state.data.backtest : null;
  }

  function renderBacktestCard() {
    if (state.backtestRunning) return;
    const summary = backtestSummary();
    if (el.btLoading) el.btLoading.hidden = true;
    if (!summary) {
      if (el.btEmpty) el.btEmpty.hidden = false;
      if (el.btResult) el.btResult.hidden = true;
      setText('#bt-badge', 'NO TEST');
      setText('#bt-meta', 'Synthetic data · not a forecast');
      if (el.btnBtReport) el.btnBtReport.hidden = true;
      if (el.btEmpty) el.btEmpty.textContent = 'No backtest this session. Run a test to see return, drawdown and trade quality.';
      return;
    }
    if (el.btEmpty) el.btEmpty.hidden = true;
    if (el.btResult) el.btResult.hidden = false;
    const update = summary.return_pct >= 0;
    const returnEl = el.btReturn;
    if (returnEl) {
      returnEl.textContent = pct(summary.return_pct, 2, true);
      returnEl.className = `value ${update ? 'up' : 'down'}`;
    }
    if (el.btDd) el.btDd.textContent = `${Number(summary.max_drawdown_pct || 0).toFixed(2)}%`;
    if (el.btTrades) el.btTrades.textContent = String(summary.trades ?? '—');
    if (el.btPf) el.btPf.textContent = Number(summary.profit_factor) >= 999 ? '∞' : Number(summary.profit_factor || 0).toFixed(2);
    const full = state.currentBacktest;
    const meta = state.data.backtest_meta || {};
    const request = (full && full.request) || meta.request || {};
    const source = (full && full.data_source) || meta.data_source || 'synthetic';
    const imported = source === 'mt5_csv';
    const days = request.days || state.data.settings.timeframe;
    const timeframe = request.timeframe || 'M5';
    setText('#bt-badge', imported ? 'MT5 CSV' : `${days}D TEST`);
    const badge = $('#bt-badge');
    if (badge) badge.className = imported ? 'badge badge-buy' : 'badge badge-gold';
    setText('#bt-sub', `Quant core + dynamic brackets · ${timeframe}`);
    setText('#bt-meta', imported
      ? `Imported MT5 history · ${summary.trades} trades · simulated fills`
      : `${summary.trades} trades · max DD ${Number(summary.max_drawdown_pct || 0).toFixed(1)}% · simulated`);
    if (el.btnBtReport) el.btnBtReport.hidden = !full && !state.data.backtest;
    if (full && full.equity) {
      const points = full.equity.map((p) => Number(p.equity));
      requestAnimationFrame(() => drawSimpleLine(el.btSpark, points, update ? '#3ddba0' : '#ff6477'));
    }
  }

  function renderActivity() {
    const logs = Array.isArray(state.data.logs) ? state.data.logs.slice(-5).reverse() : [];
    if (el.activityList) {
      if (!logs.length) {
        el.activityList.innerHTML = `<li class="activity-item"><span class="activity-dot"></span><div class="activity-main"><div class="activity-msg">Waiting for engine events…</div></div></li>`;
      } else {
        el.activityList.innerHTML = logs.map((log) => {
          const level = String(log.level || '').toUpperCase();
          const event = String(log.event || '');
          const success = ['order_executed', 'trade_closed', 'bot_started', 'paper_reset'].includes(event);
          const dotClass = level === 'ERROR' ? 'error' : level === 'WARN' ? 'warn' : success ? 'success' : 'info';
          return `<li class="activity-item">
            <span class="activity-dot ${dotClass}"></span>
            <div class="activity-main">
              <div class="activity-msg" title="${esc(log.message || '')}">${esc(log.message || '')}</div>
              <div class="activity-time">${esc(fmtTime(log.ts, true))} · ${esc(event.replace(/_/g, ' '))}</div>
            </div>
          </li>`;
        }).join('');
      }
    }
    setText('#activity-count', logs.length);
    const activityPillDot = $('#activity-state') && $('#activity-state').parentElement.querySelector('.dot');
    if (activityPillDot) activityPillDot.className = `dot ${state.data.bot.running ? 'live' : 'idle'}`;
    setText('#activity-state', state.data.bot.running ? 'AUTO' : 'IDLE');
  }

  function renderFooter() {
    const meta = state.data.meta || {};
    const bot = state.data.bot || {};
    const conn = state.data.connection || {};
    const live = meta.is_live;
    const terminalShort = brokerShort(conn.mode);
    let modeText = 'SIMULATED';
    if (state.networkDown) modeText = 'OFFLINE';
    else if (live && !conn.connected) modeText = `${terminalShort} OFFLINE`;
    else if (live && conn.armed) modeText = 'LIVE';
    else if (live) modeText = `${terminalShort} VIEW`;
    setText('#footer-mode', modeText);
    setText('#footer-latency', `${Number(conn.latency_ms || 0)} ms`);
    let scan = 'Engine idle';
    if (bot.kill_switch) scan = 'Halted';
    else if (bot.running) scan = `Scanning every ${meta.scan_interval || 20}s`;
    else scan = 'Auto paused';
    setText('#footer-scan', scan);
    setText('#footer-clock', new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  }

  function renderSparks() {
    if (!state.data) return;
    const series = (state.data.equity || []).map((p) => Number(p.equity));
    if (series.length && series.length < 2 && state.data.account) series.push(Number(state.data.account.balance));
    const risk = state.data.risk || {};
    const color = Number(risk.daily_pnl || 0) >= 0 ? '#3ddba0' : '#ff6477';
    if (el.equitySpark) drawSimpleLine(el.equitySpark, series, color);
  }

  function updateChartTimeframeButtons() {
    $$('.tf-btn').forEach((button) => button.classList.toggle('active', button.dataset.timeframe === state.timeframe));
  }

  function tickClock() {
    setText('#clock-utc', fmtClockUTC());
    if (!state.data) return;
    if (state.data.signal) setText('#signal-age', timeAgo(state.data.signal.created_at));
    if (state.data.signal && state.data.signal.status === 'executed') {
      // Execution status is intentionally stable; time-ago still updates.
    }
    if (state.data.signal) setText('#signal-age', timeAgo(state.data.signal.created_at));
  }

  function setTimeframe(tf) {
    if (!tf || state.timeframe === tf) return;
    state.timeframe = tf;
    updateChartTimeframeButtons();
    fetchCandles();
  }

  // ---------------------------------------------------------------- //
  // Interaction helpers                                               //
  // ---------------------------------------------------------------- //
  function setButtonBusy(button, busy, label = 'Working…') {
    if (!button) return;
    if (busy) {
      if (button.dataset.busy === '1') return;
      button.dataset.busy = '1';
      button.classList.add('btn-loading');
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      if (!button.dataset.originalAriaLabel) button.dataset.originalAriaLabel = button.getAttribute('aria-label') || '';
      const spinner = document.createElement('span');
      spinner.className = 'spin';
      spinner.dataset.busySpinner = '1';
      button.prepend(spinner);
      if (label) button.setAttribute('aria-label', label);
    } else {
      delete button.dataset.busy;
      button.classList.remove('btn-loading');
      button.removeAttribute('aria-busy');
      $$('[data-busy-spinner]', button).forEach((node) => node.remove());
      button.disabled = false;
      if (button.dataset.originalAriaLabel) {
        button.setAttribute('aria-label', button.dataset.originalAriaLabel);
        delete button.dataset.originalAriaLabel;
      } else {
        button.removeAttribute('aria-label');
      }
    }
  }

  async function perform(button, fn, loadingLabel = 'Working…') {
    setButtonBusy(button, true, loadingLabel);
    try {
      const result = await fn();
      if (result && result.state) applyState(result.state);
      return result;
    } catch (error) {
      if (error.status !== 401) toast('Action failed', error.message, 'error');
      return null;
    } finally {
      setButtonBusy(button, false);
      if (state.data) renderHeader();
    }
  }

  async function handleAuto() {
    const data = state.data;
    if (!data) return;
    const bot = data.bot || {};
    if (bot.running) {
      await perform(el.btnAuto, () => api('/api/bot/pause', { method: 'POST', body: {} }), 'Pausing…');
      if (state.data && !state.data.bot.running) toast('Engine paused', 'Open positions are still protected by their brackets.', 'info');
      return;
    }
    if (bot.kill_switch) {
      toast('Kill switch active', 'Clear the emergency halt in the risk governor before starting.', 'error');
      return;
    }
    const live = data.meta && data.meta.is_live;
    const conn = data.connection || {};
    if (!conn.connected || (live && !conn.armed)) {
      openConnectionModal();
      if (live && !conn.armed) toast('Arm the live session', 'Live execution is view-only until you arm it.', 'info');
      return;
    }
    if (live) {
      const confirmed = await openConfirm({
        title: 'Start live autonomous trading?',
        subtitle: 'Real orders will be routed to MetaTrader 5.',
        message: 'The engine will scan for setups and place <b>real market orders</b> with stop-loss and take-profit brackets. The risk governor remains active, but losses are real. You can halt at any time.',
        confirmLabel: 'Start live engine',
        destructive: true,
      });
      if (!confirmed) return;
    }
    const result = await perform(el.btnAuto, () => api('/api/bot/start', { method: 'POST', body: {} }), 'Starting…');
    if (result) toast('Autonomous engine started', live ? 'Live execution is active. Watch the risk governor.' : 'Paper execution is active. Watch the first scan land.', live ? 'error' : 'ok');
  }

  async function handleHalt() {
    if (!state.data) return;
    const kill = state.data.bot && state.data.bot.kill_switch;
    if (kill) {
      toast('Already halted', 'Trading is stopped. Clear the halt in the risk governor to resume.', 'info');
      return;
    }
    await perform(el.btnHalt, () => api('/api/halt', { method: 'POST', body: {} }), 'Halting…');
    toast('Emergency halt active', 'No new orders will be accepted. Existing positions remain open.', 'error', 6500);
  }

  async function handleClearHalt() {
    await perform(el.btnClearHalt, () => api('/api/halt/clear', { method: 'POST', body: {} }), 'Clearing…');
    if (state.data && !state.data.bot.kill_switch) toast('Kill switch cleared', 'The engine can be started again.', 'ok');
  }

  async function handleAnalyze() {
    await perform(el.btnAnalyze, () => api('/api/analyze', { method: 'POST', body: {} }), 'Analyzing…');
    if (state.data && state.data.signal) {
      const action = state.data.signal.action;
      toast('Analysis complete', `${action === 'WAIT' ? 'No executable setup' : action + ' setup'} · ${Math.round((state.data.signal.confidence || 0) * 100)}% confidence.`, 'info', 3600);
    }
  }

  async function handleCloseTrade(ticket) {
    if (!ticket) return;
    const live = state.data.meta && state.data.meta.is_live;
    const confirmed = await openConfirm({
      title: `Close position ${ticket}?`,
      subtitle: live ? 'This sends a live close order to MT5.' : 'This closes the paper position at the current market price.',
      message: 'The position will be closed at market. Realised profit or loss is final.',
      confirmLabel: live ? 'Close live position' : 'Close position',
      destructive: true,
    });
    if (!confirmed) return;
    try {
      const result = await api('/api/positions/close', { method: 'POST', body: { ticket } });
      if (result.state) applyState(result.state);
      const pnl = result.result && Number(result.result.pnl || 0);
      toast('Position closed', `${ticket} · ${Number.isFinite(pnl) ? signedMoney(pnl) : 'closed at market'}.`, pnl < 0 ? 'error' : 'ok');
    } catch (error) {
      if (error.status !== 401) toast('Close failed', error.message, 'error');
    }
  }

  async function handleFlatten() {
    const live = state.data.meta && state.data.meta.is_live;
    const phrase = live ? 'FLATTEN LIVE' : 'FLATTEN';
    const confirmed = await openConfirm({
      title: live ? 'Flatten all live positions?' : 'Flatten all paper positions?',
      subtitle: live ? 'Every open MT5 position will be closed at market.' : 'Every open paper position will be closed at market.',
      message: 'This is an immediate risk-reducing action. Realised profit or loss is final.',
      confirmLabel: live ? 'Flatten live book' : 'Flatten paper book',
      destructive: true,
      requireText: phrase,
    });
    if (!confirmed) return;
    try {
      const result = await api('/api/positions/close-all', { method: 'POST', body: {} });
      if (result.state) applyState(result.state);
      toast('Book flattened', `${result.result ? result.result.closed : 0} position(s) closed.`, 'ok');
    } catch (error) {
      if (error.status !== 401) toast('Flatten failed', error.message, 'error');
    }
  }

  // ---------------------------------------------------------------- //
  // Modal, drawer and confirmation                                    //
  // ---------------------------------------------------------------- //
  function updateOverlayLock() {
    const anyOpen = (el.modal && !el.modal.hidden) || (el.drawer && !el.drawer.hidden);
    document.body.style.overflow = anyOpen ? 'hidden' : '';
  }

  function focusable(container) {
    return $$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', container)
      .filter((node) => !node.disabled && node.offsetParent !== null);
  }

  function openModal(options = {}) {
    const { title = '', subtitle = '', body = '', wide = false, narrow = false, onClose = null } = options;
    if (el.drawer && !el.drawer.hidden) closeDrawer();
    if (el.modal && !el.modal.hidden) closeModal(false);
    state.lastFocus = document.activeElement;
    state.modalOnClose = typeof onClose === 'function' ? onClose : null;
    el.modalTitle.textContent = title;
    el.modalSub.textContent = subtitle;
    el.modalSub.hidden = !subtitle;
    el.modalBody.innerHTML = body;
    el.modalPanel.className = `modal-panel${wide ? ' wide' : narrow ? ' narrow' : ''}`;
    el.modal.hidden = false;
    updateOverlayLock();
    requestAnimationFrame(() => {
      const nodes = focusable(el.modalPanel);
      if (nodes.length) nodes[0].focus({ preventScroll: true });
    });
  }

  function closeModal(result = false) {
    if (!el.modal || el.modal.hidden) return;
    el.modal.hidden = true;
    const callback = state.modalOnClose;
    state.modalOnClose = null;
    el.modalBody.innerHTML = '';
    updateOverlayLock();
    if (state.lastFocus && typeof state.lastFocus.focus === 'function') {
      try { state.lastFocus.focus({ preventScroll: true }); } catch (_) { /* noop */ }
    }
    state.lastFocus = null;
    if (callback) callback(result);
  }

  function openDrawer() {
    if (!el.drawer) return;
    if (el.modal && !el.modal.hidden) closeModal(false);
    state.lastFocus = document.activeElement;
    el.drawer.hidden = false;
    state.drawerOpen = true;
    updateOverlayLock();
    requestAnimationFrame(() => $('#drawer-close', el.drawer)?.focus({ preventScroll: true }));
  }

  function closeDrawer() {
    if (!el.drawer || el.drawer.hidden) return;
    el.drawer.hidden = true;
    state.drawerOpen = false;
    updateOverlayLock();
    if (state.lastFocus && typeof state.lastFocus.focus === 'function') {
      try { state.lastFocus.focus({ preventScroll: true }); } catch (_) { /* noop */ }
    }
    state.lastFocus = null;
  }

  function openConfirm({ title, subtitle = '', message, confirmLabel = 'Confirm', destructive = false, requireText = null }) {
    return new Promise((resolve) => {
      const body = `
        <div class="confirm-copy">${message}</div>
        ${requireText ? `<div class="confirm-input">
          <label class="field-label" for="confirm-text">Type <b style="color:var(--text)">${esc(requireText)}</b> to confirm</label>
          <input class="text-input" id="confirm-text" type="text" autocomplete="off" spellcheck="false" placeholder="${esc(requireText)}">
        </div>` : ''}
        <div class="confirm-error" id="confirm-error"></div>
        <div class="modal-actions">
          <button class="btn btn-ghost" id="confirm-cancel" type="button">Cancel</button>
          <button class="btn ${destructive ? 'btn-solid-danger' : 'btn-primary'}" id="confirm-ok" type="button">${esc(confirmLabel)}</button>
        </div>`;
      openModal({ title, subtitle, body, narrow: true, onClose: (value) => resolve(!!value) });
      $('#confirm-cancel')?.addEventListener('click', () => closeModal(false));
      const input = $('#confirm-text');
      const error = $('#confirm-error');
      const ok = () => {
        if (requireText && input && input.value.trim() !== requireText) {
          if (error) error.textContent = `Type ${requireText} exactly to continue.`;
          input.focus();
          return;
        }
        closeModal(true);
      };
      $('#confirm-ok')?.addEventListener('click', ok);
      input?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); ok(); }
      });
      requestAnimationFrame(() => (input || $('#confirm-ok'))?.focus({ preventScroll: true }));
    });
  }

  // ---------------------------------------------------------------- //
  // Settings                                                          //
  // ---------------------------------------------------------------- //
  function openSettings() {
    if (!state.data || !state.bootstrap) return;
    const schema = state.bootstrap.schema;
    const settings = state.data.settings || {};
    if (!schema || !schema.specs) {
      toast('Settings unavailable', 'The engine did not return a settings schema.', 'error');
      return;
    }
    const groupMeta = schema.groups || {};
    const groupOrder = ['engine', 'risk', 'management', 'ai'];
    let html = '';
    groupOrder.forEach((groupKey, index) => {
      const keys = Object.keys(schema.specs).filter((key) => schema.specs[key].group === groupKey);
      if (!keys.length) return;
      const meta = groupMeta[groupKey] || {};
      html += `<section class="settings-section">
        <div class="settings-section-head"><span class="num-label">0${index + 1}</span><h3>${esc(meta.label || groupKey)}</h3></div>
        <p>${esc(meta.description || '')}</p>`;
      keys.forEach((key) => {
        const spec = schema.specs[key];
        const value = settings[key] !== undefined ? settings[key] : spec.default;
        if (spec.type === 'boolean') {
          html += `<div class="switch-row">
            <div class="switch-copy"><strong>${esc(spec.label)}</strong><span>${esc(spec.help || '')}</span></div>
            <label class="switch"><input type="checkbox" data-setting="${esc(key)}" ${value ? 'checked' : ''} aria-label="${esc(spec.label)}"><span class="track"></span></label>
          </div>`;
        } else if (spec.type === 'select') {
          html += `<div class="field">
            <div class="field-label-row"><label class="field-label" for="set-${esc(key)}">${esc(spec.label)}</label></div>
            <select class="text-input" id="set-${esc(key)}" data-setting="${esc(key)}">
              ${(spec.options || []).map((option) => `<option value="${esc(option)}" ${String(option) === String(value) ? 'selected' : ''}>${esc(option)}</option>`).join('')}
            </select>
            <div class="field-hint">${esc(spec.help || '')}</div>
          </div>`;
        } else {
          html += `<div class="field">
            <div class="field-label-row"><label class="field-label" for="set-${esc(key)}">${esc(spec.label)}</label></div>
            <div class="number-wrap">
              <input class="text-input" id="set-${esc(key)}" data-setting="${esc(key)}" type="number" inputmode="decimal" value="${esc(value)}" min="${esc(spec.min)}" max="${esc(spec.max)}" step="${esc(spec.step)}">
              ${spec.suffix ? `<span class="number-suffix">${esc(spec.suffix)}</span>` : ''}
            </div>
            <div class="field-hint">${esc(spec.help || '')}</div>
          </div>`;
        }
      });
      html += '</section>';
    });

    const conn = state.data.connection || {};
    const env = state.bootstrap.connection || {};
    const endpointLabel = conn.mode === 'mt4' ? 'EA endpoint' : 'Bridge host';
    const endpointValue = conn.mode === 'mt4'
      ? (env.ea_endpoint || 'http://127.0.0.1/api/ea/poll')
      : (conn.bridge_host || 'not configured');
    html += `<section class="settings-section">
      <div class="settings-section-head"><span class="num-label">05</span><h3>Environment</h3></div>
      <p>Connection and AI configuration live in the server environment. Secrets never reach this browser.</p>
      <div class="connection-grid">
        <div class="connection-item"><div class="label">Execution mode</div><div class="value">${esc(brokerName(conn.mode))}</div></div>
        <div class="connection-item"><div class="label">Data feed</div><div class="value">${conn.connected ? esc(conn.data_source || '—') : 'offline'}</div></div>
        <div class="connection-item"><div class="label">${endpointLabel}</div><div class="value">${esc(endpointValue)}</div></div>
        <div class="connection-item"><div class="label">AI overlay</div><div class="value">${esc(state.data.ai && state.data.ai.configured ? state.data.ai.model : 'quant-only')}</div></div>
      </div>
      <div class="field-row" style="margin-top:2px">
        <button class="btn btn-ghost" id="settings-connection" type="button">${icon('link', 'icon icon-sm')}Connection settings</button>
        ${state.authEnabled ? `<button class="btn btn-ghost" id="btn-logout" type="button">${icon('lock', 'icon icon-sm')}Sign out</button>` : ''}
      </div>
    </section>`;

    if (state.data.meta && state.data.meta.is_live === false) {
      html += `<section class="settings-section">
        <div class="settings-section-head"><span class="num-label">06</span><h3>Paper account</h3></div>
        <div class="danger-zone">
          <h4>Reset simulated account</h4>
          <p>Clears all paper trades, equity history and signals, then restores the starting balance. This cannot be undone.</p>
          <button class="btn btn-danger btn-sm" id="btn-reset-paper" type="button">Reset paper account</button>
        </div>
      </section>`;
    }

    el.settingsFields.innerHTML = html;
    el.settingsError.textContent = '';
    openDrawer();
    bindSettingsActions();
  }

  function bindSettingsActions() {
    $('#settings-connection')?.addEventListener('click', () => { closeDrawer(); openConnectionModal(); });
    $('#btn-logout')?.addEventListener('click', handleLogout);
    $('#btn-reset-paper')?.addEventListener('click', async () => {
      const confirmed = await openConfirm({
        title: 'Reset the paper account?',
        subtitle: 'This clears the simulated book and starts over.',
        message: 'All paper trades, equity history, logs tied to signals and the current signal will be reset. This cannot be undone.',
        confirmLabel: 'Reset paper account',
        destructive: true,
        requireText: 'RESET PAPER',
      });
      if (!confirmed) return;
      try {
        const result = await api('/api/reset-paper', { method: 'POST', body: { confirm: 'RESET PAPER' } });
        if (result.state) applyState(result.state);
        closeDrawer();
        toast('Paper account reset', 'A fresh simulated book is ready.', 'ok');
        fetchLatestBacktestOnce(true);
      } catch (error) {
        if (error.status !== 401) toast('Reset failed', error.message, 'error');
      }
    });
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (state.settingsSaving || !state.data) return;
    const updates = {};
    let invalid = false;
    $$('[data-setting]', el.settingsFields).forEach((input) => {
      const key = input.dataset.setting;
      if (input.type === 'checkbox') {
        updates[key] = input.checked;
      } else if (input.type === 'number') {
        const value = Number(input.value);
        if (!Number.isFinite(value) || input.value === '') { invalid = true; input.focus(); return; }
        updates[key] = value;
      } else {
        updates[key] = input.value;
      }
    });
    if (invalid) {
      el.settingsError.textContent = 'Every numeric field needs a valid value.';
      return;
    }
    const previousTimeframe = state.data.settings && state.data.settings.timeframe;
    state.settingsSaving = true;
    el.settingsError.textContent = '';
    setButtonBusy(el.btnSettingsSave, true, 'Saving…');
    try {
      const result = await api('/api/settings', { method: 'POST', body: { settings: updates } });
      el.settingsError.textContent = '';
      if (result.state) applyState(result.state);
      closeDrawer();
      toast('Settings saved', 'Applied to the next scan.', 'ok');
      const newTimeframe = result.state && result.state.settings && result.state.settings.timeframe;
      if (newTimeframe && newTimeframe !== previousTimeframe) {
        state.timeframe = newTimeframe;
        updateChartTimeframeButtons();
        fetchCandles();
      }
    } catch (error) {
      if (error.status !== 401) {
        el.settingsError.textContent = error.message;
        toast('Settings not saved', error.message, 'error');
      }
    } finally {
      state.settingsSaving = false;
      setButtonBusy(el.btnSettingsSave, false);
    }
  }

  // ---------------------------------------------------------------- //
  // Connection modal / live arming                                    //
  // ---------------------------------------------------------------- //
  function openConnectionModal() {
    if (!state.data) return;
    const data = state.data;
    const conn = data.connection || {};
    const env = state.bootstrap ? (state.bootstrap.connection || {}) : {};
    const live = data.meta && data.meta.is_live;
    const mode = conn.mode || (live ? 'mt5' : 'paper');
    const mt5Configured = !!(conn.bridge_configured || env.bridge_configured);
    const mt4Configured = !!(conn.ea_configured || env.ea_configured);
    const liveEnabled = !!(conn.live_enabled || env.live_enabled);
    const terminalName = brokerName(mode);
    const accountLabel = data.account
      ? `${data.account.account_type === 'real' ? 'Real account' : 'Demo account'} · ${data.account.login || '—'} · ${data.account.server || 'Broker'}`
      : 'No account data';
    const bridgeHost = conn.bridge_host || env.bridge_host || 'not configured';
    const activeHost = mode === 'mt4' ? (env.ea_endpoint || 'http://127.0.0.1/api/ea/poll') : mode === 'mt5' ? bridgeHost : 'not in use';
    const activeStatus = mode === 'paper' ? 'not in use' : conn.connected ? (mode === 'mt4' ? 'EA heartbeat online' : 'bridge connected') : (mode === 'mt4' ? 'EA heartbeat offline' : 'bridge offline');

    let body = `<div class="mode-cards">
      <div class="mode-card ${mode === 'paper' ? 'current' : ''}">
        <h4>${icon('database', 'icon icon-sm')} Paper simulator ${mode === 'paper' ? '<span class="badge badge-gold" style="margin-left:auto">CURRENT</span>' : ''}</h4>
        <p>Synthetic XAU/USD feed and an in-process execution book. Safe for testing every automatic path.</p>
        <div class="mode-status">${mode === 'paper' ? 'Connected · simulated' : 'No live terminal selected'}</div>
      </div>
      <div class="mode-card ${mode === 'mt5' ? 'current' : ''}">
        <h4>${icon('terminal', 'icon icon-sm')} MetaTrader 5 ${mode === 'mt5' ? '<span class="badge badge-gold" style="margin-left:auto">CURRENT</span>' : ''}</h4>
        <p>Windows Python bridge for account data and trade execution; starts view-only.</p>
        <div class="mode-status">${mode === 'mt5' ? (conn.connected ? 'Bridge connected' : 'Bridge disconnected') : mt5Configured ? 'Bridge configured' : 'Bridge not configured'}</div>
      </div>
      <div class="mode-card ${mode === 'mt4' ? 'current' : ''}">
        <h4>${icon('terminal', 'icon icon-sm')} MetaTrader 4 ${mode === 'mt4' ? '<span class="badge badge-gold" style="margin-left:auto">CURRENT</span>' : ''}</h4>
        <p>MT4 Expert Advisor polls the dashboard; the EA runs inside the terminal and starts view-only.</p>
        <div class="mode-status">${mode === 'mt4' ? (conn.connected ? 'EA heartbeat online' : 'EA heartbeat offline') : mt4Configured ? 'Token configured · waiting for EA' : 'Token not configured'}</div>
      </div>
    </div>`;

    body += `<div class="connection-grid">
      <div class="connection-item"><div class="label">${mode === 'mt4' ? 'EA heartbeat' : 'Bridge status'}</div><div class="value ${conn.connected && live ? 'up' : ''}">${activeStatus}</div></div>
      <div class="connection-item"><div class="label">Account</div><div class="value">${esc(accountLabel)}</div></div>
      <div class="connection-item"><div class="label">Server latency</div><div class="value">${Number(conn.latency_ms || 0)} ms</div></div>
      <div class="connection-item"><div class="label">${mode === 'mt4' ? 'EA endpoint' : 'Bridge host'}</div><div class="value">${esc(activeHost)}</div></div>
    </div>`;

    if (live) {
      if (conn.armed) {
        body += `<div class="arm-box armed">
          <h4 style="color:var(--green)">${icon('shield', 'icon icon-sm')} Live execution is armed</h4>
          <p>Real orders are enabled until <b>${esc(fmtTime(conn.arm_expires_at, true))}</b>. Arming expires automatically after 12 hours or when you disarm.</p>
          <button class="btn btn-danger btn-sm" id="btn-disarm" type="button">Disarm live execution</button>
        </div>`;
      } else {
        body += `<div class="arm-box">
          <h4>${icon('lock', 'icon icon-sm')} Arm live execution</h4>
          <p>Arming permits the engine and manual controls to place <b>real orders</b>. The server must have live trading enabled. Type the arm phrase configured in <code>AURUM_LIVE_ARM_PHRASE</code> (default: <code>ARM LIVE TRADING</code>).</p>
          <div class="arm-row">
            <input class="text-input" id="arm-phrase" type="password" autocomplete="off" placeholder="Arm phrase" ${(!liveEnabled || !conn.connected) ? 'disabled' : ''}>
            <button class="btn btn-solid-danger" id="btn-arm" type="button" ${(!liveEnabled || !conn.connected) ? 'disabled' : ''}>Arm</button>
          </div>
          <div class="confirm-error" id="arm-error"></div>
          ${!liveEnabled ? `<div class="field-hint" style="color:#e3b06a">Server live trading is disabled. Set AURUM_LIVE_ENABLED=true and restart.</div>` : ''}
        </div>`;
      }
    } else {
      body += `<div class="modal-note">${icon('info', 'icon')}
        <span>Connecting to a terminal is <b>view-only</b> by default. Arming is separate. MT5 credentials stay on its Windows bridge; MT4 also requires its EA-side real-account switch before real orders are possible.</span>
      </div>`;
    }

    const actions = `<div class="modal-actions">
      <span class="left">${live && conn.connected ? `${terminalName} connected` : 'Environment secrets stay server-side'}</span>
      ${mode !== 'mt5' ? `<button class="btn btn-primary" id="btn-switch-mt5" type="button" ${mt5Configured ? '' : 'disabled'}>Connect MT5</button>` : ''}
      ${mode !== 'mt4' ? `<button class="btn btn-primary" id="btn-switch-mt4" type="button" ${mt4Configured ? '' : 'disabled'}>Connect MT4</button>` : ''}
      ${live ? `<button class="btn btn-ghost" id="btn-reconnect" type="button">${icon('refresh', 'icon icon-sm')}Reconnect</button>` : ''}
      ${live ? `<button class="btn btn-ghost" id="btn-switch-paper" type="button">Switch to paper</button>` : ''}
    </div>`;

    openModal({
      title: 'Connection & live access',
      subtitle: 'Choose one active terminal; paper remains a separate simulator.',
      body: body + actions,
      onClose: null,
    });
    bindConnectionActions();
  }

  function bindConnectionActions() {
    $('#btn-switch-paper')?.addEventListener('click', () => switchMode('paper'));
    $('#btn-switch-mt5')?.addEventListener('click', () => switchMode('mt5'));
    $('#btn-switch-mt4')?.addEventListener('click', () => switchMode('mt4'));
    $('#btn-reconnect')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      setButtonBusy(button, true, 'Connecting…');
      try {
        const result = await api('/api/reconnect', { method: 'POST', body: {} });
        if (result.state) applyState(result.state);
        closeModal(false);
        toast(`${brokerName((state.data.connection || {}).mode)} reconnected`, 'Account and quote feeds are live.', 'ok');
        fetchCandles({ silent: true });
      } catch (error) {
        if (error.status !== 401) toast('Reconnect failed', error.message, 'error');
      } finally {
        setButtonBusy(button, false);
      }
    });
    $('#btn-arm')?.addEventListener('click', async (event) => {
      const phrase = $('#arm-phrase') ? $('#arm-phrase').value : '';
      const error = $('#arm-error');
      if (!phrase.trim()) {
        if (error) error.textContent = 'Enter the arm phrase.';
        $('#arm-phrase')?.focus();
        return;
      }
      const button = event.currentTarget;
      setButtonBusy(button, true, 'Arming…');
      try {
        const result = await api('/api/live/arm', { method: 'POST', body: { phrase } });
        if (result.state) applyState(result.state);
        closeModal(false);
        toast('Live execution armed', 'Real orders are now possible. The risk governor still applies.', 'error', 6500);
        openConnectionModal();
      } catch (err) {
        if (error) error.textContent = err.message;
        if (err.status !== 401) toast('Arming denied', err.message, 'error');
      } finally {
        setButtonBusy(button, false);
      }
    });
    $('#btn-disarm')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      setButtonBusy(button, true, 'Disarming…');
      try {
        const result = await api('/api/live/disarm', { method: 'POST', body: {} });
        if (result.state) applyState(result.state);
        closeModal(false);
        toast('Live execution disarmed', 'The engine is paused and real orders are blocked.', 'ok');
        openConnectionModal();
      } catch (error) {
        if (error.status !== 401) toast('Could not disarm', error.message, 'error');
      } finally {
        setButtonBusy(button, false);
      }
    });
  }

  async function switchMode(mode) {
    const platformName = brokerName(mode);
    const button = mode === 'paper' ? $('#btn-switch-paper') : $(`#btn-switch-${mode}`);
    setButtonBusy(button, true, mode === 'paper' ? 'Switching…' : 'Connecting…');
    try {
      const result = await api('/api/mode', { method: 'POST', body: { mode } });
      if (result.state) applyState(result.state);
      closeModal(false);
      if (mode === 'paper') {
        toast('Paper mode active', 'Simulated execution resumed. No real orders can be placed.', 'ok');
      } else {
        const armed = result.state && result.state.connection && result.state.connection.armed;
        toast(`${platformName} connected`, armed ? 'Live execution is armed.' : 'View-only connection. Arm the session before starting auto.', armed ? 'error' : 'ok');
      }
      fetchCandles();
    } catch (error) {
      if (error.status !== 401) toast(mode === 'paper' ? 'Cannot switch to paper' : `${platformName} connection failed`, error.message, 'error', 7000);
    } finally {
      setButtonBusy(button, false);
    }
  }

  // ---------------------------------------------------------------- //
  // Risk details, logs and backtest report                            //
  // ---------------------------------------------------------------- //
  function openRiskDetails() {
    const risk = state.data.risk || {};
    const checks = Array.isArray(risk.checks) ? risk.checks : [];
    const failing = checks.filter((check) => !check.passed).length;
    const rows = checks.map((check) => `<div class="check-row ${check.passed ? '' : 'fail'}">
      <span class="check-dot">${icon(check.passed ? 'check' : 'x', 'icon')}</span>
      <div class="check-copy"><strong>${esc(labelForCheck(check))}</strong><span>${esc(check.detail)}</span></div>
    </div>`).join('');
    openModal({
      title: 'Risk governor checks',
      subtitle: `${failing} of ${checks.length} conditions not satisfied · evaluated at the latest scan`,
      body: `<div class="connection-grid" style="margin-top:0">
        <div class="connection-item"><div class="label">Daily loss</div><div class="value">${Number(risk.daily_loss_pct || 0).toFixed(2)}% / ${Number(risk.daily_limit_pct || 0).toFixed(2)}%</div></div>
        <div class="connection-item"><div class="label">Drawdown</div><div class="value">${Number(risk.drawdown_pct || 0).toFixed(2)}% / ${Number(risk.drawdown_limit_pct || 0).toFixed(2)}%</div></div>
        <div class="connection-item"><div class="label">Risk budget</div><div class="value">${money(risk.risk_budget)}</div></div>
        <div class="connection-item"><div class="label">Open risk</div><div class="value">${money(risk.open_risk)}</div></div>
      </div>
      <div class="checks-list">${rows || '<div class="empty-state">No risk data yet.</div>'}</div>
      <p class="report-note">The evaluation is intentionally conservative. Setup-dependent checks are unsatisfied when the signal is WAIT. A failing check can only block a new entry; it never enlarges or rescues a position. Daily loss and drawdown are measured from account equity.</p>
      <div class="modal-actions"><button class="btn btn-ghost" data-close-modal-inline type="button">Close</button></div>`,
    });
    $('[data-close-modal-inline]', el.modalBody)?.addEventListener('click', () => closeModal(false));
  }

  function logRow(log) {
    return `<div class="log-row">
      <div class="log-time">${esc(fmtTime(log.ts, true))}</div>
      <div class="log-main"><div class="log-msg">${esc(log.message || '')}</div><div class="log-event">${esc(String(log.event || '').replace(/_/g, ' '))}</div></div>
      <div class="log-level ${esc(log.level || '')}">${esc(log.level || 'INFO')}</div>
    </div>`;
  }

  async function openLogs() {
    openModal({ title: 'Engine activity log', subtitle: 'Newest first · up to 200 events', body: '<div class="bt-loading"><span class="spin"></span>Loading events…</div>' });
    try {
      const result = await api('/api/logs?limit=200');
      const logs = (result.logs || []).slice().reverse();
      el.modalBody.innerHTML = `<div class="log-list" style="max-height:58vh;overflow:auto">${logs.map(logRow).join('') || '<div class="empty-state"><span>No events recorded yet.</span></div>'}</div>`;
    } catch (error) {
      el.modalBody.innerHTML = `<div class="modal-note modal-danger">${icon('alert', 'icon')}<span>${esc(error.message)}</span></div>`;
    }
  }

  function backtestStat(label, value, className = '') {
    return `<div class="report-stat"><div class="label">${esc(label)}</div><div class="value ${className}">${value}</div></div>`;
  }

  async function openBacktestReport() {
    if (!state.currentBacktest) {
      await fetchLatestBacktestOnce(true);
      if (!state.currentBacktest) {
        toast('No report available', 'Run a backtest first.', 'info');
        return;
      }
    }
    const result = state.currentBacktest;
    if (!result || !result.summary) return;
    const s = result.summary;
    const sharpe = s.sharpe === null || s.sharpe === undefined ? '—' : Number(s.sharpe).toFixed(2);
    const stats = [
      backtestStat('Net return', pct(s.return_pct, 2, true), s.return_pct >= 0 ? 'up' : 'down'),
      backtestStat('Max drawdown', `${Number(s.max_drawdown_pct || 0).toFixed(2)}%`, 'down'),
      backtestStat('Win rate', `${Number(s.win_rate_pct || 0).toFixed(1)}%`),
      backtestStat('Profit factor', Number(s.profit_factor) >= 999 ? '∞' : Number(s.profit_factor || 0).toFixed(2)),
      backtestStat('Trades', String(s.trades || 0)),
      backtestStat('Avg hold', `${Number(s.avg_hold_hours || 0).toFixed(1)}h`),
      backtestStat('Sharpe', sharpe),
      backtestStat('Buy & hold', pct(s.buy_hold_pct, 2, true)),
    ].join('');
    const trades = (result.trades || []).map((trade) => `<tr>
      <td class="mono">${esc(fmtDate(trade.closed_at))}</td>
      <td><span class="side-cell ${trade.side === 'BUY' ? 'side-buy' : 'side-sell'}">${esc(trade.side)}</span></td>
      <td class="num">${Number(trade.lots || 0).toFixed(2)}</td>
      <td class="num">${price(trade.entry)} → ${price(trade.exit)}</td>
      <td class="num ${Number(trade.pnl) >= 0 ? 'up' : 'down'}">${signedMoney(trade.pnl)}</td>
      <td style="font-size:9px;color:var(--muted)">${esc(trade.exit_reason || '—')}</td>
    </tr>`).join('');
    const request = result.request || {};
    const imported = result.data_source === 'mt5_csv';
    const costs = request.costs || {};
    const costNote = imported
      ? `Costs modeled: $${Number(costs.spread || 0).toFixed(2)}/oz spread, $${Number(costs.commission_per_lot || 0).toFixed(2)}/lot round turn, $${Number(costs.slippage || 0).toFixed(2)}/oz slippage. `
      : '';
    const subtitle = imported
      ? `Imported MT5 history · ${request.bars || '—'} bars · ${request.timeframe || 'M5'}`
      : `${request.days || ''} simulated days · ${s.trades} trades · seed ${request.seed || '—'}`;
    const body = `<div class="report-stats">${stats}</div>
      <div class="report-chart"><canvas id="report-equity" aria-label="Backtest equity curve"></canvas></div>
      <div class="table-scroll"><table class="data"><thead><tr><th>Closed</th><th>Side</th><th>Lots</th><th>Entry → Exit</th><th>P&amp;L</th><th>Exit</th></tr></thead><tbody>${trades}</tbody></table></div>
      <p class="report-note">${esc(costNote)}${esc(result.disclaimer || 'Simulated research only. Not a forecast.')}</p>
      <div class="modal-actions"><span class="left">${esc(result.label || 'Backtest')} · ${imported ? esc(request.bars || '') + ' bars' : esc(request.days || '') + ' days'}</span><button class="btn btn-ghost" data-close-modal-inline type="button">Close report</button></div>`;
    openModal({ title: 'Strategy lab report', subtitle, body, wide: true });
    state.reportPoints = result.equity || null;
    const draw = () => {
      if (el.modal.hidden) return;
      const canvas = $('#report-equity');
      if (canvas) drawReportLine(canvas, state.reportPoints, s.return_pct >= 0 ? '#3ddba0' : '#ff6477');
    };
    requestAnimationFrame(draw);
    setTimeout(draw, 80);
    $('[data-close-modal-inline]', el.modalBody)?.addEventListener('click', () => closeModal(false));
  }

  async function openCsvImport() {
    const body = `<div class="confirm-copy">
        Import XAU/USD bars exported from MetaTrader 5. Fills and bracket management are still simulated, but entries, sessions and volatility come from the imported history.
      </div>
      <div class="modal-note" style="margin-top:12px">${icon('info', 'icon')}
        <span><b>MT5:</b> open XAUUSD → M5 chart, load enough history (at least 30 days), then use <b>History Center → Export</b> or save the chart data as CSV. Expected columns include Date, Time, Open, High, Low, Close.</span>
      </div>
      <div class="field" style="margin-top:14px">
        <label class="field-label" for="csv-file">MT5 CSV file</label>
        <input class="file-input" id="csv-file" type="file" accept=".csv,.txt,text/csv,text/plain">
        <div class="csv-file-name" id="csv-file-name">No file selected</div>
      </div>
      <div class="field-row">
        <div class="field">
          <label class="field-label" for="csv-offset">Server UTC offset</label>
          <div class="number-wrap"><input class="text-input" id="csv-offset" type="number" value="2" min="-12" max="14" step="0.5"><span class="number-suffix">hours</span></div>
          <div class="field-hint">Exness server time is usually GMT+2 in winter and GMT+3 in summer.</div>
        </div>
        <div class="field">
          <label class="field-label" for="csv-spread">Spread assumption</label>
          <div class="number-wrap"><input class="text-input" id="csv-spread" type="number" value="0.20" min="0" max="10" step="0.01"><span class="number-suffix">$/oz</span></div>
          <div class="field-hint">Raw Spread XAUUSD varies; use your typical spread.</div>
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label class="field-label" for="csv-commission">Commission</label>
          <div class="number-wrap"><input class="text-input" id="csv-commission" type="number" value="7" min="0" max="200" step="0.1"><span class="number-suffix">$/lot</span></div>
          <div class="field-hint">Round-turn commission for your Raw Spread account.</div>
        </div>
        <div class="field">
          <label class="field-label" for="csv-timeframe">Timeframe</label>
          <select class="text-input" id="csv-timeframe">
            <option value="M5" selected>M5</option>
            <option value="M15">M15</option>
            <option value="M30">M30</option>
            <option value="H1">H1</option>
          </select>
          <div class="field-hint">Auto-detected from bar spacing if different.</div>
        </div>
      </div>
      <div class="confirm-error" id="csv-error"></div>
      <div class="modal-actions">
        <button class="btn btn-ghost" id="csv-cancel" type="button">Cancel</button>
        <button class="btn btn-primary" id="csv-run" type="button">Run imported-data test</button>
      </div>`;
    openModal({ title: 'Import MT5 history', subtitle: 'Imported broker data · simulated execution and costs', body });
    const fileInput = $('#csv-file');
    fileInput?.addEventListener('change', () => {
      const file = fileInput.files && fileInput.files[0];
      setText('#csv-file-name', file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB` : 'No file selected');
    });
    $('#csv-cancel')?.addEventListener('click', () => closeModal(false));
    $('#csv-run')?.addEventListener('click', async (event) => {
      const file = fileInput && fileInput.files ? fileInput.files[0] : null;
      const error = $('#csv-error');
      if (!file) {
        if (error) error.textContent = 'Choose an MT5 CSV file first.';
        return;
      }
      if (file.size > 7 * 1024 * 1024) {
        if (error) error.textContent = 'File is too large. The limit is 7 MB.';
        return;
      }
      const button = event.currentTarget;
      setButtonBusy(button, true, 'Reading…');
      if (error) error.textContent = '';
      try {
        const text = await file.text();
        setButtonBusy(button, true, 'Backtesting…');
        const offsetHours = Number($('#csv-offset')?.value || 2);
        const result = await api('/api/backtest/csv', {
          method: 'POST',
          timeout: 180000,
          body: {
            filename: file.name,
            csv: text,
            timeframe: $('#csv-timeframe')?.value || 'M5',
            utc_offset_minutes: Math.round(offsetHours * 60),
            spread: Number($('#csv-spread')?.value || 0.20),
            commission_per_lot: Number($('#csv-commission')?.value || 7),
            slippage: 0.02,
          },
        });
        if (result.backtest) state.currentBacktest = result.backtest;
        state.backtestAutoLoaded = true;
        if (result.state) applyState(result.state);
        closeModal(false);
        const s = result.backtest && result.backtest.summary;
        toast('Imported-data backtest complete', s ? `${result.backtest.import?.bars || '—'} bars · ${s.trades} trades · ${pct(s.return_pct, 2, true)}.` : 'Report ready.', 'ok', 6000);
      } catch (err) {
        if (error) error.textContent = err.message;
        if (err.status !== 401) toast('CSV backtest failed', err.message, 'error', 6000);
      } finally {
        setButtonBusy(button, false);
      }
    });
  }

  async function fetchLatestBacktestOnce(force = false) {
    if (state.data && state.data.meta) {
      if (!state.data.backtest && !force) {
        state.backtestAutoLoaded = true;
        return;
      }
    }
    if (state.backtestAutoLoaded && !force) return;
    state.backtestAutoLoaded = true;
    try {
      const result = await api('/api/backtest/latest');
      if (result.backtest && result.backtest.summary) {
        state.currentBacktest = result.backtest;
        if (state.data) renderBacktestCard();
      }
    } catch (_) {
      // A missing historical report is not an error worth troubling the operator.
    }
  }

  async function runBacktest() {
    if (state.backtestRunning) return;
    const days = Number(el.btDays ? el.btDays.value : 30);
    state.backtestRunning = true;
    if (el.btLoading) el.btLoading.hidden = false;
    if (el.btEmpty) el.btEmpty.hidden = true;
    if (el.btResult) el.btResult.hidden = true;
    if (el.btnBtReport) el.btnBtReport.hidden = true;
    if (el.btBadge) { el.btBadge.textContent = 'RUNNING'; el.btBadge.className = 'badge badge-gold'; }
    if (el.btnRunBacktest) setButtonBusy(el.btnRunBacktest, true, 'Running…');
    toast('Backtest started', `${days} simulated days of XAUUSD M5 data.`, 'info', 2600);
    try {
      const result = await api('/api/backtest', { method: 'POST', body: { days }, timeout: 120000 });
      if (result.backtest) state.currentBacktest = result.backtest;
      state.backtestRunning = false;
      if (result.state) applyState(result.state);
      renderBacktestCard();
      const summary = result.backtest && result.backtest.summary;
      if (summary) toast('Backtest complete', `${summary.trades} trades · ${pct(summary.return_pct, 2, true)} simulated return.`, summary.return_pct >= 0 ? 'ok' : 'error', 5000);
    } catch (error) {
      state.backtestRunning = false;
      if (el.btLoading) el.btLoading.hidden = true;
      if (state.data) renderBacktestCard();
      if (error.status !== 401) toast('Backtest failed', error.message, 'error');
    } finally {
      state.backtestRunning = false;
      if (el.btnRunBacktest) {
        setButtonBusy(el.btnRunBacktest, false);
      }
      if (state.data) renderBacktestCard();
    }
  }

  // ---------------------------------------------------------------- //
  // Session lifecycle                                                 //
  // ---------------------------------------------------------------- //
  function showLogin(message = '') {
    stopPolling();
    state.loaded = false;
    if (el.modal && !el.modal.hidden) closeModal(false);
    if (el.drawer && !el.drawer.hidden) closeDrawer();
    if (el.boot) el.boot.hidden = true;
    if (el.app) el.app.hidden = true;
    if (el.login) el.login.hidden = false;
    if (el.loginError) el.loginError.textContent = message;
    if (el.loginPassword) {
      el.loginPassword.value = '';
      setTimeout(() => el.loginPassword.focus(), 60);
    }
  }

  function handleSessionExpired() {
    if (!state.authEnabled) return;
    if (el.login && !el.login.hidden) return;
    showLogin('Your session expired. Sign in again to continue.');
  }

  async function handleLogin(event) {
    event.preventDefault();
    if (state.loginPending) return;
    const password = el.loginPassword ? el.loginPassword.value : '';
    if (!password) {
      el.loginError.textContent = 'Enter your dashboard password.';
      return;
    }
    state.loginPending = true;
    el.loginError.textContent = '';
    setButtonBusy(el.loginSubmit, true, 'Signing in…');
    try {
      await api('/api/login', { method: 'POST', body: { password }, suppressAuth: true });
      el.login.hidden = true;
      el.loginPassword.value = '';
      await loadDashboard();
    } catch (error) {
      el.loginError.textContent = error.status === 429 ? error.message : 'Incorrect password. Try again.';
      el.loginPassword.select();
    } finally {
      state.loginPending = false;
      setButtonBusy(el.loginSubmit, false);
    }
  }

  async function handleLogout() {
    try { await api('/api/logout', { method: 'POST', body: {} }); } catch (_) { /* clear client regardless */ }
    closeDrawer();
    showLogin('Signed out.');
  }

  async function loadDashboard() {
    if (state.stateLoading) return;
    state.stateLoading = true;
    try {
      if (!state.bootstrap) state.bootstrap = await api('/api/bootstrap');
      const payload = await api('/api/state');
      if (!payload.state) throw new Error('The engine returned no state.');
      state.data = payload.state;
      state.authEnabled = !!(payload.state.meta && payload.state.meta.auth_enabled);
      if (!state.loaded) state.timeframe = payload.state.settings.timeframe || 'M5';
      updateChartTimeframeButtons();
      el.boot.hidden = true;
      el.login.hidden = true;
      el.app.hidden = false;
      state.loaded = true;
      networkRestored();
      renderAll();
      await fetchCandles({ silent: true });
      state.chart?.resize();
      fetchLatestBacktestOnce();
      startPolling();
      if (state.bootRetry) { clearTimeout(state.bootRetry); state.bootRetry = null; }
    } catch (error) {
      if (error.status === 401) {
        showLogin('Session expired. Sign in again to continue.');
      } else {
        el.login.hidden = false;
        el.boot.hidden = true;
        el.app.hidden = true;
        if (el.loginError) el.loginError.textContent = 'Cannot reach the trading engine. Retrying…';
        toast('Dashboard unavailable', error.message, 'error');
        if (state.bootRetry) clearTimeout(state.bootRetry);
        state.bootRetry = setTimeout(async () => {
          state.bootRetry = null;
          try {
            const auth = await api('/api/auth', { suppressAuth: true });
            state.auth = auth;
            state.authEnabled = !!auth.auth_enabled;
            if (auth.auth_enabled && !auth.authenticated) showLogin('Sign in to continue.');
            else await loadDashboard();
          } catch (_) {
            showLogin('Engine unavailable. Sign in again when it is back.');
          }
        }, 4000);
      }
      throw error;
    } finally {
      state.stateLoading = false;
    }
  }

  async function attemptInitialLoad() {
    try {
      const auth = await api('/api/auth', { suppressAuth: true });
      state.auth = auth;
      state.authEnabled = !!auth.auth_enabled;
      if (el.loginVersion) el.loginVersion.textContent = `v${auth.version || '1.0.0'}`;
      if (auth.auth_enabled && !auth.authenticated) {
        el.boot.hidden = true;
        el.login.hidden = false;
        setTimeout(() => el.loginPassword?.focus(), 100);
        return;
      }
      await loadDashboard();
      if (state.bootRetry) { clearTimeout(state.bootRetry); state.bootRetry = null; }
    } catch (error) {
      el.boot.hidden = false;
      el.app.hidden = true;
      if (el.bootMsg) el.bootMsg.textContent = 'Desk unreachable · retrying';
      if (!state.bootRetry) {
        state.bootRetry = setTimeout(() => {
          state.bootRetry = null;
          attemptInitialLoad();
        }, 4000);
      }
    }
  }

  // ---------------------------------------------------------------- //
  // Wiring                                                            //
  // ---------------------------------------------------------------- //
  function cacheElements() {
    el.boot = $('#boot');
    el.bootMsg = $('#boot-msg');
    el.login = $('#login');
    el.loginForm = $('#login-form');
    el.loginPassword = $('#login-password');
    el.loginSubmit = $('#login-submit');
    el.loginError = $('#login-error');
    el.loginVersion = $('#login-version');
    el.app = $('#app');
    el.btnAuto = $('#btn-auto');
    el.autoLabel = $('#auto-label');
    el.btnHalt = $('#btn-halt');
    el.btnSettings = $('#btn-settings');
    el.btnConnection = $('#btn-connection');
    el.envBanner = $('#env-banner');
    el.envTitle = $('#env-title');
    el.envText = $('#env-text');
    el.envAction = $('#env-action');
    el.chartCanvas = $('#chart-canvas');
    el.chartTooltip = $('#chart-tooltip');
    el.chartEmpty = $('#chart-empty');
    el.chartArea = $('#chart-area');
    el.heroChange = $('#hero-change');
    el.accountDelta = $('#account-delta');
    el.accountType = $('#account-type');
    el.accountDot = $('#account-dot');
    el.equitySpark = $('#equity-spark');
    el.signalCard = $('#signal-card');
    el.signalContent = $('#signal-content');
    el.signalEmpty = $('#signal-empty');
    el.signalReasons = $('#signal-reasons');
    el.signalRing = $('#signal-ring');
    el.btnAnalyze = $('#btn-analyze');
    el.riskCard = $('#risk-card');
    el.riskChecks = $('#risk-checks');
    el.btnClearHalt = $('#btn-clear-halt');
    el.positionsBody = $('#positions-body');
    el.positionsTable = $('#positions-table');
    el.positionsEmpty = $('#positions-empty');
    el.panelPositions = $('#panel-positions');
    el.panelHistory = $('#panel-history');
    el.historyBody = $('#history-body');
    el.historyTable = $('#history-table');
    el.historyEmpty = $('#history-empty');
    el.tabPositions = $('#tab-positions');
    el.tabHistory = $('#tab-history');
    el.btnFlatten = $('#btn-flatten');
    el.perfPnl = $('#perf-pnl');
    el.btEmpty = $('#bt-empty');
    el.btLoading = $('#bt-loading');
    el.btResult = $('#bt-result');
    el.btReturn = $('#bt-return');
    el.btDd = $('#bt-dd');
    el.btTrades = $('#bt-trades');
    el.btPf = $('#bt-pf');
    el.btSpark = $('#bt-spark');
    el.btBadge = $('#bt-badge');
    el.btDays = $('#bt-days');
    el.btnRunBacktest = $('#btn-run-backtest');
    el.btnBtReport = $('#btn-bt-report');
    el.activityList = $('#activity-list');
    el.toasts = $('#toasts');
    el.modal = $('#modal');
    el.modalPanel = $('#modal-panel');
    el.modalTitle = $('#modal-title');
    el.modalSub = $('#modal-sub');
    el.modalBody = $('#modal-body');
    el.modalClose = $('#modal-close');
    el.drawer = $('#drawer');
    el.drawerClose = $('#drawer-close');
    el.settingsForm = $('#settings-form');
    el.settingsFields = $('#settings-fields');
    el.settingsError = $('#settings-error');
    el.btnSettingsSave = $('#btn-settings-save');
    el.btnSettingsCancel = $('#btn-settings-cancel');
    el.btRunning = false;
  }

  function bindStaticEvents() {
    el.btnAuto?.addEventListener('click', handleAuto);
    el.btnHalt?.addEventListener('click', handleHalt);
    el.btnSettings?.addEventListener('click', openSettings);
    el.btnConnection?.addEventListener('click', openConnectionModal);
    el.envAction?.addEventListener('click', openConnectionModal);
    el.btnAnalyze?.addEventListener('click', handleAnalyze);
    el.btnClearHalt?.addEventListener('click', handleClearHalt);
    el.btnFlatten?.addEventListener('click', handleFlatten);
    el.btnRunBacktest?.addEventListener('click', runBacktest);
    $('#btn-bt-import')?.addEventListener('click', openCsvImport);
    el.btnBtReport?.addEventListener('click', openBacktestReport);
    $('#risk-more')?.addEventListener('click', openRiskDetails);
    $('#btn-refresh-chart')?.addEventListener('click', () => {
      fetchCandles();
      toast('Chart refreshed', `${state.timeframe} candles reloaded.`, 'info', 2200);
    });
    $('#btn-view-logs')?.addEventListener('click', openLogs);
    el.loginForm?.addEventListener('submit', handleLogin);
    $('#login-reveal')?.addEventListener('click', () => {
      if (!el.loginPassword) return;
      const showing = el.loginPassword.type === 'text';
      el.loginPassword.type = showing ? 'password' : 'text';
      const button = $('#login-reveal');
      if (button) button.innerHTML = icon(showing ? 'eye' : 'eye-off', 'icon');
    });
    $$('.tf-btn').forEach((button) => button.addEventListener('click', () => setTimeframe(button.dataset.timeframe)));
    el.tabPositions?.addEventListener('click', () => { state.activeTab = 'positions'; setPanelVisibility(); });
    el.tabHistory?.addEventListener('click', () => { state.activeTab = 'history'; setPanelVisibility(); });
    el.positionsBody?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-close-ticket]');
      if (button) handleCloseTrade(button.dataset.closeTicket);
    });
    el.modalClose?.addEventListener('click', () => closeModal(false));
    $('[data-close-modal]')?.addEventListener('click', () => closeModal(false));
    el.drawerClose?.addEventListener('click', closeDrawer);
    $('[data-close-drawer]')?.addEventListener('click', closeDrawer);
    el.btnSettingsCancel?.addEventListener('click', closeDrawer);
    el.settingsForm?.addEventListener('submit', saveSettings);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        if (el.modal && !el.modal.hidden) { closeModal(false); return; }
        if (el.drawer && !el.drawer.hidden) { closeDrawer(); return; }
      }
      if (event.key === 'Tab') {
        const container = el.modal && !el.modal.hidden ? el.modalPanel : el.drawer && !el.drawer.hidden ? el.drawer : null;
        if (!container) return;
        const nodes = focusable(container);
        if (!nodes.length) return;
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.loaded) {
        refreshState();
        fetchCandles({ silent: true });
      }
    });
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (state.chart) state.chart.resize();
        renderSparks();
        if (state.data) renderBacktestCard();
        if (state.reportPoints && el.modal && !el.modal.hidden) {
          const canvas = $('#report-equity');
          if (canvas) drawReportLine(canvas, state.reportPoints, '#3ddba0');
        }
      }, 120);
    });
  }

  function initCharts() {
    if (!el.chartCanvas) return;
    state.chart = new PriceChart(el.chartCanvas, el.chartTooltip, (candle) => {
      state.hoverCandle = candle || null;
      updateOhlc(candle || latestCandle());
    });
    updateChartTimeframeButtons();
  }

  async function init() {
    cacheElements();
    bindStaticEvents();
    initCharts();
    setInterval(tickClock, 1000);
    tickClock();
    await attemptInitialLoad();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
