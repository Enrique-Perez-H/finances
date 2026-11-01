import {card, row, el, eur2, get, post, note, button} from '../ui.js';

let connectedMembers = null;  // members shown while choosing "who am I" right after connecting

export async function render(root) {
  const d = await get('/api/tricount');
  const reload = () => render(root);
  const caution = card('How this works',
    note('Only your share of a shared expense counts as your spending, whoever paid it. An expense you paid is matched to its bank entry (same amount, nearby date) and that entry then counts at your share. An expense your partner paid appears as an entry for your share. Settling-up payments are ignored: a transfer between you is classified by your own rules.'),
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
    row('Matched to a bank entry', String(d.linked)), row('Paid by your partner (added)', String(d.virtual - d.unmatched_mine.length)),
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
    : e.mode === 'linked' ? 'matched to your bank entry'
    : e.payer === 'me' ? 'no bank entry found yet: counted at your share' : 'paid by your partner: added at your share';
  const unmatched = d.unmatched_mine.length ? card('Paid by you, not found in the bank yet',
    note('These count at your share for now and match automatically when the bank entry arrives. If you paid with an account that is not synced, they simply stay as they are.'),
    ...d.unmatched_mine.map(e => el('div', {className: 'review'},
      row(`${e.date} · ${e.description.slice(0, 26)}`, `${eur2(e.my_share)} of ${eur2(e.total)}`), toggle(e)))) : null;
  const list = card('Recent shared expenses', ...(d.expenses.length ? d.expenses.slice(0, 30).map(e => el('div', {className: 'review'},
    row(`${e.date} · ${e.description.slice(0, 26)}`, `${eur2(e.my_share)} of ${eur2(e.total)}`), note(status(e)), toggle(e)))
    : [note('Nothing yet. Tap Sync Tricount.')]));
  root.replaceChildren(head, ...(unmatched ? [unmatched] : []), list, caution);
}
