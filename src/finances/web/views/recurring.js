import {card, row, el, eur, eur2, get, post, note, button, donut} from '../ui.js';

const CADENCES = ['monthly', 'every two months', 'quarterly', 'semiannual', 'yearly'];

// Everything about an item you marked yourself: rename, change rhythm, name variants, entries.
// Search all entries (same direction as the item) and add the ones that belong to it.
// A detected item has no group yet: the first entry you add turns it into an item you manage.
function findCard(i, reload) {
  const q = el('input', {placeholder: 'Search entries, e.g. part of the name', className: 'wide', value: i.kind === 'manual' ? '' : (i.key || '')});
  const out = el('div', {});
  let groupId = i.group_id ?? null;
  const run = async () => {
    out.replaceChildren(note('Searching…'));
    const params = new URLSearchParams({q: q.value.trim(), direction: i.direction, sort: 'date', limit: 40});
    const res = await get('/api/entries?' + params);
    const list = res.items.filter(e => !(e.recurring && e.recurring.group_id === groupId && groupId !== null));
    out.replaceChildren(...(list.length ? list.map(e => {
      const r = el('div', {className: 'row'},
        el('span', {textContent: `${e.date} · ${e.description.slice(0, 26)} · ${eur2(Math.abs(e.amount))}` +
          (e.recurring ? ` · in "${e.recurring.name}"` : '')}));
      r.appendChild(button('Add', async () => {
        try {
          if (groupId === null) {
            const cadence = CADENCES.includes(i.cadence) ? i.cadence : 'monthly';
            groupId = (await post('/api/recurring/manual', {tx_ids: [e.id], name: i.label, cadence})).group_id;
            if (i.key && i.kind !== 'manual') { try { await post('/api/recurring/manual/pattern', {group_id: groupId, pattern: i.key}); } catch (err) { /* too generic to match by name */ } }
          } else {
            await post('/api/recurring/manual', {tx_ids: [e.id], group_id: groupId});
          }
          r.replaceChildren(el('span', {className: 'muted', textContent: `Added · ${e.date} · ${e.description.slice(0, 26)}`}));
        } catch (err) { alert(err.message); }
      }, 'ghost'));
      return r;
    }) : [note('No entries found.')]), button('Done', reload));
  };
  const go = button('Search', run);
  q.addEventListener('keydown', ev => { if (ev.key === 'Enter') run(); });
  return el('details', {className: 'group'},
    el('summary', {}, el('span', {textContent: 'Find entries to add'}), el('span', {textContent: i.direction === 'in' ? 'money in' : 'money out'})),
    note(i.kind === 'manual' ? 'Pick entries that belong to this item, even if their text differs.'
      : 'Adding an entry here turns this into an item you manage (rename, change rhythm, remove entries).'),
    q, go, out);
}

function manageCard(i, reload) {
  const name = el('input', {value: i.label, className: 'wide'});
  const cadence = el('select', {className: 'picker'}, ...CADENCES.map(c => el('option', {value: c, textContent: c, selected: c === i.cadence})));
  const save = button('Save', async () => {
    try { await post('/api/recurring/manual/update', {group_id: i.group_id, name: name.value, cadence: cadence.value}); reload(); }
    catch (e) { alert(e.message); }
  });
  const pattern = el('input', {placeholder: 'e.g. limpiezas jaca', className: 'wide'});
  const addPattern = button('Add', async () => {
    try { await post('/api/recurring/manual/pattern', {group_id: i.group_id, pattern: pattern.value}); reload(); }
    catch (e) { alert(e.message); }
  });
  return el('details', {className: 'group'},
    el('summary', {}, el('span', {textContent: 'Manage'}), el('span', {textContent: `${i.occurrences} entries`})),
    el('div', {className: 'muted', textContent: 'Name and rhythm'}), name, cadence, save,
    el('div', {className: 'muted', textContent: 'Entries containing these words join automatically:'}),
    ...(i.patterns.length ? i.patterns.map(p => el('div', {className: 'row'}, el('span', {textContent: `"${p}"`}),
      button('✕', async () => { await post('/api/recurring/manual/pattern', {group_id: i.group_id, pattern: p, remove: true}); reload(); }, 'ghost')))
      : [note('None yet. Add the ways the payee appears in your statement.')]),
    pattern, addPattern,
    el('div', {className: 'muted', textContent: 'Entries in it:'}),
    ...i.entries.slice().reverse().slice(0, 12).map(e => el('div', {className: 'row'},
      el('span', {textContent: `${e.date} · ${e.description.slice(0, 24)} · ${eur2(Math.abs(e.amount))}`}),
      button('✕', async () => { await post('/api/recurring/manual/unlink', {group_id: i.group_id, tx_id: e.id}); reload(); }, 'ghost'))),
    i.entries.length > 12 ? note(`and ${i.entries.length - 12} earlier ones`) : null,
    button('Remove this recurring item', async () => {
      if (confirm(`Remove "${i.label}" from the recurring list? Your entries stay untouched.`)) {
        await post('/api/recurring/manual/delete', {group_id: i.group_id});
        reload();
      }
    }, 'ghost'));
}

