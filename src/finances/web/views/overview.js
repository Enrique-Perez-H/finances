import {card, row, el, eur, eur2, get, note, lineChart, barChart, legend} from '../ui.js';

const LABELS = {cash: 'Cash', savings: 'Savings', investment: 'Investments'};
const RANGES = [['6m', 6], ['12m', 12], ['2y', 24], ['All', null]];
const signed = n => (n === null || n === undefined) ? null : `${n >= 0 ? '+' : ''}${eur(n)}`;
const INCOME = 'var(--good)', SPENDING = 'var(--bad)';

function savedRange() {
  try { return localStorage.getItem('range') || '12m'; } catch (e) { return '12m'; }
}

export async function render(root) {
  const [d, flow] = await Promise.all([get('/api/evolution?months=600'), get('/api/cashflow?months=600')]);
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

  const evolutionCard = card('Evolution');
  const flowCard = card('Income and spending per month');
  let range = savedRange();

  const buttons = () => el('div', {className: 'ranges'}, ...RANGES.map(([name]) => {
    const b = el('button', {textContent: name === 'All' ? 'Since start' : name, className: name === range ? 'on' : ''});
    b.addEventListener('click', () => {
      range = name;
      try { localStorage.setItem('range', name); } catch (e) { /* private mode */ }
      draw();
    });
    return b;
  }));
  const windowOf = rows => {
    const entry = RANGES.find(r => r[0] === range) || RANGES[1];
    return entry[1] ? rows.slice(-entry[1]) : rows;
  };

  // Biggest single spends and incomes of the month selected on the income and spending chart.
  let topToken = 0;
  const topCard = card('Top 3');
  const topBox = el('div', {className: 'top'});
  topCard.append(topBox);
  const showTop = async month => {
    const token = ++topToken;
    let top;
    try { top = await get(`/api/top?month=${month}&n=3`); } catch (e) { return; }
    if (token !== topToken) return;  // a newer selection has replaced this one
    const list = (title, items, cls) => el('div', {},
      el('div', {className: 'muted', textContent: title}),
      ...(items.length ? items.map(t => el('div', {className: 'entry'},
        el('div', {className: 'row'}, el('span', {textContent: t.description.slice(0, 34)}),
          el('span', {className: cls, textContent: eur2(t.amount)})),
        note(`${t.date} · ${t.category}`))) : [note('None')]));
    topCard.querySelector('h2').textContent = `Top 3 in ${month}`;
    topBox.replaceChildren(list('Biggest spends', top.spends, 'neg'), list('Biggest incomes', top.incomes, 'pos'),
      note('Tap a month on the income and spending chart to change it.'));
  };

  const drawEvolution = () => {
    const rows = windowOf(d.rows);
    const kinds = ['cash', 'savings', 'investment'].filter(k => rows.some(r => r[k] !== null));
    const series = [...kinds.map(k => ({label: LABELS[k], values: rows.map(r => r[k])})),
      {label: 'Total', values: rows.map(r => r.total)}];  // sum of the known parts
    const points = Math.max(...series.map(s => s.values.filter(v => v !== null).length));
    evolutionCard.replaceChildren(el('h2', {textContent: 'Evolution'}), buttons(),
      points >= 2 ? lineChart(series, rows.map(r => r.month), {
        notes: rows.map(r => r.partial ? 'Total leaves out accounts with no data for this month.' : '')}) : null,
      legend(series),
      note(points >= 2
        ? 'Tap or drag on the chart to read a month. The total is the sum of the lines that have data; early months leave out investments, which start when you first synced.'
        : `Not enough history for a line yet (${rows.length} month${rows.length === 1 ? '' : 's'} with data). Investments gain one point per sync day.`));
  };

  const drawFlow = () => {
    const rows = windowOf(flow);
    if (!rows.length) {
      flowCard.replaceChildren(el('h2', {textContent: 'Income and spending per month'}), note('No transactions yet.'));
      return;
    }
    const series = [{label: 'Income', values: rows.map(r => r.income), color: INCOME},
      {label: 'Spending', values: rows.map(r => r.spending), color: SPENDING}];
    const done = rows.filter(r => !r.partial);
    const sum = k => done.reduce((a, r) => a + r[k], 0);
    const avg = k => (done.length ? sum(k) / done.length : 0);
    const net = r => `${r.saved >= 0 ? '+' : ''}${eur(r.saved)}`;
    const table = el('table', {className: 'months'},
      el('tr', {}, ...['Month', 'In', 'Out', 'Net'].map(h => el('th', {textContent: h}))),
      ...rows.slice().reverse().map(r => el('tr', {},
        el('td', {textContent: r.month + (r.partial ? ' *' : '')}),
        el('td', {className: 'pos', textContent: eur(r.income)}),
        el('td', {className: 'neg', textContent: eur(r.spending)}),
        el('td', {className: r.saved >= 0 ? 'pos' : 'neg', textContent: net(r)}))),
      done.length ? el('tr', {className: 'avg'},
        el('td', {textContent: 'Average'}), el('td', {textContent: eur(avg('income'))}),
        el('td', {textContent: eur(avg('spending'))}),
        el('td', {textContent: `${avg('saved') >= 0 ? '+' : ''}${eur(avg('saved'))}`})) : null);
    const rate = sum('income') > 0 ? Math.round((sum('saved') / sum('income')) * 100) : null;
    flowCard.replaceChildren(el('h2', {textContent: 'Income and spending per month'}), buttons(),
      barChart(series, rows.map(r => r.month), {
        notes: rows.map(r => r.partial ? 'Month in progress: not final yet.' : ''),
        onSelect: i => showTop(rows[i].month)}),
      legend(series),
      done.length ? row('Average month', `${eur(avg('income'))} in · ${eur(avg('spending'))} out`) : null,
      rate === null ? null : row('Saved over this period', `${rate}% of income`, rate >= 0 ? 'pos' : 'neg'),
      table,
      note('* month in progress. Money moved between your own accounts, one-off capital items and reimbursed payments are not counted.'));
  };

  const draw = () => { drawEvolution(); drawFlow(); };
  draw();
  root.replaceChildren(total, evolutionCard, flowCard, topCard);
}
