// Small DOM helpers. Text always goes through textContent, never innerHTML.
export const eur = n => new Intl.NumberFormat(undefined, {style: 'currency', currency: 'EUR', maximumFractionDigits: 0}).format(n);
export const eur2 = n => new Intl.NumberFormat(undefined, {style: 'currency', currency: 'EUR'}).format(n);
export const pct = n => `${(n * 100).toFixed(0)}%`;

export function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  kids.flat().forEach(k => k != null && e.append(k));
  return e;
}
export const card = (title, ...kids) =>
  el('section', {className: 'card'}, title ? el('h2', {textContent: title}) : null, ...kids);
export const row = (l, r, cls = '') =>
  el('div', {className: 'row'}, el('span', {textContent: l}), el('span', {textContent: r, className: cls}));
export const note = text => el('div', {className: 'muted', textContent: text});
export const button = (text, onclick, cls = '') => {
  const b = el('button', {textContent: text, className: cls});
  b.addEventListener('click', onclick);
  return b;
};

export async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json()).error || r.status);
  return r.json();
}
export async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: {'X-Requested-With': 'finances', 'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}

const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs) => {
  const e = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  return e;
};

const compact = v => new Intl.NumberFormat(undefined, {notation: 'compact', maximumFractionDigits: 1}).format(v);

// Round tick values covering [min, max], about `count` of them.
function niceTicks(min, max, count = 4) {
  if (min === max) max = min + 1;
  const raw = (max - min) / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v / step) * step);
  return ticks;
}

// Line chart with a y axis. series = [{label, values}] (null = unknown, breaks the line).
// Click or drag on the chart to read the values of that point in the box underneath.
export function lineChart(series, labels = [], {format = eur, notes = [], onSelect = null} = {}) {
  const W = 340, H = 150, L = 46, R = 8, T = 8, B = 20;
  const all = series.flatMap(s => s.values).filter(v => v !== null);
  if (!all.length) all.push(0);
  const n = Math.max(...series.map(s => s.values.length), 1);
  const minV = Math.min(...all), maxV = Math.max(...all);
  // start at zero unless the values sit in a narrow band far above it
  const ticks = niceTicks(minV >= 0 && minV < maxV * 0.5 ? 0 : minV, maxV);
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const x = i => L + (i / Math.max(n - 1, 1)) * (W - L - R);
  const y = v => T + (1 - (v - lo) / ((hi - lo) || 1)) * (H - T - B);
  const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', class: 'chart'});

  ticks.forEach(t => {
    svg.append(svgEl('line', {x1: L, x2: W - R, y1: y(t), y2: y(t), class: t === 0 ? 'axis zero' : 'axis'}));
    const label = svgEl('text', {x: L - 5, y: y(t) + 3, 'text-anchor': 'end', class: 'tick'});
    label.textContent = compact(t);
    svg.append(label);
  });
  const xs = n <= 4 ? [...Array(n).keys()] : [0, Math.round((n - 1) / 3), Math.round(2 * (n - 1) / 3), n - 1];
  xs.forEach(i => {
    if (!labels[i]) return;
    const t = svgEl('text', {x: x(i), y: H - 5, class: 'tick',
      'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'});
    t.textContent = labels[i];
    svg.append(t);
  });

  series.forEach((s, k) => {
    let run = [];
    const flush = () => {
      if (run.length > 1) svg.append(svgEl('polyline', {points: run.join(' '), class: `line s${k}`, fill: 'none'}));
      else if (run.length === 1) {
        const [cx, cy] = run[0].split(',');
        svg.append(svgEl('circle', {cx, cy, r: 2.5, class: `dot s${k}`}));
      }
      run = [];
    };
    s.values.forEach((v, i) => (v === null ? flush() : run.push(`${x(i)},${y(v)}`)));
    flush();
  });

  const marker = svgEl('g', {});
  svg.append(marker);
  const readout = el('div', {className: 'readout'});
  const select = i => {
    marker.replaceChildren(svgEl('line', {x1: x(i), x2: x(i), y1: T, y2: H - B, class: 'marker'}));
    series.forEach((s, k) => s.values[i] !== null && marker.append(
      svgEl('circle', {cx: x(i), cy: y(s.values[i]), r: 3.5, class: `dot s${k}`})));
    readout.replaceChildren(el('strong', {textContent: labels[i] ?? `#${i + 1}`}),
      ...series.map((s, k) => el('span', {}, el('i', {className: `sw s${k}`}), `${s.label}: `,
        s.values[i] === null ? '–' : format(s.values[i]))),
      ...(notes[i] ? [el('em', {className: 'warn', textContent: notes[i]})] : []));
    if (onSelect) onSelect(i);
  };
  const pick = ev => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    select(Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1)))));
  };
  svg.addEventListener('pointerdown', pick);
  svg.addEventListener('pointermove', ev => { if (ev.buttons) pick(ev); });
  select(n - 1);
  return el('div', {className: 'chartbox'}, svg, readout);
}

