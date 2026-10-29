import {card, row, el, eur, eur2, get, post, note, button} from '../ui.js';

const item = (i, reload) => {
  const detail = [i.category];
  if (i.kind === 'presence') detail.push(`appears most months, no fixed day · ${i.months_seen}`);
  if (i.variable) detail.push(`amount varies ${eur2(i.amount_min)} to ${eur2(i.amount_max)}`);
  detail.push(`×${i.occurrences} · next ~${i.next_date}${i.active ? '' : ' · looks ended'}`);
  return el('div', {className: 'review'},
    row(i.label.slice(0, 34), i.kind === 'presence' ? `~${eur2(i.monthly_equivalent)}/mo`
      : `${eur2(i.typical_amount)} ${i.cadence}`, i.direction === 'in' ? 'pos' : ''),
    note(detail.join(' · ')),
    button('Not recurring', async () => { await post('/api/recurring/ignore', {key: i.key, direction: i.direction}); reload(); }, 'ghost'));
};

export async function render(root) {
  const d = await get('/api/recurring');
  const reload = () => render(root);
  const costs = d.items.filter(i => i.direction === 'out' && i.active);
  const incoming = d.items.filter(i => i.direction === 'in' && i.active);
  const ended = d.items.filter(i => !i.active);
  const hidden = d.hidden.length ? el('details', {className: 'card'},
    el('summary', {textContent: `Hidden by you (${d.hidden.length})`}),
    ...d.hidden.map(h => el('div', {className: 'row'}, el('span', {textContent: `${h.key} (${h.direction === 'in' ? 'money in' : 'money out'})`}),
      button('Restore', async () => { await post('/api/recurring/restore', h); reload(); }, 'ghost')))) : null;
  root.replaceChildren(
    card('Recurring costs', el('div', {className: 'big', textContent: `${eur(d.monthly_costs)}/mo`}),
      note(`${eur(d.yearly_costs)} per year · recurring income ${eur(d.monthly_income)}/mo`),
      note('Found from repeated payments: a steady rhythm, or a payee that shows up almost every month even with a different date and amount. Variable ones count at their real monthly average.')),
    ...(d.upcoming.length ? [card('Coming up', ...d.upcoming.map(i =>
      row(`${i.next_date} · ${i.label.slice(0, 26)}`, eur2(i.typical_amount))))] : []),
    card('All recurring costs', ...(costs.length ? costs.map(i => item(i, reload)) : [note('None detected yet.')])),
    ...(incoming.length ? [card('Recurring income', ...incoming.map(i => item(i, reload)))] : []),
    ...(ended.length ? [card('Looks ended', ...ended.map(i => item(i, reload)))] : []),
    ...(hidden ? [hidden] : []),
  );
}
