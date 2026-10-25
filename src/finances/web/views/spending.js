import {card, row, el, eur, eur2, pct, get, post, note, button, barRow, categoryInput, categoryPicker} from '../ui.js';

const QUICK = ['Internal transfer', 'Groceries', 'Restaurants & bars', 'Transport', 'Shopping',
  'Subscriptions', 'Utilities & telecom', 'Health', 'Home & furniture', 'Leisure', 'Other'];
let month = null;
let openCategory = null;

export async function render(root) {
  const [s, review, cats, rules, generic, pairs] = await Promise.all([
    get('/api/spending' + (month ? `?month=${month}` : '')), get('/api/review'),
    get('/api/categories'), get('/api/rules'), get('/api/review/generic'), get('/api/pairs')]);
  month = s.month;
  const reload = () => render(root);

  const picker = el('select', {}, ...s.months.map(m => el('option', {value: m, textContent: m, selected: m === month})));
  picker.addEventListener('change', () => { month = picker.value; openCategory = null; reload(); });
  const delta = s.previous_month ? s.total - s.previous_total : null;

  const parts = [card('Spending',
    el('div', {}, picker),
    el('div', {className: 'big', textContent: eur(s.total)}),
    delta === null ? null : note(`${delta >= 0 ? '+' : ''}${eur(delta)} vs ${s.previous_month}`))];

  if (review.length) parts.push(reviewCard(review, cats, reload, s.unclear));
  if (generic.total) parts.push(genericCard(generic, cats, reload));

  const max = s.categories.length ? s.categories[0].total : 0;
  parts.push(card('By category', ...s.categories.map(c =>
    barRow(c.category, c.total, max, `${eur(c.total)} · ${pct(c.share)}`,
      () => { openCategory = openCategory === c.category ? null : c.category; reload(); })),
    note('Tap a category to see its shops and move them to another category.')));

  if (openCategory) parts.push(await entriesCard(openCategory, month, cats, reload));
  if (pairs.count) parts.push(pairsCard(pairs, reload));
  parts.push(rulesCard(rules, reload));
  parts.push(categoriesCard(cats, reload));
  root.replaceChildren(...parts);
}

function reviewCard(review, cats, reload, unclear) {
  const items = review.slice(0, 12).map(g => {
    const pattern = el('input', {value: g.pattern, className: 'wide'});
    const choose = async category => {
      await post('/api/categorize', {pattern: pattern.value, category});
      reload();
    };
    const chips = el('div', {className: 'chips'}, ...QUICK.map(c => button(c, () => choose(c), 'chip')));
    return el('div', {className: 'review'},
      row(g.sample.slice(0, 40), `${eur2(g.total)} · ×${g.count}`),
      el('div', {className: 'muted', textContent: 'Entries containing:'}), pattern,
      el('div', {className: 'muted', textContent: 'are:'}), chips,
      categoryPicker(cats, choose, {placeholder: 'Or pick any category…'}));
  });
  return card(`To review (${unclear} entries)`,
    note('I could not classify these. Your answer becomes a rule applied to every matching entry, now and on future syncs.'),
    ...items, review.length > 12 ? note(`${review.length - 12} more after these.`) : null);
}

function genericCard(generic, cats, reload) {
  const items = generic.entries.map(e => {
    const choose = async category => { await post('/api/categorize', {tx_id: e.id, category}); reload(); };
    const chips = el('div', {className: 'chips'}, ...QUICK.map(c => button(c, () => choose(c), 'chip')));
    return el('div', {className: 'review'},
      row(`${e.date} · ${e.description.slice(0, 28)}`, eur2(e.amount), 'neg'), chips,
      categoryPicker(cats, choose, {placeholder: 'Or pick any category…'}));
  });
  return card(`No shop name (${generic.total} entries)`,
    note('The bank gives no merchant for these (e.g. contactless), so a rule would label them all the same. They are asked one by one, biggest first.'),
    ...items, generic.total > items.length ? note(`${generic.total - items.length} more after these.`) : null);
}

