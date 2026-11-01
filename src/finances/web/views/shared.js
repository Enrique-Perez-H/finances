import {card, row, el, eur2, get, post, note, button} from '../ui.js';

let connectedMembers = null;
let unmatchedShown = 10;  // how many unmatched expenses to list at once  // members shown while choosing "who am I" right after connecting

export async function render(root) {
  const d = await get('/api/tricount');
  const reload = () => render(root);
  const caution = card('How this works',
    note('Only your share of a shared expense counts as your spending, whoever paid it. An expense you paid is matched to its bank entry (same amount, within a few weeks) and that entry then counts at your share. An expense your partner paid appears as an entry for your share. Settling-up payments are ignored: a transfer between you is classified by your own rules.'),
    note('Tricount has no public interface. This reads your group through the same private endpoints its app uses, so it can stop working when Tricount changes them. It never changes your group. Your share link is stored encrypted in this vault only: keep it private, because anyone with it can read and edit the group.'));

  if (!d.configured) {
    const link = el('input', {placeholder: 'Tricount share link', className: 'wide'});
    const out = el('div', {className: 'neg'});
    const connect = button('Connect', async () => {
      try { connectedMembers = (await post('/api/tricount/connect', {link: link.value})).members; reload(); }
      catch (e) { out.textContent = e.message; }
    });
    root.replaceChildren(card('Shared costs with Tricount',
      note('In Tricount, open the group, tap the share icon and copy the link.'), link, connect, out), caution);
    return;
  }

  if (!d.connected_as) {
    const members = d.members.length ? d.members : (connectedMembers ?? []);
    const pick = el('select', {className: 'picker'}, ...members.map(m => el('option', {value: m.uuid, textContent: m.name})));
    root.replaceChildren(card(`Connected to “${d.title}”`, note('Which member are you?'), pick,
      button('That is me', async () => { await post('/api/tricount/me', {uuid: pick.value}); reload(); })), caution);
    return;
  }

  const me = d.members.find(m => m.uuid === d.connected_as)?.name ?? 'you';
  const out = el('div', {className: 'muted'});
  const sync = button('Sync Tricount', async () => {
    out.textContent = 'Syncing…';
    try {
      const s = (await post('/api/tricount/sync', {})).tricount;
      out.textContent = s.state === 'ok' ? `Done. ${s.expenses} expenses.${s.note ? ' ' + s.note : ''}` : `Failed: ${s.error}`;
      if (s.state === 'ok') reload();
    } catch (e) { out.textContent = 'Failed: ' + e.message; }
  });
  const balance = d.balance === null || d.balance === undefined ? null
    : Math.abs(d.balance) < 0.005 ? 'All square.'
    : d.balance > 0 ? `Your partner owes you ${eur2(d.balance)}.` : `You owe your partner ${eur2(-d.balance)}.`;

  const head = card(`Shared with Tricount: ${d.title}`,
    balance ? el('div', {className: 'big', textContent: balance}) : null,
    note(`You are ${me}${d.synced_at ? ` · last synced ${d.synced_at.replace('T', ' ').slice(0, 16)}` : ' · not synced yet'}`),
    row('Your share of all shared expenses', eur2(d.my_total)),
    row('Matched to a bank entry', String(d.linked)),
    row('Paid by your partner (added)', String(d.expenses.filter(e => e.mode === 'virtual' && e.payer !== 'me' && !e.ignored).length)),
    d.confirmed_elsewhere ? row('Paid elsewhere (confirmed by you)', String(d.confirmed_elsewhere)) : null,
    d.ignored ? row('Left out by you', String(d.ignored)) : null,
    sync, ' ', button('Disconnect', async () => {
      if (confirm('Disconnect Tricount? Everything goes back to counting in full, and the link is forgotten.')) {
        await post('/api/tricount/disconnect', {}); connectedMembers = null; reload();
      }
    }, 'ghost'), out,
    note('The balance is what Tricount says you owe each other. It does not move money.'));

  const toggle = e => button(e.ignored ? 'Count it again' : 'Leave out', async () => {
    await post('/api/tricount/ignore', {uuid: e.uuid, ignored: !e.ignored}); reload();
  }, 'ghost');
  const status = e => e.ignored ? 'left out by you'
    : e.mode === 'linked' ? (e.pinned ? 'matched to your bank entry (confirmed by you)' : 'matched to your bank entry')
    : e.mode === 'virtual' && e.pinned && e.payer === 'me' ? 'paid elsewhere (confirmed by you): counted at your share'
    : e.payer === 'me' ? 'no bank entry found yet: counted at your share' : 'paid by your partner: added at your share';
  const act = async (path, body) => {
    try { await post(path, body); reload(); } catch (e) { alert(e.message); }
  };
  const candidateRow = (e, c) => el('div', {className: 'review'},
    row(`${c.date} · ${c.description.slice(0, 30)}`, eur2(c.amount), 'neg'),
    note(`${c.account} · ${c.reason}${c.category && c.category !== 'uncategorized' ? ' · ' + c.category : ''}`),
    button('This is it', () => act('/api/tricount/link', {uuid: e.uuid, tx_id: c.id})));
  const searchBox = e => {
    const q = el('input', {placeholder: 'Words in the bank text, e.g. clinic', className: 'wide'});
    const results = el('div', {});
    const go = button('Search', async () => {
      results.textContent = 'Searching…';
      try {
        const found = await get(`/api/tricount/search?uuid=${encodeURIComponent(e.uuid)}&q=${encodeURIComponent(q.value)}&days=90`);
        results.replaceChildren(...(found.length ? found.map(c => candidateRow(e, c))
          : [note('Nothing found. Try other words, or leave the box empty to list payments around that date.')]));
      } catch (err) { results.textContent = err.message; }
    });
    return el('details', {}, el('summary', {textContent: 'Search another payment'}),
      note('Looks at all your bank payments 90 days either side of the date, whatever the amount.'), q, go, results);
  };
  const shown = d.unmatched_mine.slice(0, unmatchedShown);
  const unmatched = d.unmatched_mine.length ? card(`Not matched to your bank yet (${d.unmatched_mine.length})`,
    note('You paid these, but no bank payment with the same amount was found from 5 days before to 3 weeks after the date. They count at your share meanwhile. Pick the bank payment they correspond to, search for it, or say there is none.'),
    ...shown.map(e => el('div', {className: 'review'},
      el('strong', {textContent: `${e.date} · ${e.description.slice(0, 28)}`}),
      row('You paid, your share', `${eur2(e.total)} → ${eur2(e.my_share)}`),
      ...(e.candidates && e.candidates.length ? [note('Best candidates in your bank:'), ...e.candidates.map(c => candidateRow(e, c))]
        : [note('No similar bank payment within 45 days.')]),
      searchBox(e),
      el('div', {className: 'chips'},
        button('Paid elsewhere: no bank entry', () => act('/api/tricount/confirm', {uuid: e.uuid}), 'chip'),
        button('Leave out', () => act('/api/tricount/ignore', {uuid: e.uuid, ignored: true}), 'chip')))),
    d.unmatched_mine.length > unmatchedShown
      ? button(`Show ${Math.min(10, d.unmatched_mine.length - unmatchedShown)} more`, () => { unmatchedShown += 10; reload(); }, 'ghost') : null)
    : null;
  const list = card('Recent shared expenses', ...(d.expenses.length ? d.expenses.slice(0, 30).map(e => el('div', {className: 'review'},
    row(`${e.date} · ${e.description.slice(0, 26)}`, `${eur2(e.my_share)} of ${eur2(e.total)}`), note(status(e)),
    e.mode === 'linked' ? button('Not this payment', () => act('/api/tricount/unlink', {uuid: e.uuid}), 'ghost') : null,
    toggle(e)))
    : [note('Nothing yet. Tap Sync Tricount.')]));
  root.replaceChildren(head, ...(unmatched ? [unmatched] : []), list, caution);
}
