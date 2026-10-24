import {card, row, el, eur, eur2, get, note} from '../ui.js';

const item = i => el('div', {className: 'review'},
  row(i.label.slice(0, 34), `${eur2(i.typical_amount)} ${i.cadence}`, i.direction === 'in' ? 'pos' : ''),
  note(`${i.category}${i.variable ? ' · amount varies' : ''} · ×${i.occurrences} · next ~${i.next_date}` +
       (i.active ? '' : ' · looks ended')));

export async function render(root) {
  const d = await get('/api/recurring');
  const costs = d.items.filter(i => i.direction === 'out' && i.active);
  const incoming = d.items.filter(i => i.direction === 'in' && i.active);
  const ended = d.items.filter(i => !i.active);
  root.replaceChildren(
    card('Recurring costs', el('div', {className: 'big', textContent: `${eur(d.monthly_costs)}/mo`}),
      note(`${eur(d.yearly_costs)} per year · recurring income ${eur(d.monthly_income)}/mo`),
      note('Detected automatically from repeated payments; amounts are typical values.')),
    ...(d.upcoming.length ? [card('Coming up', ...d.upcoming.map(i =>
      row(`${i.next_date} · ${i.label.slice(0, 26)}`, eur2(i.typical_amount))))] : []),
    card('All recurring costs', ...(costs.length ? costs.map(item) : [note('None detected yet (needs 3 or more repeats).')])),
    ...(incoming.length ? [card('Recurring income', ...incoming.map(item))] : []),
    ...(ended.length ? [card('Looks ended', ...ended.map(item))] : []),
  );
}