// Entries of one category and month, grouped by shop. Move a whole shop (a rule for the
// future too) or a single entry.
async function entriesCard(category, month, cats, reload) {
  const txs = await get(`/api/transactions?category=${encodeURIComponent(category)}&month=${month}`);
  const groups = new Map();
  for (const t of txs) {
    const g = groups.get(t.key) ?? {key: t.key, pattern: t.pattern, label: t.description, total: 0, items: []};
    g.total += t.amount;
    g.items.push(t);
    groups.set(t.key, g);
  }
  const others = cats.filter(c => c !== category);
  const list = [...groups.values()].sort((a, b) => a.total - b.total).map(g => {
    const pattern = el('input', {value: g.pattern, className: 'wide'});
    const moveAll = categoryPicker(others, async c => {
      await post('/api/categorize', {pattern: pattern.value, category: c});
      reload();
    }, {placeholder: `Move all "${g.pattern}" to…`});
    const entries = g.items.map(t => el('div', {className: 'review'},
      row(t.description.slice(0, 40), eur2(t.amount), t.amount < 0 ? 'neg' : 'pos'),
      el('div', {className: 'muted', textContent: `${t.date} · move this entry only:`}),
      categoryPicker(others, async c => { await post('/api/categorize', {tx_id: t.id, category: c}); reload(); },
        {placeholder: 'Move to…'})));
    return el('details', {className: 'group'},
      el('summary', {}, el('span', {textContent: `${g.label.slice(0, 30)} ×${g.items.length}`}),
        el('span', {textContent: eur2(g.total)})),
      el('div', {className: 'muted', textContent: 'Rule for entries containing:'}), pattern, moveAll, ...entries);
  });
  return card(`${category} · ${month}`, ...list);
}

function pairsCard(pairs, reload) {
  const items = pairs.pairs.map(p => el('div', {className: 'pair'},
    row(`${p.spend.date} · ${p.spend.description.slice(0, 30)}`, eur2(p.spend.amount), 'neg'),
    row(`${p.reimbursement.date} · ${p.reimbursement.description.slice(0, 30)}`, eur2(p.reimbursement.amount), 'pos'),
    button('Not a pair', async () => { await post('/api/pairs/block', {tx_id: p.spend.id}); reload(); }, 'ghost')));
  return el('details', {className: 'card'},
    el('summary', {textContent: `Reimbursements left out of the totals (${pairs.count} pairs, ${eur(pairs.spent)})`}),
    note('A payment and a receipt of the same amount (within 1%, up to 60 days apart) cancel out. If one is wrong, tap "Not a pair".'),
    ...items);
}

function rulesCard(rules, reload) {
  if (!rules.length) return card('Your rules', note('None yet. They appear as you answer the review questions.'));
  return card('Your rules', ...rules.map(r => el('div', {className: 'row'},
    el('span', {textContent: `"${r.pattern}" → ${r.category}`}),
    button('✕', async () => { await post('/api/rules/delete', {pattern: r.pattern}); reload(); }, 'ghost'))));
}

function categoriesCard(cats, reload) {
  const from = el('select', {className: 'picker'}, ...cats.map(c => el('option', {value: c, textContent: c})));
  const to = categoryInput(cats);
  const go = button('Merge / rename', async () => {
    const dst = to.input.value.trim();
    if (!dst || dst === from.value) return;
    if (!confirm(`Move everything in "${from.value}" to "${dst}", now and in future syncs?`)) return;
    await post('/api/categories/merge', {src: from.value, dst});
    reload();
  });
  return card('Categories',
    note('Merge a category into another, or rename it by typing a new name. It applies to all entries and to future syncs.'),
    el('div', {className: 'muted', textContent: 'Move everything in:'}), from,
    el('div', {className: 'muted', textContent: 'into (existing or new name):'}), to.wrap, go);
}
