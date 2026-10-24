import {card, row, el, eur, eur2, pct, get, post, note, button, barRow, categoryInput} from '../ui.js';

const QUICK = ['Internal transfer', 'Groceries', 'Restaurants & bars', 'Transport', 'Shopping',
  'Subscriptions', 'Utilities', 'Health', 'Transfers sent', 'Other'];
let month = null;
let openCategory = null;

export async function render(root) {
  const [s, review, cats, rules] = await Promise.all([
    get('/api/spending' + (month ? `?month=${month}` : '')), get('/api/review'),
    get('/api/categories'), get('/api/rules')]);
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

  const max = s.categories.length ? s.categories[0].total : 0;
  parts.push(card('By category', ...s.categories.map(c => {
    const body = [barRow(c.category, c.total, max, `${eur(c.total)} · ${pct(c.share)}`,
      () => { openCategory = openCategory === c.category ? null : c.category; reload(); })];
    return el('div', {}, ...body);
  }), note('Tap a category to see and fix its entries.')));

  if (openCategory) parts.push(await entriesCard(openCategory, month, cats, reload));
  parts.push(rulesCard(rules, reload));
  root.replaceChildren(...parts);
}

function reviewCard(review, cats, reload, unclear) {
  const items = review.slice(0, 8).map(g => {
    const pattern = el('input', {value: g.pattern, className: 'wide'});
    const cat = categoryInput(cats);
    const choose = async category => {
      if (!category.trim()) return;
      await post('/api/categorize', {pattern: pattern.value, category});
      reload();
    };
    const chips = el('div', {className: 'chips'}, ...QUICK.map(c => button(c, () => choose(c), 'chip')));
    return el('div', {className: 'review'},
      row(g.sample.slice(0, 40), `${eur2(g.total)} · ×${g.count}`),
      el('div', {className: 'muted', textContent: 'Entries containing:'}), pattern,
      el('div', {className: 'muted', textContent: 'are (tap one, or type another):'}), chips,
      cat.wrap, button('Apply typed category', () => choose(cat.input.value)));
  });
  return card(`To review (${unclear} entries)`,
    note('I could not classify these. Your answer becomes a rule applied to every matching entry, now and on future syncs.'),
    ...items, review.length > 8 ? note(`${review.length - 8} more after these.`) : null);
}

async function entriesCard(category, month, cats, reload) {
  const txs = await get(`/api/transactions?category=${encodeURIComponent(category)}&month=${month}`);
  return card(`${category} · ${month}`, ...txs.map(t => {
    const sel = el('select', {}, ...cats.map(c => el('option', {value: c, textContent: c, selected: c === t.category})));
    sel.addEventListener('change', async () => {
      await post('/api/categorize', {tx_id: t.id, category: sel.value});
      reload();
    });
    return el('div', {className: 'review'}, row(t.description.slice(0, 40), eur2(t.amount), t.amount < 0 ? 'neg' : 'pos'),
      el('div', {className: 'muted', textContent: `${t.date} · this entry only:`}), sel);
  }));
}

function rulesCard(rules, reload) {
  if (!rules.length) return card('Your rules', note('None yet. They appear as you answer the review questions.'));
  return card('Your rules', ...rules.map(r => el('div', {className: 'row'},
    el('span', {textContent: `"${r.pattern}" → ${r.category}`}),
    button('✕', async () => { await post('/api/rules/delete', {pattern: r.pattern}); reload(); }, 'ghost'))));
}
