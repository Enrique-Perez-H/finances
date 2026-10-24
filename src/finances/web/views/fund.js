import {card, row, el, eur, eur2, get, post, note, button, lineChart, legend} from '../ui.js';

const FIELDS = [
  ['name', 'Name', 'text'], ['start_date', 'Start date', 'date'],
  ['initial', 'Starting amount (€)', 'number'], ['monthly', 'Monthly contribution (€)', 'number'],
  ['annual_rate', 'Interest per year (%, effective)', 'number'], ['years', 'Years', 'number'],
  ['contribution_growth', 'Contribution increase per year (%)', 'number'],
  ['tax_rate', 'Tax on the gain at the end (%)', 'number'], ['target', 'Target amount (€, optional)', 'number'],
];
const NEW = {name: 'Fund', start_date: new Date().toISOString().slice(0, 10), initial: 0, monthly: 100,
  annual_rate: 3, years: 18, contribution_growth: 0, tax_rate: 0, target: '', link_account_id: ''};

let editing = null;  // null | 'new' | plan id

export async function render(root) {
  const [plans, summary] = await Promise.all([get('/api/funds'), get('/api/summary')]);
  const reload = () => render(root);
  const parts = plans.map(p => editing === p.plan.id
    ? editor(p.plan, summary.accounts, reload) : view(p, reload));
  if (editing === 'new') parts.push(editor(NEW, summary.accounts, reload));
  if (!plans.length && editing !== 'new') {
    parts.push(card('Fund', note('Model a fund for your son: contributions, interest, tax and a target. Nothing here touches any bank.')));
  }
  if (editing !== 'new') parts.push(el('div', {}, button('+ New fund', () => { editing = 'new'; reload(); })));
  root.replaceChildren(...parts);
}

function results(f) {
  const series = [
    {label: 'Balance', values: [f.initial, ...f.years.map(y => y.balance)]},
    {label: 'You put in', values: [f.initial, ...f.years.map(y => y.contributed)]}];
  return el('div', {},
    el('div', {className: 'big', textContent: eur(f.final)}),
    note(`after ${f.years.length} years (${f.years.at(-1).year}) · ${eur(f.contributed)} contributed · ${eur(f.interest)} interest`),
    lineChart(series, ['start', String(f.years.at(-1).year)]), legend(series),
    f.tax > 0 ? row('After tax', eur(f.final_after_tax)) : null,
    ...(f.required_monthly !== null ? [f.target_reached
      ? el('div', {className: 'pos', textContent: 'Target reached with these assumptions.'})
      : row('Monthly needed for the target', eur2(f.required_monthly))] : []),
    el('div', {className: 'muted', textContent: 'If the interest rate were different:'}),
    ...f.scenarios.map(s => row(`${s.rate.toFixed(1)}% per year`, eur(s.final))));
}

function view({plan, projection: f}, reload) {
  const table = el('details', {}, el('summary', {textContent: 'Year by year'}),
    ...f.years.map(y => row(`${y.year} (year ${y.age})`, `${eur(y.balance)} · interest ${eur(y.interest)}`)));
  return card(plan.name, results(f), table,
    el('div', {}, button('Edit', () => { editing = plan.id; reload(); }, 'ghost')));
}

function editor(plan, accounts, reload) {
  const inputs = {};
  const form = el('div', {}, ...FIELDS.map(([k, label, type]) => {
    inputs[k] = el('input', {type, value: plan[k] ?? '', className: 'wide'});
    if (type === 'number') inputs[k].setAttribute('step', 'any');
    return el('label', {}, el('div', {className: 'muted', textContent: label}), inputs[k]);
  }));
  const link = el('select', {}, el('option', {value: '', textContent: 'No: use the starting amount'}),
    ...accounts.map(a => el('option', {value: a.id, textContent: `${a.name} (${eur(a.balance)})`,
      selected: a.id === plan.link_account_id})));
  form.append(el('label', {}, el('div', {className: 'muted', textContent: 'Start from the real balance of an account?'}), link));
  const out = el('div', {});
  const body = () => ({id: plan.id, ...Object.fromEntries(FIELDS.map(([k]) => [k, inputs[k].value])),
    link_account_id: link.value});
  const calc = async () => {
    try { out.replaceChildren(results(await post('/api/funds/preview', body()))); }
    catch (e) { out.replaceChildren(el('div', {className: 'neg', textContent: e.message})); }
  };
  const save = async () => {
    try { await post('/api/funds', body()); editing = null; reload(); }
    catch (e) { out.replaceChildren(el('div', {className: 'neg', textContent: e.message})); }
  };
  const actions = el('div', {}, button('Calculate', calc), ' ', button('Save', save), ' ',
    button('Cancel', () => { editing = null; reload(); }, 'ghost'));
  if (plan.id) {
    actions.append(' ', button('Delete', async () => {
      if (confirm(`Delete "${plan.name}"?`)) { await post('/api/funds/delete', {id: plan.id}); editing = null; reload(); }
    }, 'ghost'));
  }
  calc();
  return card(plan.id ? 'Edit fund' : 'New fund', form, actions, out);
}
