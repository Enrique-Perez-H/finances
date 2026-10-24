import {card, row, el, eur, eur2, get, note, button, lineChart, legend} from '../ui.js';

const FIELDS = [
  ['months', 'Months ahead', 24, 1],
  ['income_growth', 'Salary growth per year (%)', 0, 0.5],
  ['inflation', 'Cost growth per year (%)', 2, 0.5],
  ['return', 'Investment return per year (%)', 4, 0.5],
  ['invest_share', 'Share of monthly savings invested (%)', 0, 5],
  ['extra_income', 'Extra income per month (€)', 0, 50],
  ['extra_cost', 'Extra cost per month (€)', 0, 50],
];

function saved() {
  try { return JSON.parse(localStorage.getItem('forecast') || '{}'); } catch (e) { return {}; }
}

export async function render(root) {
  const values = Object.fromEntries(FIELDS.map(([k, , d]) => [k, saved()[k] ?? d]));
  const other = saved().other_income ?? true;
  const inputs = {};
  const form = el('div', {}, ...FIELDS.map(([k, label, , step]) => {
    inputs[k] = el('input', {type: 'number', value: values[k], step, className: 'wide'});
    inputs[k].setAttribute('inputmode', 'decimal');
    return el('label', {}, el('div', {className: 'muted', textContent: label}), inputs[k]);
  }));
  const otherBox = el('input', {type: 'checkbox', checked: other});
  form.append(el('label', {className: 'muted'}, otherBox, ' Keep counting non-recurring income'));
  const out = el('div', {});
  const run = async () => {
    const q = new URLSearchParams(Object.fromEntries(FIELDS.map(([k]) => [k, inputs[k].value || 0])));
    q.set('other_income', otherBox.checked ? '1' : '0');
    try {
      localStorage.setItem('forecast', JSON.stringify({
        ...Object.fromEntries(FIELDS.map(([k]) => [k, inputs[k].value])), other_income: otherBox.checked}));
    } catch (e) { /* private mode */ }
    try { draw(out, await get('/api/forecast?' + q)); }
    catch (e) { out.replaceChildren(card('', note(e.message))); }
  };
  root.replaceChildren(card('Assumptions', form, button('Recalculate', run)), out);
  await run();
}

function draw(out, f) {
  const b = f.baseline;
  if (!b.months_used) {
    out.replaceChildren(card('Forecast', note('Not enough history yet: needs at least one completed month of transactions.')));
    return;
  }
  const series = [
    {label: 'Cash & savings', values: f.rows.map(r => r.liquid)},
    {label: 'Investments', values: f.rows.map(r => r.invested)},
    {label: 'Total', values: f.rows.map(r => r.total)}];
  const marks = [6, 12, 24, 36, 60, 120].filter(m => m <= f.rows.length);
  out.replaceChildren(
    card('Projected wealth', el('div', {className: 'big', textContent: eur(f.end_total)}),
      note(`in ${f.rows.length} months, from ${eur(f.start_total)} today`),
      lineChart(series, f.rows.map(r => r.month)), legend(series),
      f.runway_month ? el('div', {className: 'neg', textContent: `Cash runs out in ${f.runway_month} at this pace.`})
                     : note('Cash stays positive over the whole period.')),
    card('Per month (average)',
      row('Saved', eur(f.average_net), f.average_net >= 0 ? 'pos' : 'neg'),
      f.savings_rate === null ? null : row('Savings rate', `${(f.savings_rate * 100).toFixed(0)}%`)),
    card('Checkpoints', ...marks.map(m => {
      const r = f.rows[m - 1];
      return row(`${r.month} (${m} mo)`, eur(r.total));
    })),
    card(`Baseline (last ${b.months_used} full months)`,
      row('Recurring income', eur2(b.recurring_income)), row('Other income', eur2(b.other_income)),
      row('Recurring costs', eur2(b.recurring_costs)), row('Other spending', eur2(b.variable_costs)),
      note('These come from your transactions; the assumptions above are yours. It is an estimate, not advice.')),
  );
}
