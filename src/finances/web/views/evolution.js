import {card, row, el, eur, get, post, note, barRow, lineChart, legend} from '../ui.js';

const LABELS = {cash: 'Cash', savings: 'Savings', investment: 'Investments', total: 'Total'};
const signed = n => (n === null || n === undefined) ? '–' : `${n >= 0 ? '+' : ''}${eur(n)}`;
let showAll = false;

export async function render(root) {
  const d = await get('/api/evolution');
  const reload = () => render(root);
  if (!d.rows.length) {
    root.replaceChildren(card('Evolution', note('Nothing to show yet. Sync first.')));
    return;
  }
  const kinds = ['cash', 'savings', 'investment'].filter(k => d.rows.some(r => r[k] !== null));
  const series = [...kinds.map(k => ({label: LABELS[k], values: d.rows.map(r => r[k])})),
    {label: 'Total (complete months)', values: d.rows.map(r => r.partial ? null : r.total)}];
  const last = d.latest;

  const now = card('Total by type', el('div', {className: 'big', textContent: eur(last.total)}),
    note(d.changes.total.month === null ? 'Total change appears once every account has history for two months.'
      : `Total · ${signed(d.changes.total.month)} vs last month${d.changes.total.year === null ? '' : ` · ${signed(d.changes.total.year)} vs a year ago`}`),
    ...kinds.map(k => row(`${LABELS[k]}  (${signed(d.changes[k].month)} / month)`, eur(last[k]))));

  const chart = card('Evolution', lineChart(series, d.rows.map(r => r.month)), legend(series),
    note('Cash and savings are rebuilt from your transactions. Investments start when you first synced, earlier months are unknown.'));

  const rows = showAll ? d.rows.slice().reverse() : d.rows.slice(-12).reverse();
  const table = card('Month by month', ...rows.map(r => el('div', {className: 'review'},
    row(r.month + (r.partial ? ' (some accounts unknown)' : ''), eur(r.total ?? 0)),
    note(kinds.map(k => `${LABELS[k]} ${r[k] === null ? '?' : eur(r[k])}`).join(' · ')))),
    d.rows.length > 12 ? el('button', {className: 'ghost', textContent: showAll ? 'Show last 12' : 'Show all',
      onclick: () => { showAll = !showAll; reload(); }}) : null);

  const i = d.interest;
  const max = Math.max(...i.monthly.map(m => m.amount), 0);
  const interest = card('Interest and dividends received',
    el('div', {className: 'big', textContent: eur(i.last_12_months)}),
    note(`last 12 months · about ${eur(i.average_per_month)} per month · ${eur(i.total)} in total`),
    ...i.monthly.slice(-12).map(m => barRow(m.month, m.amount, max, eur(m.amount))),
    ...i.by_account.map(a => row(a.account, eur(a.total))),
    i.monthly.length ? null : note('None found yet. Trade Republic interest appears after the next Trade Republic sync.'));

  const types = card('Account types',
    note('Choose how each account counts. For example, set Trade Republic cash to Investments if you treat it as part of it.'),
    ...d.accounts.map(a => {
      const sel = el('select', {}, ...['cash', 'savings', 'investment'].map(k =>
        el('option', {value: k, textContent: LABELS[k], selected: k === a.kind})));
      sel.addEventListener('change', async () => { await post('/api/accounts/kind', {id: a.id, kind: sel.value}); reload(); });
      return el('div', {className: 'review'}, row(a.name, eur(a.balance)), sel);
    }));

  root.replaceChildren(now, chart, interest, table, types);
}
