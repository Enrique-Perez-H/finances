import {card, el, post, note, button, categoryInput} from '../ui.js';

// Rules and category tools. Kept out of the way: rules work silently in the background.
export function toolsCard(cats, rules, reload) {
  const dir = {in: 'money in', out: 'money out', any: 'in or out'};
  const ruleRows = rules.length ? rules.map(r => el('div', {className: 'row'},
    el('span', {textContent: `"${r.pattern}" (${dir[r.direction] ?? r.direction}) → ${r.category}${r.ignored ? '  ⚠ ignored: too generic' : ''}`}),
    button('✕', async () => { await post('/api/rules/delete', {pattern: r.pattern, direction: r.direction}); reload(); }, 'ghost')))
    : [note('No rules yet. They are created when you answer the review questions or move a whole shop.')];

  const from = el('select', {className: 'picker'}, ...cats.map(c => el('option', {value: c, textContent: c})));
  const to = categoryInput(cats);
  const merge = button('Merge / rename', async () => {
    const dst = to.input.value.trim();
    if (!dst || dst === from.value) return;
    if (!confirm(`Move everything in "${from.value}" to "${dst}", now and in future syncs?`)) return;
    await post('/api/categories/merge', {src: from.value, dst});
    reload();
  });

  return el('details', {className: 'card'},
    el('summary', {textContent: 'Advanced: rules and category tools'}),
    el('h2', {textContent: 'Your rules'}),
    note('A rule applies to every entry whose text contains its words. Delete any that match too much.'),
    ...ruleRows,
    el('h2', {textContent: 'Merge or rename a category'}),
    el('div', {className: 'muted', textContent: 'Move everything in:'}), from,
    el('div', {className: 'muted', textContent: 'into (existing or new name):'}), to.wrap, merge);
}
