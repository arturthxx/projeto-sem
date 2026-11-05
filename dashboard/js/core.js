/* Smart Energy Manager — núcleo do dashboard.
 *
 * Scripts clássicos (sem módulos ES) concatenáveis em ordem; tudo vive no
 * namespace global único `window.SEM`. Este arquivo define utilidades de DOM,
 * formatação (pt-BR, hora local), rótulos do vocabulário do contrato
 * (docs/05) e um barramento de eventos simples.
 */
(function (global) {
  'use strict';

  const SEM = (global.SEM = global.SEM || {});
  SEM.pages = SEM.pages || {};
  SEM.version = '0.1.0';

  // ------------------------------------------------------------------ DOM
  const SVG_NS = 'http://www.w3.org/2000/svg';

  /** Acrescenta filhos (strings viram nós de texto; null/false são ignorados). */
  function append(node, children) {
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) append(node, c);
      else if (c instanceof Node) node.appendChild(c);
      else node.appendChild(document.createTextNode(String(c)));
    }
    return node;
  }

  /** Aplica atributos; `class`, `text`, `on*` (funções), `style` (objeto) e `dataset` têm tratamento especial. */
  function setAttrs(node, attrs, isSvg) {
    if (!attrs) return node;
    for (const k of Object.keys(attrs)) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.setAttribute('class', v);
      else if (k === 'text') node.textContent = String(v);
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (!isSvg && (k === 'value' || k === 'checked' || k === 'disabled')) node[k] = v;
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    return node;
  }

  /** Cria elemento HTML: el('div', {class:'x'}, 'texto', filho). Texto sempre via textContent. */
  SEM.el = function el(tag, attrs, ...children) {
    return append(setAttrs(document.createElement(tag), attrs, false), children);
  };

  /** Cria elemento SVG. */
  SEM.svg = function svg(tag, attrs, ...children) {
    return append(setAttrs(document.createElementNS(SVG_NS, tag), attrs, true), children);
  };

  SEM.clear = function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  };

  let uid = 0;
  /** Identificador único (ids de padrões SVG, aria-describedby etc.). */
  SEM.uid = (prefix) => `${prefix || 'sem'}-${++uid}`;

  // ------------------------------------------------------------------ armazenamento local (conveniência)
  SEM.prefs = {
    get(key, fallback) {
      try {
        const v = global.localStorage.getItem('sem.' + key);
        return v === null ? fallback : v;
      } catch (_e) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        if (value === null || value === undefined) global.localStorage.removeItem('sem.' + key);
        else global.localStorage.setItem('sem.' + key, String(value));
      } catch (_e) {
        /* armazenamento indisponível (aba privada, prévia): ignora */
      }
    },
  };

  // ------------------------------------------------------------------ barramento de eventos
  const listeners = {};
  SEM.bus = {
    on(name, fn) {
      (listeners[name] = listeners[name] || []).push(fn);
    },
    emit(name, payload) {
      for (const fn of listeners[name] || []) {
        try {
          fn(payload);
        } catch (e) {
          console.error('[SEM] erro no ouvinte', name, e);
        }
      }
    },
  };

  // ------------------------------------------------------------------ números
  const nfCache = new Map();
  function nf(dec) {
    const key = dec;
    if (!nfCache.has(key)) {
      nfCache.set(key, new Intl.NumberFormat('pt-BR', { minimumFractionDigits: dec, maximumFractionDigits: dec }));
    }
    return nfCache.get(key);
  }

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  SEM.isNum = isNum;

  /** Número em pt-BR com `dec` casas; '—' quando ausente. */
  SEM.fmt = function fmt(v, dec) {
    if (!isNum(v)) return '—';
    const d = dec === undefined ? 1 : dec;
    const r = nf(d).format(v);
    return r === '-0' || /^-0,0+$/.test(r) ? r.slice(1) : r;
  };

  /** Casas decimais adequadas à magnitude (leituras de instrumento). */
  SEM.autoDec = function autoDec(v) {
    if (!isNum(v)) return 1;
    const a = Math.abs(v);
    if (a >= 100) return 0;
    if (a >= 10) return 1;
    return 2;
  };

  /** Valor com unidade, separados por espaço inseparável. */
  SEM.fmtU = function fmtU(v, unit, dec) {
    if (!isNum(v)) return '—';
    return `${SEM.fmt(v, dec === undefined ? SEM.autoDec(v) : dec)} ${unit}`;
  };

  /** Fração 0..1 como porcentagem. */
  SEM.pct = function pct(v, dec) {
    if (!isNum(v)) return '—';
    return `${SEM.fmt(v * 100, dec === undefined ? 0 : dec)}%`;
  };

  /** Diferença de fração em pontos percentuais. */
  SEM.pp = function pp(v, dec) {
    if (!isNum(v)) return '—';
    return `${SEM.fmt(v * 100, dec === undefined ? 0 : dec)} p.p.`;
  };

  SEM.signed = function signed(v, dec) {
    if (!isNum(v)) return '—';
    const s = SEM.fmt(v, dec);
    return v > 0 ? '+' + s : s;
  };

  // ------------------------------------------------------------------ tempo (hora local de config.site)
  // America/Sao_Paulo não tem horário de verão desde 2019: deslocamento fixo
  // (config.site.utc_offset_h, padrão −3). Evita dependência de Intl com fuso.
  SEM.time = {
    offsetMs: -3 * 3600e3,
    label: 'UTC−3',
    configure(site) {
      const h = site && isNum(site.utc_offset_h) ? site.utc_offset_h : -3;
      this.offsetMs = h * 3600e3;
      this.label = `UTC${h < 0 ? '−' : '+'}${Math.abs(h)}`;
    },
    local(ms) {
      return new Date(ms + this.offsetMs);
    },
  };

  const p2 = (n) => String(n).padStart(2, '0');

  SEM.fmtTime = function fmtTime(ms, withSec) {
    if (!isNum(ms) || ms <= 0) return '—';
    const d = SEM.time.local(ms);
    const base = `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
    return withSec ? `${base}:${p2(d.getUTCSeconds())}` : base;
  };

  SEM.fmtDate = function fmtDate(ms) {
    if (!isNum(ms) || ms <= 0) return '—';
    const d = SEM.time.local(ms);
    return `${p2(d.getUTCDate())}/${p2(d.getUTCMonth() + 1)}`;
  };

  SEM.fmtDateTime = function fmtDateTime(ms) {
    if (!isNum(ms) || ms <= 0) return '—';
    return `${SEM.fmtDate(ms)} ${SEM.fmtTime(ms)}`;
  };

  /** 'AAAA-MM-DD' → 'DD/MM'. */
  SEM.fmtDay = function fmtDay(day) {
    if (typeof day !== 'string') return '—';
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
    return m ? `${m[3]}/${m[2]}` : day;
  };

  /** Dia local 'AAAA-MM-DD' de um instante. */
  SEM.dayKey = function dayKey(ms) {
    const d = SEM.time.local(ms);
    return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())}`;
  };

  /** Segundos desde a meia-noite local. */
  SEM.secOfDay = function secOfDay(ms) {
    const d = SEM.time.local(ms);
    return d.getUTCHours() * 3600 + d.getUTCMinutes() * 60 + d.getUTCSeconds();
  };

  /** 'HH:MM' → segundos. */
  SEM.hhmm = function hhmm(s) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || ''));
    return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 : null;
  };

  SEM.fmtDuration = function fmtDuration(sec) {
    if (!isNum(sec)) return '—';
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h > 0) return `${h} h ${p2(m)} min`;
    return `${m} min`;
  };

  // ------------------------------------------------------------------ séries
  /** Último valor numérico de um vetor. */
  SEM.lastNum = function lastNum(arr) {
    if (!Array.isArray(arr)) return null;
    for (let i = arr.length - 1; i >= 0; i--) if (isNum(arr[i])) return arr[i];
    return null;
  };

  /** Índice do primeiro t >= x (busca binária; t crescente). */
  SEM.bisect = function bisect(t, x) {
    let lo = 0;
    let hi = t.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (t[mid] < x) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  };

  /** Índice mais próximo de x. */
  SEM.nearest = function nearest(t, x) {
    if (!t.length) return -1;
    const i = SEM.bisect(t, x);
    if (i <= 0) return 0;
    if (i >= t.length) return t.length - 1;
    return x - t[i - 1] <= t[i] - x ? i - 1 : i;
  };

  /** Integral trapezoidal simples de potência (W) em Wh, ignorando lacunas > maxGapMs. */
  SEM.integrateWh = function integrateWh(t, v, maxGapMs) {
    let acc = 0;
    const gap = maxGapMs || 15 * 60e3;
    for (let i = 1; i < t.length; i++) {
      const dt = t[i] - t[i - 1];
      if (dt <= 0 || dt > gap || !isNum(v[i]) || !isNum(v[i - 1])) continue;
      acc += ((v[i] + v[i - 1]) / 2) * (dt / 3600e3);
    }
    return acc;
  };

  // ------------------------------------------------------------------ vocabulário (docs/05 §1)
  SEM.labels = {
    mode: {
      conventional: 'Convencional',
      rules: 'Regras (faixas de SOC)',
      predictive: 'Preditiva (MPC)',
    },
    modeShort: { conventional: 'Convencional', rules: 'Regras', predictive: 'Preditiva' },
    modeDesc: {
      conventional:
        'Referência: cargas seguem o perfil programado; a carga deslocável liga às 18h até cumprir a energia diária. Apenas o supervisor de segurança atua.',
      rules:
        'Máquina de estados por faixas de SOC com histerese (NORMAL, ECONOMY, CRITICAL, PROTECTION). Cargas de menor prioridade são reduzidas ou desligadas conforme o SOC cai.',
      predictive:
        'Controle preditivo com horizonte deslizante (MILP). Replaneja a cada passo usando previsão FV e de demanda; comandos expiram e, sem plano válido, o firmware volta às regras.',
    },
    state: {
      NORMAL: { label: 'Normal', tone: 'ok', icon: 'ok' },
      ECONOMY: { label: 'Economia', tone: 'warn', icon: 'warn' },
      CRITICAL: { label: 'Crítico', tone: 'serious', icon: 'warn' },
      PROTECTION: { label: 'Proteção', tone: 'crit', icon: 'stop' },
    },
    safety: {
      OK: { label: 'OK', tone: 'ok', icon: 'ok', desc: 'Nenhuma proteção ativa.' },
      BOOT: { label: 'Inicialização', tone: 'info', icon: 'info', desc: 'Aguardando amostras válidas antes de habilitar cargas.' },
      LVD: { label: 'LVD', tone: 'crit', icon: 'stop', desc: 'Desconexão por subtensão ou SOC mínimo rígido.' },
      OVP: { label: 'Sobretensão', tone: 'crit', icon: 'stop', desc: 'Sobretensão no barramento de cargas.' },
      OC: { label: 'Sobrecorrente', tone: 'crit', icon: 'stop', desc: 'Sobrecorrente no barramento; religamento automático.' },
      OC_LOCKOUT: { label: 'Bloqueio por sobrecorrente', tone: 'crit', icon: 'stop', desc: 'Tentativas esgotadas: exige reset manual.' },
      OT: { label: 'Sobretemperatura', tone: 'crit', icon: 'stop', desc: 'Temperatura da bateria acima do limite.' },
      SENSOR: { label: 'Falha de sensor', tone: 'serious', icon: 'warn', desc: 'Leitura ausente ou inválida; cargas desligadas por segurança.' },
    },
    prio: { high: 'Alta', medium: 'Média', low: 'Baixa' },
    level: {
      info: { label: 'Info', tone: 'info', icon: 'info' },
      warn: { label: 'Alerta', tone: 'warn', icon: 'warn' },
      error: { label: 'Erro', tone: 'serious', icon: 'warn' },
      critical: { label: 'Crítico', tone: 'crit', icon: 'stop' },
    },
    scenario: {
      ensolarado: 'Ensolarado',
      parcialmente_nublado: 'Parcialmente nublado',
      nublado: 'Nublado',
      semana_variavel: 'Semana variável',
    },
    origin: { firmware: 'Firmware', backend: 'Backend' },
  };

  /** Rótulo legível de um identificador (cenário, estratégia etc.). */
  SEM.label = function label(group, key) {
    const g = SEM.labels[group] || {};
    const v = g[key];
    if (v && typeof v === 'object') return v.label;
    if (v) return v;
    return String(key === null || key === undefined ? '—' : key).replace(/_/g, ' ');
  };

  // Unidades do vocabulário de séries (docs/05 §8) — usadas no CSV.
  SEM.units = {
    t: 'ms', pv_v: 'V', pv_i: 'A', pv_p: 'W', pv_p_avail: 'W', curtail_p: 'W', irr_poa: 'W/m2',
    t_amb: 'degC', t_pv: 'degC', t_bat: 'degC', bat_v: 'V', bat_i: 'A', bat_p: 'W',
    bus_v: 'V', bus_i: 'A', bus_p: 'W', aux_p: 'W', soc: '1', soc_unc: '1', soc_true: '1',
    unserved_p: 'W', def_done_wh: 'Wh', mode: 'text', state: 'text', safety: 'text',
  };
  SEM.unitOf = function unitOf(col) {
    if (SEM.units[col]) return SEM.units[col];
    const m = /^l\d+_(p|on|duty|d)$/.exec(col);
    if (m) return { p: 'W', on: '0/1', duty: '1', d: '0/1' }[m[1]];
    return '';
  };

  // ------------------------------------------------------------------ ícones (SVG em linha; nunca emojis)
  const ICONS = {
    ok: 'M4 8.5l2.6 2.6L12 5.6',
    warn: 'M8 2.5l6 10.5H2zM8 6.5v3M8 11.3v.2',
    stop: 'M5.5 2h5L14 5.5v5L10.5 14h-5L2 10.5v-5zM8 5v3.6M8 10.8v.2',
    info: 'M8 2a6 6 0 1 0 0 12A6 6 0 1 0 8 2zM8 7.2v4M8 4.9v.2',
    sun: 'M8 5a3 3 0 1 0 0 6 3 3 0 1 0 0-6zM8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M3 13l1.4-1.4M11.6 4.4L13 3',
    moon: 'M12.5 10.5A5.5 5.5 0 0 1 5.5 3.5a5.5 5.5 0 1 0 7 7z',
    auto: 'M8 2a6 6 0 1 0 0 12zM8 2a6 6 0 0 1 0 12',
    copy: 'M5 5h8v9H5zM3 11V2h7',
    ext: 'M9 3h4v4M13 3L7.5 8.5M11 9.5V13H3V5h3.5',
  };
  /** Ícone 16×16 em traço (cor = currentColor). */
  SEM.icon = function icon(name, cls) {
    const s = SEM.svg('svg', { viewBox: '0 0 16 16', width: 16, height: 16, class: 'icon ' + (cls || ''), 'aria-hidden': 'true', focusable: 'false' });
    s.appendChild(SEM.svg('path', { d: ICONS[name] || ICONS.info }));
    return s;
  };
})(window);
