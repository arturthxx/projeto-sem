/* Smart Energy Manager — camada de dados.
 *
 * Duas implementações de DataSource com a mesma interface:
 *   - ApiSource:    backend FastAPI (/api/*) + Server-Sent Events (/api/stream)
 *   - StaticSource: pacote sem.demo.v1 (window.SEM_DEMO ou data/demo.json)
 *
 * Ambas entregam um "snapshot" normalizado:
 *   { kind, source, fixture, disclaimer, generatedAt, meta, config, live,
 *     history:{t, series}, energyDaily:[...], decisions:[...], events:[...],
 *     forecast, comparison, nowMs }
 * Detecção automática (SEM.data.detect): window.SEM_DEMO → estático;
 * ?fixture → data/demo.fixture.json (apenas desenvolvimento local);
 * senão tenta /api/meta → API; senão data/demo.json.
 */
(function (global) {
  'use strict';

  const SEM = global.SEM;
  const isNum = SEM.isNum;

  // ------------------------------------------------------------------ utilidades de rede
  async function fetchJson(url, timeoutMs) {
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs || 8000) : null;
    try {
      const r = await fetch(url, { headers: { Accept: 'application/json' }, signal: ctrl ? ctrl.signal : undefined, cache: 'no-store' });
      if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
      const ct = r.headers.get('content-type') || '';
      if (!ct.includes('json')) throw new Error(`${url}: resposta não é JSON`);
      return await r.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  // ------------------------------------------------------------------ telemetria → linha do vocabulário §8
  /** Converte um pacote sem.telemetry.v1 numa linha colunar (docs/05 §8). */
  function telemetryToRow(tel) {
    if (!tel || typeof tel !== 'object') return null;
    const m = tel.meas || {};
    const val = (p, k) => (m[p] && m[p].ok !== false && isNum(m[p][k]) ? m[p][k] : null);
    const row = {
      t: tel.ts_ms,
      pv_v: val('pv', 'v'), pv_i: val('pv', 'i'), pv_p: val('pv', 'p'),
      bat_v: val('bat', 'v'), bat_i: val('bat', 'i'), bat_p: val('bat', 'p'),
      bus_v: val('bus', 'v'), bus_i: val('bus', 'i'), bus_p: val('bus', 'p'),
      t_pv: val('t_pv', 't'), t_bat: val('t_bat', 't'), irr_poa: val('irr', 'g'),
      soc: tel.soc && isNum(tel.soc.value) ? tel.soc.value : null,
      soc_unc: tel.soc && isNum(tel.soc.unc) ? tel.soc.unc : null,
      mode: tel.ems ? tel.ems.mode : null,
      state: tel.ems ? tel.ems.state : null,
      safety: tel.ems ? tel.ems.safety : null,
    };
    for (const ld of tel.loads || []) {
      const n = ld.id;
      row[`l${n}_p`] = isNum(ld.p) ? ld.p : null;
      row[`l${n}_on`] = ld.on ? 1 : 0;
      row[`l${n}_duty`] = isNum(ld.duty) ? ld.duty : null;
      row[`l${n}_d`] = ld.demand ? 1 : 0;
      if (ld.deferrable && isNum(ld.deferrable.e_done_wh)) row.def_done_wh = ld.deferrable.e_done_wh;
    }
    if (tel.sim) {
      if (isNum(tel.sim.p_pv_avail)) row.pv_p_avail = tel.sim.p_pv_avail;
      if (isNum(tel.sim.soc_true)) row.soc_true = tel.sim.soc_true;
    }
    if (isNum(row.pv_p_avail) && isNum(row.pv_p)) row.curtail_p = Math.max(0, row.pv_p_avail - row.pv_p);
    return row;
  }

  /** Acrescenta uma linha a um histórico colunar (mantém colunas alinhadas). */
  function appendRow(history, row, maxPoints) {
    if (!row || !isNum(row.t)) return false;
    const t = history.t;
    if (t.length && row.t <= t[t.length - 1]) return false;
    const n = t.length;
    t.push(row.t);
    const s = history.series;
    for (const k of Object.keys(row)) {
      if (k === 't') continue;
      if (!s[k]) s[k] = new Array(n).fill(null);
    }
    for (const k of Object.keys(s)) s[k].push(k in row ? row[k] : null);
    const excess = t.length - (maxPoints || 20000);
    if (excess > 0) {
      t.splice(0, excess);
      for (const k of Object.keys(s)) s[k].splice(0, excess);
    }
    return true;
  }

  // ------------------------------------------------------------------ normalização
  function byTsDesc(a, b) {
    return (Number(b.ts_ms) || 0) - (Number(a.ts_ms) || 0);
  }

  function normalizeHistory(h) {
    const t = h && Array.isArray(h.t) ? h.t.slice() : [];
    const series = {};
    const src = (h && h.series) || {};
    for (const k of Object.keys(src)) {
      if (Array.isArray(src[k]) && src[k].length === t.length) series[k] = src[k].slice();
    }
    return { t, series };
  }

  function normalizeComparison(c) {
    if (!c || typeof c !== 'object' || !Array.isArray(c.table) || !c.table.length) return null;
    return {
      scenarios: Array.isArray(c.scenarios) ? c.scenarios : [...new Set(c.table.map((r) => r.scenario))],
      strategies: Array.isArray(c.strategies) ? c.strategies : [...new Set(c.table.map((r) => r.strategy))],
      seeds: Array.isArray(c.seeds) ? c.seeds : [],
      metrics_def: c.metrics_def || {},
      table: c.table,
      series: c.series || {},
      source: c.source || 'simulation',
    };
  }

  /** Monta o snapshot comum a partir das partes (já no formato do contrato). */
  function buildSnapshot(parts) {
    const meta = parts.meta || {};
    const live = parts.live || null;
    const history = normalizeHistory(parts.history);
    let nowMs = live && isNum(live.ts_ms) && live.ts_ms > 0 ? live.ts_ms : null;
    if (!nowMs && history.t.length) nowMs = history.t[history.t.length - 1];
    if (!nowMs) nowMs = Date.now();
    const source = (live && live.source) || meta.source || parts.source || null;
    return {
      kind: parts.kind,
      source,
      fixture: parts.fixture === true,
      disclaimer: parts.disclaimer || meta.disclaimer || null,
      generatedAt: parts.generatedAt || null,
      meta,
      config: meta.config || {},
      live,
      history,
      energyDaily: Array.isArray(parts.energyDaily) ? parts.energyDaily.slice() : [],
      decisions: (Array.isArray(parts.decisions) ? parts.decisions.slice() : []).sort(byTsDesc),
      events: (Array.isArray(parts.events) ? parts.events.slice() : []).sort(byTsDesc),
      forecast: parts.forecast || null,
      comparison: normalizeComparison(parts.comparison),
      nowMs,
    };
  }

  // ------------------------------------------------------------------ StaticSource
  class StaticSource {
    /** @param {object} doc pacote sem.demo.v1  @param {string} origin descrição da origem do arquivo */
    constructor(doc, origin) {
      this.kind = 'static';
      this.doc = doc;
      this.origin = origin;
    }

    static async fromUrl(url) {
      const doc = await fetchJson(url, 15000);
      return new StaticSource(doc, url);
    }

    async load() {
      const d = this.doc || {};
      if (d.schema !== 'sem.demo.v1') console.warn('[SEM] pacote estático com schema inesperado:', d.schema);
      // Regra do contrato: o pacote estático é SEMPRE simulação.
      const live = d.live ? Object.assign({}, d.live, { source: d.live.source || d.source }) : null;
      return buildSnapshot({
        kind: 'static',
        source: d.source,
        fixture: d.fixture === true,
        disclaimer: d.disclaimer,
        generatedAt: d.generated_at,
        meta: Object.assign({ source: d.source }, d.meta || {}),
        live,
        history: d.history,
        energyDaily: d.energy_daily && d.energy_daily.days,
        decisions: d.decisions,
        events: d.events,
        forecast: d.forecast,
        comparison: d.comparison,
      });
    }

    subscribe() {
      /* pacote estático: sem atualizações */
    }

    /** Exportação CSV: no modo estático não há downloads por link (mostra-se o texto). */
    csvLink() {
      return null;
    }
  }

  // ------------------------------------------------------------------ ApiSource
  class ApiSource {
    constructor(meta) {
      this.kind = 'api';
      this.firstMeta = meta || null;
      this.es = null;
      this.snapshot = null;
      this.timer = null;
    }

    async load() {
      const opt = (p) => fetchJson(p).catch((e) => {
        console.warn('[SEM]', e.message);
        return null;
      });
      const [meta, live, history, daily, decisions, events, forecast, comparison] = await Promise.all([
        this.firstMeta ? Promise.resolve(this.firstMeta) : fetchJson('api/meta'),
        opt('api/live'),
        opt('api/history?minutes=4320&step_s=60'),
        opt('api/energy/daily?days=14'),
        opt('api/decisions?limit=100'),
        opt('api/events?limit=100'),
        opt('api/forecast'),
        opt('api/comparison'),
      ]);
      this.firstMeta = null;
      this.snapshot = buildSnapshot({
        kind: 'api',
        meta: meta || {},
        live,
        history,
        energyDaily: daily && daily.days,
        decisions: decisions && decisions.items,
        events: events && events.items,
        forecast,
        comparison,
      });
      return this.snapshot;
    }

    /** Assina o SSE; `onChange(snapshot, kind)` a cada telemetria/decisão/evento; `onStatus(texto)` na conexão. */
    subscribe(onChange, onStatus) {
      if (typeof EventSource === 'undefined') {
        onStatus('sem SSE');
        return;
      }
      const es = new EventSource('api/stream');
      this.es = es;
      es.onopen = () => onStatus('ao vivo');
      es.onerror = () => onStatus('reconectando');
      const parse = (ev) => {
        try {
          return JSON.parse(ev.data);
        } catch (_e) {
          return null;
        }
      };
      es.addEventListener('telemetry', (ev) => {
        const tel = parse(ev);
        const snap = this.snapshot;
        if (!tel || !snap) return;
        snap.live = tel;
        if (tel.source) snap.source = tel.source;
        if (isNum(tel.ts_ms) && tel.ts_ms > 0) snap.nowMs = tel.ts_ms;
        appendRow(snap.history, telemetryToRow(tel), 20000);
        onChange(snap, 'telemetry');
      });
      es.addEventListener('decision', (ev) => {
        const d = parse(ev);
        if (!d || !this.snapshot) return;
        this.snapshot.decisions.unshift(d);
        this.snapshot.decisions.length = Math.min(this.snapshot.decisions.length, 200);
        onChange(this.snapshot, 'decision');
      });
      es.addEventListener('event', (ev) => {
        const d = parse(ev);
        if (!d || !this.snapshot) return;
        this.snapshot.events.unshift(d);
        this.snapshot.events.length = Math.min(this.snapshot.events.length, 200);
        onChange(this.snapshot, 'event');
      });
      // Agregados lentos (energia diária, previsão, metadados) são relidos periodicamente.
      this.timer = setInterval(async () => {
        try {
          const [meta, daily, forecast] = await Promise.all([fetchJson('api/meta'), fetchJson('api/energy/daily?days=14'), fetchJson('api/forecast')]);
          const snap = this.snapshot;
          snap.meta = meta;
          snap.config = meta.config || snap.config;
          snap.energyDaily = daily.days || snap.energyDaily;
          snap.forecast = forecast;
          onChange(snap, 'refresh');
        } catch (e) {
          console.warn('[SEM] atualização periódica falhou:', e.message);
        }
      }, 120000);
    }

    csvLink(minutes) {
      return `api/export.csv?minutes=${minutes || 1440}`;
    }
  }

  // ------------------------------------------------------------------ detecção
  async function detect() {
    if (global.SEM_DEMO && typeof global.SEM_DEMO === 'object') return new StaticSource(global.SEM_DEMO, 'pacote embutido');
    const qs = new URLSearchParams(global.location.search);
    if (qs.has('fixture')) return StaticSource.fromUrl('data/demo.fixture.json');
    if (!qs.has('demo') && global.location.protocol !== 'file:') {
      try {
        const meta = await fetchJson('api/meta', 3000);
        if (meta && typeof meta === 'object' && 'config' in meta) return new ApiSource(meta);
      } catch (_e) {
        /* sem backend: segue para o pacote estático */
      }
    }
    try {
      return await StaticSource.fromUrl('data/demo.json');
    } catch (e) {
      const err = new Error('Nenhuma fonte de dados disponível.');
      err.detail = e && e.message;
      throw err;
    }
  }

  SEM.data = { detect, StaticSource, ApiSource, telemetryToRow, appendRow, buildSnapshot, fetchJson };
})(window);
