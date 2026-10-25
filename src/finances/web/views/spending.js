import {card, row, el, eur, eur2, pct, get, post, note, button, barRow, categoryPicker} from '../ui.js';
import {browserCard, browse} from './browser.js';

const QUICK = ['Internal transfer', 'Groceries', 'Restaurants & bars', 'Transport', 'Shopping',
  'Subscriptions', 'Utilities & telecom', 'Health', 'Home & furniture', 'Leisure', 'Other'];
let month = null;
let scrollToBrowser = false;
let avgMonths = (() => { try { return Number(localStorage.getItem('avgMonths') ?? 12); } catch (e) { return 12; } })();

export async function render(root) {
  const [s, review, cats, generic, pairs, avg] = await Promise.all([
    get('/api/spending' + (month ? `?month=${month}` : '')), get('/api/review'),
    get('/api/categories'), get('/api/review/generic'), get('/api/pairs'),
    get(`/api/spending/average?months=${avgMonths}`)]);
  month = s.month;
  const reload = () => render(root);

  const picker = el('select', {}, ...s.months.map(m => el('option', {value: m, textContent: m, selected: m === month})));
  picker.addEventListener('change', () => { month = picker.value; reload(); });
  const delta = s.previous_month ? s.total - s.previous_total : null;

  const parts = [card('Spending',
    el('div', {}, picker),
    el('div', {className: 'big', textContent: eur(s.total)}),
    delta === null ? null : note(`${delta >= 0 ? '+' : ''}${eur(delta)} vs ${s.previous_month}`))];

  parts.push(averageCard(avg, reload));
  if (review.length) parts.push(reviewCard(review, cats, reload, s.unclear));
  if (generic.total) parts.push(genericCard(generic, cats, reload));

  const max = s.categories.length ? s.categories[0].total : 0;
  parts.push(card('By category', ...s.categories.map(c =>
    barRow(c.category, c.total, max, `${eur(c.total)} · ${pct(c.share)}`, () => {
      browse.category = c.category;
      browse.period = 'month';
      browse.q = '';
      scrollToBrowser = true;
      reload();
    })),
    note('Tap a category to browse its entries below.')));

  const browser = browserCard(cats, s.months, month);
  parts.push(browser);
  if (pairs.count) parts.push(pairsCard(pairs, reload));
  root.replaceChildren(...parts);
  if (scrollToBrowser) {
    scrollToBrowser = false;
    browser.scrollIntoView({behavior: 'smooth', block: 'start'});
  }
}

function reviewCard(review, cats, reload, unclear) {
  const items = review.slice(0, 12).map(g => {
    const pattern = el('input', {value: g.pattern, className: 'wide'});
    const choose = async category => {
      try {
        await post('/api/categorize', {pattern: pattern.value, category, direction: g.direction});
        reload();
      } catch (e) { alert(e.message); }
    };
    const chips = el('div', {className: 'chips'}, ...QUICK.map(c => button(c, () => choose(c), 'chip')));
    return el('div', {className: 'review'},
      row(g.sample.slice(0, 40), `${eur2(g.total)} · ×${g.count}`, g.total < 0 ? 'neg' : 'pos'),
      el('div', {className: 'muted', textContent: g.direction === 'in' ? 'Money in, entries containing:' : 'Money out, entries containing:'}), pattern,
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

// A typical month: average per category over the last completed months.
function averageCard(avg, reload) {
  const choices = [[3, '3m'], [6, '6m'], [12, '12m'], [0, 'All']];
  const buttons = el('div', {className: 'ranges'}, ...choices.map(([n, label]) => {
    const b = el('button', {textContent: label, className: n === avgMonths ? 'on' : ''});
    b.addEventListener('click', () => {
      avgMonths = n;
      try { localStorage.setItem('avgMonths', String(n)); } catch (e) { /* private mode */ }
      reload();
    });
    return b;
  }));
  if (!avg.months_used) {
    return card('Average month', buttons, note('Needs at least one completed month of transactions.'));
  }
  const max = avg.categories.length ? avg.categories[0].average : 0;
  return card('Average month', buttons,
    el('div', {className: 'big', textContent: eur(avg.total)}),
    note(`spent per month on average · ${avg.months_used} completed month${avg.months_used === 1 ? '' : 's'} (${avg.first} to ${avg.last})`),
    ...avg.categories.map(c => barRow(c.category, c.average, max, `${eur(c.average)} · ${pct(c.share)}`)),
    note('A month without spending in a category counts as zero. The month in progress, transfers between your accounts, one-off capital items and reimbursed payments are not counted.'));
}
