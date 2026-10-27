import {card, row, el, eur, eur2, get, note, button, barRow, downloadPost} from '../ui.js';

let year = '';
const pct = n => `${(n * 100).toFixed(0)}%`;

export async function render(root) {
  const d = await get('/api/yearly' + (year ? `?year=${year}` : ''));
  year = d.year;
  const reload = () => render(root);
  if (!d.years.length) {
    root.replaceChildren(card('Yearly', note('Nothing to show yet. Sync first.')));
    return;
  }

  const picker = el('select', {}, ...d.years.map(y => el('option', {value: y, textContent: y, selected: y === year})));
  picker.addEventListener('change', () => { year = picker.value; reload(); });
  const head = card(`Year ${d.year}`, el('div', {}, picker),
    row('Income', eur(d.income.total), 'pos'), row('Spending', eur(d.spending.total), 'neg'),
    row('Saved', `${d.saved >= 0 ? '+' : ''}${eur(d.saved)}${d.savings_rate === null ? '' : ` (${pct(d.savings_rate)})`}`,
      d.saved >= 0 ? 'pos' : 'neg'),
    note('Informational. Check the figures against your bank and broker certificates; this is not tax advice.'));

  const maxIn = d.income.by_category[0]?.total ?? 0, maxOut = d.spending.by_category[0]?.total ?? 0;
  const income = card('Income by type', ...(d.income.by_category.length
    ? d.income.by_category.map(c => barRow(c.category, c.total, maxIn, eur2(c.total))) : [note('None')]));
  const interest = card('Interest and dividends',
    el('div', {className: 'big', textContent: eur2(d.interest.total)}),
    row('Interest on cash', eur2(d.interest.interest)), row('Dividends and other', eur2(d.interest.other)),
    ...d.interest.accounts.map(a => note(`${a.account}: interest ${eur2(a.interest)} · other ${eur2(a.other)}`)),
    note('Interest is the Trade Republic cash payout, already net of tax. Anything else under this type shows as other.'));
  const spending = card('Spending by category', ...(d.spending.by_category.length
    ? d.spending.by_category.map(c => barRow(c.category, c.total, maxOut, eur2(c.total))) : [note('None')]));
  const taxes = card('Taxes and fees paid', el('div', {className: 'big', textContent: eur2(d.taxes_paid.total)}),
    ...d.taxes_paid.top.map(t => row(`${t.date} · ${t.description.slice(0, 26)}`, eur2(t.amount))),
    d.taxes_paid.top.length ? note('Largest five.') : null);
  const sources = card('Main sources of income', ...d.income_sources.map(s => el('div', {className: 'review'},
    row(s.label.slice(0, 34), eur2(s.total), 'pos'), note(`${s.category} · ×${s.count}`))));

  const n = d.not_counted;
  const notCounted = el('details', {className: 'card'},
    el('summary', {textContent: 'Not counted above'}),
    note('Shown so the totals are not a mystery.'),
    row('Moved between your own accounts', `${eur2(n.internal_out)} out · ${eur2(n.internal_in)} in`),
    row('One-off capital items', `${eur2(n.capital_out)} out · ${eur2(n.capital_in)} in`),
    row(`Reimbursed payments (${n.reimbursed_pairs})`, `${eur2(n.reimbursed_spent)} spent · ${eur2(n.reimbursed_received)} received`));

  const table = el('table', {className: 'months'},
    el('tr', {}, ...['Month', 'In', 'Out', 'Net'].map(h => el('th', {textContent: h}))),
    ...d.monthly.map(m => el('tr', {},
      el('td', {textContent: m.month.slice(5)}), el('td', {className: 'pos', textContent: eur(m.income)}),
      el('td', {className: 'neg', textContent: eur(m.spending)}),
      el('td', {className: m.saved >= 0 ? 'pos' : 'neg', textContent: `${m.saved >= 0 ? '+' : ''}${eur(m.saved)}`}))));
  const months = card('Month by month', table);

  const out = el('div', {className: 'muted'});
  const exporter = (label, kind, style, y = d.year) => button(label, async () => {
    out.textContent = 'Preparing…';
    try { out.textContent = `Saved ${await downloadPost('/api/export', {year: y, kind, style})} in your Downloads.`; }
    catch (e) { out.textContent = 'Failed: ' + e.message; }
  }, 'chip');
  const exportCard = card('Export',
    note('Spanish Excel files use semicolons and decimal commas. Standard CSV uses commas and dots (Google Sheets, other tools).'),
    el('div', {className: 'chips'}, exporter(`${d.year} entries (Excel, Spain)`, 'entries', 'es'),
      exporter(`${d.year} entries (CSV)`, 'entries', 'csv'),
      exporter(`${d.year} summary (Excel, Spain)`, 'summary', 'es'), exporter(`${d.year} summary (CSV)`, 'summary', 'csv'),
      exporter('All years, entries (Excel, Spain)', 'entries', 'es', 'all')), out);

  root.replaceChildren(head, income, interest, spending, taxes, sources, notCounted, months, exportCard);
}
