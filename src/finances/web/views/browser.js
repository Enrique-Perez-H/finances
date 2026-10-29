import {card, row, el, eur2, get, post, note, button, categoryPicker} from '../ui.js';

// "Mark as recurring": existing recurring items you made, or a new one.
function recurringPicker(groups, onChoose) {
  const sel = el('select', {className: 'picker'},
    el('option', {value: '', textContent: 'Mark as recurring…', selected: true, disabled: true}),
    ...groups.map(g => el('option', {value: String(g.group_id), textContent: `↻ ${g.label}`})),
    el('option', {value: '__new__', textContent: '＋ New recurring item…'}));
  sel.addEventListener('change', () => {
    const value = sel.value;
    sel.selectedIndex = 0;
    if (value === '__new__') {
      const name = (prompt('Name of this recurring item (e.g. Home cleaning)') || '').trim();
      if (name) onChoose({name});
    } else if (value) onChoose({group_id: Number(value)});
  });
  return sel;
}

const PAGE = 50;
export const browse = {category: '*', period: 'month', q: '', sort: 'date', view: 'list', selected: new Set()};

function dateRange(period, month) {
  if (period === 'month') return {since: `${month}-01`, until: `${month}-31`};
  if (period === 'all') return {};
  const back = {'3m': 3, '12m': 12}[period];
  const d = new Date();
  d.setMonth(d.getMonth() - back);
  return {since: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`};
}

// A browser for every entry: pick a category and a period, search, and move entries one by
// one, several at once, or a whole shop at a time.
export function browserCard(cats, months, month) {
  const root = card('Browse entries');
  root.id = 'browser';
  let items = [], meta = {count: 0, total_in: 0, total_out: 0, has_more: false};
  let manual = [];
  const flash = el('div', {className: 'muted'});
  const list = el('div', {});
  const summary = el('div', {className: 'muted'});
  const bulk = el('div', {className: 'bulk'});

  const select = (key, options, value, onChange) => {
    const s = el('select', {}, ...options.map(([v, label]) => el('option', {value: v, textContent: label, selected: v === value})));
    s.addEventListener('change', () => onChange(s.value));
    return s;
  };
  const params = offset => {
    const q = new URLSearchParams({category: browse.category, sort: browse.sort, offset, limit: PAGE,
      ...dateRange(browse.period, month)});
    if (browse.q.trim()) q.set('q', browse.q.trim());
    return q;
  };
  const load = async (more = false) => {
    const res = await get('/api/entries?' + params(more ? items.length : 0));
    items = more ? items.concat(res.items) : res.items;
    meta = res;
    try { manual = (await get('/api/recurring')).items.filter(i => i.kind === 'manual'); } catch (e) { manual = []; }
    if (!more) browse.selected.clear();
    draw();
  };
  const removeLocally = ids => {
    const gone = new Set(ids);
    if (browse.category === '*') return false;  // still in the list, only its label changes
    for (const t of items.filter(t => gone.has(t.id))) {  // keep the summary line honest
      if (t.amount < 0) meta.total_out = Math.max(0, meta.total_out + t.amount);
      else meta.total_in = Math.max(0, meta.total_in - t.amount);
    }
    items = items.filter(t => !gone.has(t.id));
    meta.count -= ids.length;
    ids.forEach(i => browse.selected.delete(i));
    return true;
  };
  const moveOne = async (t, category) => {
    try {
      await post('/api/categorize', {tx_id: t.id, category});
      if (!removeLocally([t.id])) t.category = category;
      draw();
    } catch (e) { alert(e.message); }
  };
  const moveSelected = async category => {
    const ids = [...browse.selected];
    try {
      await post('/api/categorize/bulk', {tx_ids: ids, category});
      if (!removeLocally(ids)) items.forEach(t => ids.includes(t.id) && (t.category = category));
      browse.selected.clear();
      draw();
    } catch (e) { alert(e.message); }
  };

  const entryRow = t => {
    const box = el('input', {type: 'checkbox', checked: browse.selected.has(t.id)});
    box.addEventListener('change', () => {
      box.checked ? browse.selected.add(t.id) : browse.selected.delete(t.id);
      drawBulk();
    });
    return el('div', {className: 'entry'},
      el('div', {className: 'row'}, el('label', {className: 'muted'}, box, ` ${t.date}`),
        el('span', {textContent: eur2(t.amount), className: t.amount < 0 ? 'neg' : 'pos'})),
      el('div', {className: 'desc', textContent: t.description}),
      categoryPicker(cats.filter(c => c !== t.category), c => moveOne(t, c), {current: t.category}));
  };

  const markRecurring = async (ids, extra) => {
    try {
      const res = await post('/api/recurring/manual', {tx_ids: ids, ...extra});
      await load();
      flash.textContent = `Marked ${ids.length} entries as recurring. Find it in the Recurring tab.`;
      return res;
    } catch (e) { alert(e.message); }
  };

  const drawBulk = () => {
    const n = browse.selected.size;
    bulk.replaceChildren(...(n ? [
      el('strong', {textContent: `${n} selected`}),
      categoryPicker(cats, moveSelected, {placeholder: `Move ${n} to…`}),
      recurringPicker(manual, extra => markRecurring([...browse.selected], extra)),
      button('Clear', () => { browse.selected.clear(); draw(); }, 'ghost')] : []));
    bulk.style.display = n ? 'block' : 'none';
  };

  const groupedView = () => {
    const groups = new Map();
    for (const t of items) {
      const g = groups.get(t.key) ?? {key: t.key, pattern: t.pattern, label: t.description, total: 0, items: []};
      g.total += t.amount;
      g.items.push(t);
      groups.set(t.key, g);
    }
    return [...groups.values()].sort((a, b) => Math.abs(b.total) - Math.abs(a.total)).map(g => {
      const pattern = el('input', {value: g.pattern, className: 'wide'});
      const direction = g.items.every(t => t.amount < 0) ? 'out' : g.items.every(t => t.amount > 0) ? 'in' : 'any';
      const moveAll = categoryPicker(cats, async c => {
        try {
          await post('/api/categorize', {pattern: pattern.value, category: c, direction});
          await load();
        } catch (e) { alert(e.message); }
      }, {placeholder: `Move all "${g.pattern}" to…`});
      return el('details', {className: 'group'},
        el('summary', {}, el('span', {textContent: `${g.label.slice(0, 30)} ×${g.items.length}`}),
          el('span', {textContent: eur2(g.total)})),
        el('div', {className: 'muted', textContent: 'Every entry containing (now and in future syncs):'}), pattern, moveAll,
        button('Treat as a recurring item…', async () => {
          const name = (prompt('Name of this recurring item (e.g. Home cleaning)', g.pattern) || '').trim();
          if (!name) return;
          const future = confirm(`Also include future entries containing "${pattern.value}" automatically?`);
          await markRecurring(g.items.map(t => t.id), {name, ...(future ? {pattern: pattern.value} : {})});
        }, 'ghost'),
        ...g.items.map(entryRow));
    });
  };

  const draw = () => {
    summary.textContent = `${meta.count} entries · out ${eur2(meta.total_out)} · in ${eur2(meta.total_in)}`;
    list.replaceChildren(
      ...(items.length ? (browse.view === 'shops' ? groupedView() : items.map(entryRow))
                       : [note('Nothing matches these filters.')]),
      ...(meta.has_more ? [button('Show more', () => load(true), 'ghost')] : []));
    drawBulk();
  };

  const categories = [['*', 'All categories'], ['uncategorized', 'Uncategorized (to review)'], ...cats.map(c => [c, c])];
  const search = el('input', {type: 'search', value: browse.q, placeholder: 'Search text…', className: 'wide'});
  let timer;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { browse.q = search.value; load(); }, 300);
  });
  const periods = [['month', `Month ${month}`], ['3m', 'Last 3 months'], ['12m', 'Last 12 months'], ['all', 'All time']];
  root.append(
    el('div', {className: 'filters'},
      select('category', categories, browse.category, v => { browse.category = v; load(); }),
      select('period', periods, browse.period, v => { browse.period = v; load(); }),
      select('sort', [['date', 'Newest first'], ['amount', 'Largest first']], browse.sort, v => { browse.sort = v; load(); }),
      select('view', [['list', 'List'], ['shops', 'Grouped by shop']], browse.view, v => { browse.view = v; draw(); })),
    search, summary, flash, bulk, list);
  load();
  return root;
}
