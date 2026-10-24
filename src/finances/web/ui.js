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

// Lines: series = [{label, values, cls}], x labels shown at both ends.
export function lineChart(series, labels = []) {
  const W = 320, H = 120, pad = 4;
  const all = series.flatMap(s => s.values);
  const max = Math.max(...all, 0), min = Math.min(...all, 0);
  const n = Math.max(...series.map(s => s.values.length));
  const x = i => pad + (i / Math.max(n - 1, 1)) * (W - 2 * pad);
  const y = v => H - pad - ((v - min) / ((max - min) || 1)) * (H - 2 * pad);
  const svg = svgEl('svg', {viewBox: `0 0 ${W} ${H + 14}`, role: 'img'});
  if (min < 0) svg.append(svgEl('line', {x1: 0, x2: W, y1: y(0), y2: y(0), class: 'axis'}));
  series.forEach((s, k) => svg.append(svgEl('polyline', {
    points: s.values.map((v, i) => `${x(i)},${y(v)}`).join(' '),
    class: `line s${k}`, fill: 'none'})));
  if (labels.length) {
    [[0, labels[0], 'start'], [n - 1, labels[labels.length - 1], 'end']].forEach(([i, text, anchor]) => {
      const t = svgEl('text', {x: x(i), y: H + 11, 'text-anchor': anchor, class: 'tick'});
      t.textContent = text;
      svg.append(t);
    });
  }
  return svg;
}

export function legend(series) {
  return el('div', {className: 'legend'}, ...series.map((s, k) =>
    el('span', {}, el('i', {className: `sw s${k}`}), s.label)));
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
