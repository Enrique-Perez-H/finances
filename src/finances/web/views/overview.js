import {card, row, el, eur, get, note, lineChart, legend} from '../ui.js';

const LABELS = {cash: 'Cash', savings: 'Savings', investment: 'Investments'};
const RANGES = [['6m', 6], ['12m', 12], ['2y', 24], ['All', null]];
const signed = n => (n === null || n === undefined) ? null : `${n >= 0 ? '+' : ''}${eur(n)}`;

function savedRange() {
  try { return localStorage.getItem('range') || '12m'; } catch (e) { return '12m'; }
}

export async function render(root) {
  const d = await get('/api/evolution?months=600');
  if (!d.rows.length) {
    root.replaceChildren(card('Total', note('Nothing to show yet. Tap Sync.')));
    return;
  }
  const last = d.latest;
  const parts = [signed(d.changes.total.month) && `${signed(d.changes.total.month)} vs last month`,
    signed(d.changes.total.year) && `${signed(d.changes.total.year)} vs a year ago`].filter(Boolean);
  const total = card('Total', el('div', {className: 'big', textContent: eur(last.total)}),
    note(parts.length ? parts.join(' · ')
      : 'The change appears once every account has history for two months.'));

  const chartCard = card('Evolution');
  let range = savedRange();
  const draw = () => {
    const entry = RANGES.find(r => r[0] === range) || RANGES[1];
    const rows = entry[1] ? d.rows.slice(-entry[1]) : d.rows;
    const kinds = ['cash', 'savings', 'investment'].filter(k => rows.some(r => r[k] !== null));
    const series = [...kinds.map(k => ({label: LABELS[k], values: rows.map(r => r[k])})),
      {label: 'Total', values: rows.map(r => r.partial ? null : r.total)}];
    const buttons = el('div', {className: 'ranges'}, ...RANGES.map(([name]) => {
      const b = el('button', {textContent: name === 'All' ? 'Since start' : name, className: name === range ? 'on' : ''});
      b.addEventListener('click', () => {
        range = name;
        try { localStorage.setItem('range', name); } catch (e) { /* private mode */ }
        draw();
      });
      return b;
    }));
    const points = Math.max(...series.map(s => s.values.filter(v => v !== null).length));
    chartCard.replaceChildren(el('h2', {textContent: 'Evolution'}), buttons,
      points >= 2 ? lineChart(series, rows.map(r => r.month)) : null, legend(series),
      note(points >= 2
        ? 'Tap or drag on the chart to read a month. The total line only covers months where every account is known. Investments start when you first synced.'
        : `Not enough history for a line yet (${rows.length} month${rows.length === 1 ? '' : 's'} with data). Investments gain one point per sync day.`));
  };
  draw();
  root.replaceChildren(total, chartCard);
}
