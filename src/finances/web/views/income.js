import {card, row, el, eur, eur2, get, note, barRow, lineChart, legend} from '../ui.js';

export async function render(root) {
  const d = await get('/api/income');
  if (!d.categories.length) {
    root.replaceChildren(card('Income', note('No income found yet. Sync first.')));
    return;
  }
  const series = [
    {label: 'Salary', values: d.monthly.map(m => m.salary)},
    {label: 'Everything else', values: d.monthly.map(m => m.other)}];
  const max = d.categories[0].total;
  root.replaceChildren(
    card('Income', el('div', {className: 'big', textContent: eur(d.monthly_average)}),
      note(`average per month over ${d.months.length} months · ${eur(d.total)} in total`),
      lineChart(series, d.months), legend(series),
      note(`Completed months only (${d.excluded_month} is excluded until it ends). Internal transfers between your own accounts are not counted.`)),
    card('What you receive, by type', ...d.categories.map(c =>
      barRow(c.category, c.total, max, `${eur(c.monthly_average)}/mo · ×${c.count}`))),
    card('Top sources', ...d.sources.slice(0, 10).map(s =>
      el('div', {className: 'review'}, row(s.label.slice(0, 34), eur2(s.total), 'pos'),
        note(`${s.category} · ×${s.count} · avg ${eur2(s.average)} · last ${s.last}`)))),
  );
}