export function legend(series) {
  return el('div', {className: 'legend'}, ...series.map((s, k) =>
    el('span', {}, el('i', {className: `sw s${k}`, style: s.color ? `background:${s.color}` : ''}), s.label)));
}

export function barRow(label, value, max, rightText, onclick) {
  const w = max ? Math.max(2, Math.round((value / max) * 100)) : 0;
  const r = el('div', {className: 'barrow' + (onclick ? ' tap' : '')},
    row(label, rightText), el('div', {className: 'bar', style: `width:${w}%`}));
  if (onclick) r.addEventListener('click', onclick);
  return r;
}

export function categoryInput(categories, value = '') {
  const id = 'cats-' + Math.random().toString(36).slice(2);
  const input = el('input', {value, placeholder: 'category', className: 'wide'});
  input.setAttribute('list', id);
  const list = el('datalist', {id}, ...categories.map(c => el('option', {value: c})));
  return {input, wrap: el('span', {}, input, list)};
}

// Dropdown of categories. Choosing one calls onChoose(name) straight away; the last option
// asks for a new name. `current` is shown as selected when given.
export function categoryPicker(categories, onChoose, {current = '', placeholder = 'Choose a category…'} = {}) {
  const sel = el('select', {className: 'picker'},
    el('option', {value: '', textContent: current || placeholder, selected: true, disabled: !current}),
    ...categories.filter(c => c !== current).map(c => el('option', {value: c, textContent: c})),
    el('option', {value: '__new__', textContent: '＋ New category…'}));
  sel.addEventListener('change', () => {
    let value = sel.value;
    sel.selectedIndex = 0;
    if (value === '__new__') value = (prompt('Name of the new category') || '').trim();
    if (value) onChoose(value);
  });
  return sel;
}

// Grouped bars per label with a y axis. series = [{label, values, color}]. Tap a month to read it.
export function barChart(series, labels = [], {format = eur, notes = [], onSelect = null} = {}) {
  const W = 340, H = 160, L = 46, R = 8, T = 8, B = 20;
  const n = labels.length || 1;
  const maxV = Math.max(...series.flatMap(s => s.values), 1);
  const ticks = niceTicks(0, maxV);
  const hi = ticks[ticks.length - 1];
  const y = v => T + (1 - v / hi) * (H - T - B);
  const slot = (W - L - R) / n, pad = Math.min(6, slot * 0.2);
  const barW = Math.max(1.5, (slot - 2 * pad) / series.length);
  const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', class: 'chart'});
  ticks.forEach(t => {
    svg.append(svgEl('line', {x1: L, x2: W - R, y1: y(t), y2: y(t), class: t === 0 ? 'axis zero' : 'axis'}));
    const label = svgEl('text', {x: L - 5, y: y(t) + 3, 'text-anchor': 'end', class: 'tick'});
    label.textContent = compact(t);
    svg.append(label);
  });
  const marker = svgEl('rect', {y: T, height: H - T - B, width: slot, class: 'band', visibility: 'hidden'});
  svg.append(marker);
  series.forEach((s, k) => s.values.forEach((v, i) => svg.append(svgEl('rect', {
    x: L + i * slot + pad + k * barW, y: y(v), width: Math.max(barW - 1, 1), height: Math.max(H - B - y(v), 0),
    rx: 1.5, fill: s.color || 'var(--accent)'}))));
  const shown = n <= 4 ? [...Array(n).keys()] : [0, Math.round((n - 1) / 3), Math.round(2 * (n - 1) / 3), n - 1];
  shown.forEach(i => {
    const t = svgEl('text', {x: L + (i + 0.5) * slot, y: H - 5, class: 'tick', 'text-anchor': 'middle'});
    t.textContent = labels[i];
    svg.append(t);
  });
  const readout = el('div', {className: 'readout'});
  const select = i => {
    marker.setAttribute('x', L + i * slot);
    marker.setAttribute('visibility', 'visible');
    readout.replaceChildren(el('strong', {textContent: labels[i]}),
      ...series.map(s => el('span', {}, el('i', {className: 'sw', style: `background:${s.color}`}), `${s.label}: ${format(s.values[i])}`)),
      ...(notes[i] ? [el('em', {className: 'warn', textContent: notes[i]})] : []));
    if (onSelect) onSelect(i);
  };
  const pick = ev => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    select(Math.max(0, Math.min(n - 1, Math.floor((px - L) / slot))));
  };
  svg.addEventListener('pointerdown', pick);
  svg.addEventListener('pointermove', ev => { if (ev.buttons) pick(ev); });
  select(n - 1);
  return el('div', {className: 'chartbox'}, svg, readout);
}
