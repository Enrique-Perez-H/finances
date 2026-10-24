import {card, row, el, eur, get, lineChart, note} from '../ui.js';

export async function render(root) {
  const d = await get('/api/summary');
  root.replaceChildren(
    card('Net worth', el('div', {className: 'big', textContent: eur(d.net_worth.total)}),
      ...Object.entries(d.net_worth.by_kind).map(([k, v]) => row(k, eur(v)))),
    card('Accounts', ...d.accounts.map(a => row(`${a.name} · ${a.provider}`, eur(a.balance)))),
    card('Monthly cash flow', ...d.cashflow.map(m =>
      row(m.month, (m.saved >= 0 ? '+' : '') + eur(m.saved), m.saved >= 0 ? 'pos' : 'neg')),
      note('Internal transfers between your own accounts are excluded.')),
    ...(d.holdings.length ? [card('Investments', ...d.holdings.map(h =>
      row(h.symbol, `${eur(h.value)} (${h.cost ? (((h.value / h.cost) - 1) * 100).toFixed(1) : '–'}%)`)))] : []),
  );
}
