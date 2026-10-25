import {card, row, el, eur, eur2, get, post, note, button, lineChart, legend} from '../ui.js';

const pct = n => `${(n * 100).toFixed(1)}%`;
let editing = false;
let showAll = false;

export async function render(root) {
  const d = await get('/api/sonfund');
  const reload = () => render(root);
  if (!d.configured || editing) {
    root.replaceChildren(settingsCard(d, reload));
    return;
  }
  const rows = d.rows, proj = d.projection;
  const months = [...rows.map(r => r.month), ...proj.map(r => r.month)];
  const lastActual = rows.length ? rows[rows.length - 1].son_balance : d.config.initial;
  const series = [
    {label: 'So far', values: [...rows.map(r => r.son_balance), ...proj.map(() => null)]},
    {label: 'Projection', values: [...rows.map((r, i) => (i === rows.length - 1 ? lastActual : null)),
      ...proj.map(r => r.son_balance)], color: undefined}];

  const head = card(d.config.name, el('div', {className: 'big', textContent: eur(d.son_balance)}),
    note(`his part of the account today${d.share_now === null ? '' : ` · ${pct(d.share_now)} of ${eur(d.account_balance)}`}`),
    row('Interest credited to him, last 12 months', eur2(d.interest_last_12)),
    row('Interest credited to him, in total', eur2(d.interest_total)),
    row('Deposits set aside for him', eur2(d.deposits_total)),
    d.rate_used === null ? null : row('Rate used for the projection', `${d.rate_used.toFixed(2)}% a year`),
    d.rate_used !== null && d.config.annual_rate === null ? note('That rate is estimated from the interest you really received.') : null,
    d.projected_end === null ? null : row(`In ${d.config.years} years`, eur(d.projected_end)),
    ...d.notes.map(note));

  const chart = card('How his part evolves',
    months.length >= 2 ? lineChart(series, months) : null, legend(series),
    note('Each month the interest paid on the account is split by his share of it, and his part stays in and earns interest too. The projection assumes the rate above plus any monthly amount you set.'));

  const shown = showAll ? rows.slice().reverse() : rows.slice(-12).reverse();
  const table = card('Month by month', ...(shown.length ? shown.map(r => el('div', {className: 'review'},
    row(r.month + (r.partial ? ' (so far)' : ''), eur2(r.son_balance)),
    note(r.share === null ? 'Account balance unknown for this month.'
      : `${pct(r.share)} of the account · interest paid ${eur2(r.interest)} → his ${eur2(r.son_interest)}`
        + (r.deposits ? ` · deposit ${eur2(r.deposits)}` : '')))) : [note('No months yet after the start date.')]),
    rows.length > 12 ? button(showAll ? 'Show last 12' : 'Show all', () => { showAll = !showAll; reload(); }, 'ghost') : null);

  const deposits = card('Deposits set aside for him',
    note('Money you moved into the account for him. It joins his part from that month on.'),
    ...d.deposits.map(x => el('div', {className: 'row'},
      el('span', {textContent: `${x.date} · ${eur2(x.amount)}${x.note ? ' · ' + x.note : ''}`}),
      button('✕', async () => { await post('/api/sonfund/deposit/delete', {id: x.id}); reload(); }, 'ghost'))),
    depositForm(reload));

  root.replaceChildren(head, chart, table, deposits,
    el('div', {}, button('Edit settings', () => { editing = true; reload(); }, 'ghost')));
}

function depositForm(reload) {
  const date = el('input', {type: 'date', value: new Date().toISOString().slice(0, 10), className: 'wide'});
  const amount = el('input', {type: 'number', step: 'any', placeholder: 'Amount (€)', className: 'wide'});
  const noteBox = el('input', {type: 'text', placeholder: 'Note (optional)', className: 'wide'});
  const out = el('div', {className: 'neg'});
  return el('div', {}, date, amount, noteBox,
    button('Add deposit', async () => {
      try { await post('/api/sonfund/deposit', {date: date.value, amount: amount.value, note: noteBox.value}); reload(); }
      catch (e) { out.textContent = e.message; }
    }), out);
}

function settingsCard(d, reload) {
  const c = d.config ?? {name: 'Fund', start_date: new Date().toISOString().slice(0, 10), initial: 0,
    monthly: 0, annual_rate: null, years: 18, account_id: (d.accounts.find(a => /trade|tr-/i.test(a.id + a.name)) ?? d.accounts[0])?.id};
  const fields = [
    ['name', 'Name', 'text', c.name], ['start_date', 'His part starts on', 'date', c.start_date],
    ['initial', 'How much of the account is his on that date (€)', 'number', c.initial],
    ['monthly', 'Planned extra deposit per month, projection only (€)', 'number', c.monthly],
    ['annual_rate', 'Interest rate for the projection (% a year), empty = estimate from real payments', 'number', c.annual_rate ?? ''],
    ['years', 'Years to project', 'number', c.years]];
  const inputs = {};
  const form = el('div', {}, ...fields.map(([k, label, type, value]) => {
    inputs[k] = el('input', {type, value, className: 'wide'});
    if (type === 'number') inputs[k].setAttribute('step', 'any');
    return el('label', {}, el('div', {className: 'muted', textContent: label}), inputs[k]);
  }));
  const account = el('select', {className: 'picker'}, ...d.accounts.map(a =>
    el('option', {value: a.id, textContent: `${a.name} (${eur(a.balance)})`, selected: a.id === c.account_id})));
  form.append(el('label', {}, el('div', {className: 'muted', textContent: 'Account whose interest is shared'}), account));
  const out = el('div', {className: 'neg'});
  const save = async () => {
    try {
      await post('/api/sonfund', {...Object.fromEntries(fields.map(([k]) => [k, inputs[k].value])), account_id: account.value});
      editing = false;
      reload();
    } catch (e) { out.textContent = e.message; }
  };
  return card(d.config ? 'Settings' : 'Your son’s fund',
    note('Keep the money in your Trade Republic account. Say how much of it is his, and the interest paid on the account is shared in that proportion, month after month.'),
    form, button('Save', save), ' ',
    d.config ? button('Cancel', () => { editing = false; reload(); }, 'ghost') : null, out);
}
