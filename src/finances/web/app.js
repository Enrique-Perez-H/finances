const eur = n => new Intl.NumberFormat(undefined, {style: 'currency', currency: 'EUR'}).format(n);
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  kids.flat().forEach(k => e.append(k));
  return e;
};
const card = (title, ...kids) => el('section', {className: 'card'}, el('h2', {textContent: title}), ...kids);
const row = (l, r, cls = '') => el('div', {className: 'row'}, el('span', {textContent: l}), el('span', {textContent: r, className: cls}));

function line(points) {
  const W = 300, H = 90, max = Math.max(...points), min = Math.min(...points, 0);
  const x = i => (i / Math.max(points.length - 1, 1)) * W;
  const y = v => H - ((v - min) / ((max - min) || 1)) * (H - 6) - 3;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const path = document.createElementNS(ns, 'polyline');
  path.setAttribute('points', points.map((v, i) => `${x(i)},${y(v)}`).join(' '));
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '2');
  svg.append(path);
  return svg;
}

function render(d) {
  const app = document.getElementById('app');
  app.replaceChildren(
    card('Net worth', el('div', {className: 'big', textContent: eur(d.net_worth.total)}),
      ...Object.entries(d.net_worth.by_kind).map(([k, v]) => row(k, eur(v)))),
    card('Accounts', ...d.accounts.map(a => row(`${a.name} · ${a.provider}`, eur(a.balance)))),
    card('Monthly cash flow', ...d.cashflow.map(m =>
      row(m.month, (m.saved >= 0 ? '+' : '') + eur(m.saved), m.saved >= 0 ? 'pos' : 'neg'))),
    card('Spending this month', ...(d.categories.length ? d.categories.map(c => {
      const w = Math.round((c.total / d.categories[0].total) * 100);
      return el('div', {}, row(c.category, eur(c.total)), el('div', {className: 'bar', style: `width:${w}%`}));
    }) : [el('div', {className: 'muted', textContent: 'No spending yet'})])),
    card('Projection (est.)', line(d.projection.map(p => p.value)),
      el('div', {className: 'muted', textContent:
        `${d.projection.length} yrs, 4%/yr + average saving. Not financial advice.`}),
      row(`In ${d.projection.at(-1)?.year ?? 0} years`, eur(d.projection.at(-1)?.value ?? 0))),
  );
}

async function load() {
  const r = await fetch('/api/summary');
  render(await r.json());
}

document.getElementById('sync').addEventListener('click', async ev => {
  const btn = ev.currentTarget, status = document.getElementById('status');
  btn.disabled = true; status.textContent = 'Syncing…';
  try {
    const r = await fetch('/api/sync', {method: 'POST', headers: {'X-Requested-With': 'finances'}});
    const s = await r.json();
    status.textContent = Object.entries(s).map(([k, v]) =>
      `${k}: ${v.state}${v.prompt ? ' – ' + v.prompt : ''}`).join(' | ');
    await load();
  } catch (e) { status.textContent = 'Sync failed'; }
  btn.disabled = false;
});

load();
