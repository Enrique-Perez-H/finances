import {card, row, el, eur, eur2, get, post, note, button, categoryPicker} from '../ui.js';

const FILTERS = [['all', 'All'], ['out', 'Spending'], ['in', 'Income'], ['review', 'To review'], ['shared', 'Shared'], ['recurring', 'Recurring']];
const CADENCES = 'monthly, every two months, quarterly, semiannual or yearly';
const PAGE = 40;
let month = '';
let filter = 'all';
let category = '*';
let query = '';
let shown = PAGE;

const params = m => {
  const q = new URLSearchParams({since: `${m}-01`, until: `${m}-31`, sort: 'date', limit: String(shown)});
  if (filter === 'out' || filter === 'in') q.set('direction', filter);
  if (['review', 'shared', 'recurring'].includes(filter)) q.set('flag', filter);
  if (category !== '*') q.set('category', category);
  if (query.trim()) q.set('q', query.trim());
  return q;
};

export async function render(root) {
  const overview = await get('/api/month?month=' + month);
  month = overview.month;
  const [list, cats, rec] = await Promise.all([get('/api/entries?' + params(month)), get('/api/categories'), get('/api/recurring')]);
  const groups = rec.items.filter(i => i.kind === 'manual');
  const reload = () => render(root);
  const act = async (fn) => { try { await fn(); await reload(); } catch (e) { alert(e.message); } };

  // --- month navigation and totals ---
  const i = overview.months.indexOf(month);
  const go = m => { month = m; shown = PAGE; reload(); };
  const picker = el('select', {}, ...overview.months.map(m => el('option', {value: m, textContent: m, selected: m === month})));
  picker.addEventListener('change', () => go(picker.value));
  const prev = button('◀', () => go(overview.months[i + 1]), 'ghost');
  const next = button('▶', () => go(overview.months[i - 1]), 'ghost');
  prev.disabled = i < 0 || i + 1 >= overview.months.length;  // months are newest first
  next.disabled = i <= 0;
  const chip = (label, key, n) => button(n === undefined ? label : `${label} (${n})`,
    () => { filter = key; category = '*'; shown = PAGE; reload(); }, filter === key ? 'chip on' : 'chip');
  const head = card('Month', el('div', {className: 'row'}, prev, picker, next),
    row('Income', eur(overview.income), 'pos'), row('Spending', eur(overview.spending), 'neg'),
    row('Saved', `${overview.saved >= 0 ? '+' : ''}${eur(overview.saved)}`, overview.saved >= 0 ? 'pos' : 'neg'),
    overview.removed_by_sharing > 0 ? note(`Splitting shared costs took ${eur(overview.removed_by_sharing)} off this month's spending.`) : null,
    overview.partial ? note('Month in progress: not final yet.') : null);

  // --- filters ---
  const search = el('input', {type: 'search', value: query, placeholder: 'Search the text…', className: 'wide'});
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { query = search.value; shown = PAGE; reload(); }, 400); });
  const catSelect = el('select', {className: 'picker'}, el('option', {value: '*', textContent: 'All categories'}),
    el('option', {value: 'uncategorized', textContent: 'Uncategorized', selected: category === 'uncategorized'}),
    ...cats.map(c => el('option', {value: c, textContent: c, selected: c === category})));
  catSelect.value = category;
  catSelect.addEventListener('change', () => { category = catSelect.value; shown = PAGE; reload(); });
  const filters = card('Show', el('div', {className: 'chips'},
    chip('All', 'all'), chip('Spending', 'out'), chip('Income', 'in'),
    chip('To review', 'review', overview.to_review), chip('Shared', 'shared', overview.shared),
    chip('Recurring', 'recurring', overview.recurring)), search, catSelect);

  // --- the entries ---
  const entryRow = t => {
    const outflow = t.raw_amount < 0;
    const virtual = t.account_id === 'tricount-shared';
    const sharedBox = el('input', {type: 'checkbox', checked: t.share < 1, disabled: !outflow || virtual});
    sharedBox.addEventListener('change', () => act(() => post('/api/share', {tx_ids: [t.id], share: sharedBox.checked ? 0.5 : 1})));
    const label = el('label', {className: 'muted'}, sharedBox, virtual ? ' From Tricount' : outflow ? ' Shared with my partner (½)' : ' Income');

    const current = t.recurring ? `↻ ${t.recurring.name}${t.recurring.explicit ? '' : ' (by name)'}` : 'Not recurring';
    const recurring = el('select', {className: 'picker'},
      el('option', {value: '', textContent: current, selected: true, disabled: true}),
      ...groups.filter(g => !t.recurring || g.group_id !== t.recurring.group_id)
        .map(g => el('option', {value: String(g.group_id), textContent: `↻ ${g.label}`})),
      el('option', {value: '__new__', textContent: '＋ New recurring item…'}),
      ...(t.recurring ? [el('option', {value: '__off__', textContent: 'Not recurring (remove)'})] : []));
    recurring.addEventListener('change', () => {
      const v = recurring.value;
      recurring.selectedIndex = 0;
      if (v === '__off__') {
        if (!t.recurring.explicit) { alert(`This entry joins “${t.recurring.name}” through a name you gave it. Change the words in the Recurring tab.`); return; }
        act(() => post('/api/recurring/manual/unlink', {group_id: t.recurring.group_id, tx_id: t.id}));
      } else if (v === '__new__') {
        const name = (prompt('Name of this recurring item (e.g. Home cleaning)', t.description.slice(0, 24)) || '').trim();
        if (!name) return;
        const cadence = (prompt(`How often does it come? ${CADENCES}`, 'monthly') || 'monthly').trim().toLowerCase();
        act(() => post('/api/recurring/manual', {tx_ids: [t.id], name, cadence}));
      } else if (v) {
        act(() => post('/api/recurring/manual', {tx_ids: [t.id], group_id: Number(v)}));
      }
    });

    return el('div', {className: 'entry'},
      el('div', {className: 'row'}, el('span', {className: 'muted', textContent: t.date}),
        el('span', {className: t.amount < 0 ? 'neg' : 'pos', textContent: eur2(t.amount)})),
      el('div', {className: 'desc', textContent: t.description}),
      t.share < 1 ? el('div', {className: 'muted', textContent: `counts ${Math.round(t.share * 100)}% of ${eur2(Math.abs(t.raw_amount))}`}) : null,
      label,
      categoryPicker(cats.filter(c => c !== t.category), c => act(() => post('/api/categorize', {tx_id: t.id, category: c})), {current: t.category}),
      recurring);
  };
  const entries = card(`Entries (${list.count})`,
    ...(list.items.length ? list.items.map(entryRow) : [note('Nothing matches these filters.')]),
    list.has_more ? button('Show more', () => { shown += PAGE; reload(); }, 'ghost') : null);

  root.replaceChildren(head, filters, entries);
}