const item = (i, reload) => {
  if (i.kind === 'manual') {
    return el('div', {className: 'review'},
      row(`↻ ${i.label}`, i.occurrences ? `~${eur2(i.monthly_equivalent)}/mo` : 'no entries yet', i.direction === 'in' ? 'pos' : ''),
      note(['set by you', i.cadence, i.variable ? `amount varies ${eur2(i.amount_min)} to ${eur2(i.amount_max)}` : '',
        i.next_date ? `next ~${i.next_date}` : ''].filter(Boolean).join(' · ')),
      manageCard(i, reload), findCard(i, reload));
  }
  const detail = [i.category];
  if (i.kind === 'presence') detail.push(`appears most months, no fixed day · ${i.months_seen}`);
  if (i.variable) detail.push(`amount varies ${eur2(i.amount_min)} to ${eur2(i.amount_max)}`);
  detail.push(`×${i.occurrences} · next ~${i.next_date}${i.active ? '' : ' · looks ended'}`);
  return el('div', {className: 'review'},
    row(i.label.slice(0, 34), i.kind === 'presence' ? `~${eur2(i.monthly_equivalent)}/mo`
      : `${eur2(i.typical_amount)} ${i.cadence}`, i.direction === 'in' ? 'pos' : ''),
    note(detail.join(' · ')),
    findCard(i, reload),
    button('Not recurring', async () => { await post('/api/recurring/ignore', {key: i.key, direction: i.direction}); reload(); }, 'ghost'));
};

function shareByCategory(costs) {
  const m = {};
  for (const i of costs) m[i.category || 'Uncategorised'] = (m[i.category || 'Uncategorised'] || 0) + (i.monthly_equivalent || 0);
  return Object.entries(m).map(([label, value]) => ({label, value})).filter(x => x.value > 0).sort((a, b) => b.value - a.value);
}
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
  const addByName = (() => {
    const name = el('input', {placeholder: 'Name, e.g. Home cleaning', className: 'wide'});
    const pattern = el('input', {placeholder: 'Entries containing, e.g. limpiezas jaca', className: 'wide'});
    const cadence = el('select', {className: 'picker'}, ...CADENCES.map(c => el('option', {value: c, textContent: c})));
    const direction = el('select', {className: 'picker'}, el('option', {value: 'out', textContent: 'A cost (money out)'}),
      el('option', {value: 'in', textContent: 'An income (money in)'}));
    return el('details', {className: 'card'}, el('summary', {textContent: 'Add a recurring item by its name'}),
      note('For something the automatic detection missed. Entries whose text contains those words are included now and in future syncs. You can also pick entries by hand in Spending → Browse entries.'),
      name, pattern, cadence, direction,
      button('Add', async () => {
        try {
          await post('/api/recurring/manual', {name: name.value, pattern: pattern.value, cadence: cadence.value, direction: direction.value});
          reload();
        } catch (e) { alert(e.message); }
      }));
  })();
  root.replaceChildren(
    card('Recurring costs', el('div', {className: 'big', textContent: `${eur(d.monthly_costs)}/mo`}),
      note(`${eur(d.yearly_costs)} per year · recurring income ${eur(d.monthly_income)}/mo`),
      note('Found from repeated payments: a steady rhythm, or a payee that shows up almost every month even with a different date and amount. Variable ones count at their real monthly average.')),
    card('Share per category', donut(shareByCategory(costs), {format: v => `${eur(v)}/mo`}),
      note('Monthly equivalent of the recurring costs above, grouped by category.')),
    ...(d.upcoming.length ? [card('Coming up', ...d.upcoming.map(i =>
      row(`${i.next_date} · ${i.label.slice(0, 26)}`, eur2(i.typical_amount))))] : []),
    card('All recurring costs', ...(costs.length ? costs.map(i => item(i, reload)) : [note('None detected yet.')])),
    ...(incoming.length ? [card('Recurring income', ...incoming.map(i => item(i, reload)))] : []),
    ...(ended.length ? [card('Looks ended', ...ended.map(i => item(i, reload)))] : []),
    addByName,
    ...(hidden ? [hidden] : []),
  );
}
