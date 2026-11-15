import {card, row, el, eur2, get, post, note, button} from '../ui.js';

let connectedMembers = null;  // members shown while choosing "who am I" right after connecting
let reviewShown = 15;         // how many Tricount expenses to list at once
const selected = new Set();   // Tricount expenses ticked for a bulk choice
const split = {q: '', days: 60, shown: 10};  // the bank-entries list

const dayRange = n => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
};

export async function render(root) {
  const d = await get('/api/tricount');
  const reload = () => render(root);
  const act = async (path, body) => {
    try { await post(path, body); reload(); } catch (e) { alert(e.message); }
  };

  const explain = card('How this works',
    note('Your bank entries are the source of truth: they count in full unless you split them. Tricount expenses are only a list. Nothing from Tricount counts until you choose, one by one: add it as shared (your share counts), add it as only yours (all of it counts), or mark it as already in your bank (it is skipped, because the bank entry counts it).'),
    note('Tricount has no public interface. This reads your group through the same private endpoints its app uses, so it can stop working when Tricount changes them. It never changes your group. Your share link is stored encrypted in this vault only: keep it private, because anyone with it can read and edit the group.'));

  if (!d.configured) {
    const link = el('input', {placeholder: 'Tricount share link', className: 'wide'});
    const out = el('div', {className: 'neg'});
    const connect = button('Connect', async () => {
      try { connectedMembers = (await post('/api/tricount/connect', {link: link.value})).members; reload(); }
      catch (e) { out.textContent = e.message; }
    });
    root.replaceChildren(card('Shared costs with Tricount',
      note('In Tricount, open the group, tap the share icon and copy the link.'), link, connect, out),
      await splitCard(reload), explain);
    return;
  }

  if (!d.connected_as) {
    const members = d.members.length ? d.members : (connectedMembers ?? []);
    const pick = el('select', {className: 'picker'}, ...members.map(m => el('option', {value: m.uuid, textContent: m.name})));
    root.replaceChildren(card(`Connected to “${d.title}”`, note('Which member are you?'), pick,
      button('That is me', async () => { await post('/api/tricount/me', {uuid: pick.value}); reload(); })), explain);
    return;
  }

  const me = d.members.find(m => m.uuid === d.connected_as)?.name ?? 'you';
  const out = el('div', {className: 'muted'});
  const sync = button('Sync Tricount', async () => {
    out.textContent = 'Syncing…';
    try {
      const s = (await post('/api/tricount/sync', {})).tricount;
      out.textContent = s.state === 'ok' ? `Done. ${s.expenses} expenses.` : `Failed: ${s.error}`;
      if (s.state === 'ok') reload();
    } catch (e) { out.textContent = 'Failed: ' + e.message; }
  });
  const balance = d.balance === null || d.balance === undefined ? null
    : Math.abs(d.balance) < 0.005 ? 'All square.'
    : d.balance > 0 ? `Your partner owes you ${eur2(d.balance)}.` : `You owe your partner ${eur2(-d.balance)}.`;
  const head = card(`Tricount: ${d.title}`,
    balance ? el('div', {className: 'big', textContent: balance}) : null,
    note(`You are ${me}${d.synced_at ? ` · last synced ${d.synced_at.replace('T', ' ').slice(0, 16)}` : ' · not synced yet'}`),
    row('Added to your figures', `${eur2(d.counted_total)} (${d.shared + d.mine} expenses)`),
    row('Waiting for your choice', String(d.pending)),
    sync, ' ', button('Disconnect', async () => {
      if (confirm('Disconnect Tricount? What you added from it disappears and the link is forgotten. Bank entries you split stay split.')) {
        await post('/api/tricount/disconnect', {}); connectedMembers = null; reload();
      }
    }, 'ghost'), out,
    note('The balance is what Tricount says you owe each other. It does not move money.'));

  // --- expenses waiting for a choice ---
  const choose = (uuids, choice) => act('/api/tricount/choose', {uuids, choice});
  const pending = d.expenses.filter(e => e.choice === '');
  const paidBy = e => (e.payer === 'me' ? 'You paid' : 'Your partner paid');
  const shown = pending.slice(0, reviewShown);
  const bulk = el('div', {className: 'bulk'});
  const drawBulk = () => {
    const n = selected.size;
    bulk.replaceChildren(...(n ? [el('strong', {textContent: `${n} selected`}),
      el('div', {className: 'chips'},
        button('Shared', () => { const ids = [...selected]; selected.clear(); choose(ids, 'shared'); }, 'chip'),
        button('Only mine', () => { const ids = [...selected]; selected.clear(); choose(ids, 'mine'); }, 'chip'),
        button('Already in my bank', () => { const ids = [...selected]; selected.clear(); choose(ids, 'skip'); }, 'chip'),
        button('Clear', () => { selected.clear(); drawBulk(); }, 'ghost'))] : []));
    bulk.style.display = n ? 'block' : 'none';
  };
  const reviewRow = e => {
    const box = el('input', {type: 'checkbox', checked: selected.has(e.uuid)});
    box.addEventListener('change', () => { box.checked ? selected.add(e.uuid) : selected.delete(e.uuid); drawBulk(); });
    return el('div', {className: 'review'},
      el('div', {className: 'row'}, el('label', {}, box, ` ${e.date} · ${e.description.slice(0, 26)}`), el('span', {textContent: eur2(e.total)})),
      note(`${paidBy(e)} · your share ${eur2(e.my_share)}`),
      e.in_bank ? el('div', {className: 'warn', textContent: `Looks already in your bank: ${e.in_bank.date} · ${e.in_bank.description} · ${eur2(Math.abs(e.in_bank.amount))}. If so, choose “Already in my bank”.`}) : null,
      el('div', {className: 'chips'},
        button(`Shared: add ${eur2(e.my_share)}`, () => choose([e.uuid], 'shared'), 'chip'),
        button(`Only mine: add ${eur2(e.total)}`, () => choose([e.uuid], 'mine'), 'chip'),
        button('Already in my bank', () => choose([e.uuid], 'skip'), 'chip')));
  };
  const review = card(`Tricount expenses to review (${pending.length})`,
    note('Choose what each one does to your figures. Until you do, it counts for nothing.'), bulk,
    ...(shown.length ? shown.map(reviewRow) : [note(d.count ? 'All reviewed.' : 'Nothing yet. Tap Sync Tricount.')]),
    pending.length > reviewShown ? button(`Show ${Math.min(15, pending.length - reviewShown)} more`, () => { reviewShown += 15; reload(); }, 'ghost') : null);
  drawBulk();

  // --- what you chose ---
  const stateText = e => e.choice === 'shared' ? `shared: ${eur2(e.my_share)} counts` : `only yours: ${eur2(e.total)} counts`;
  const added = d.expenses.filter(e => e.choice === 'shared' || e.choice === 'mine');
  const addedCard = added.length ? el('details', {className: 'card'},
    el('summary', {textContent: `Added to your figures (${added.length})`}),
    ...added.map(e => el('div', {className: 'review'},
      row(`${e.date} · ${e.description.slice(0, 26)}`, eur2(e.choice === 'shared' ? e.my_share : e.total)),
      note(`${paidBy(e)} ${eur2(e.total)} · ${stateText(e)}`),
      e.in_bank ? el('div', {className: 'warn', textContent: `Also looks like your bank payment on ${e.in_bank.date} (${eur2(Math.abs(e.in_bank.amount))}): it may count twice.`}) : null,
      el('div', {className: 'chips'},
        e.choice === 'shared' ? button('Make it only mine', () => choose([e.uuid], 'mine'), 'chip') : button('Make it shared', () => choose([e.uuid], 'shared'), 'chip'),
        button('Take it back', () => choose([e.uuid], ''), 'chip')))))
    : null;
  const skipped = d.expenses.filter(e => e.choice === 'skip');
  const skippedCard = skipped.length ? el('details', {className: 'card'},
    el('summary', {textContent: `Already in your bank (${skipped.length})`}),
    ...skipped.map(e => el('div', {className: 'row'},
      el('span', {textContent: `${e.date} · ${e.description.slice(0, 26)} · ${eur2(e.total)}`}),
      button('Take back', () => choose([e.uuid], ''), 'ghost')))) : null;

  root.replaceChildren(head, review, await splitCard(reload), ...(addedCard ? [addedCard] : []),
    ...(skippedCard ? [skippedCard] : []), explain);
}

// --- bank entries: split a payment in half because it is a shared cost ---
async function splitCard(reload) {
  const q = new URLSearchParams({direction: 'out', limit: String(split.shown), since: dayRange(split.days)});
  if (split.q.trim()) q.set('q', split.q.trim());
  const res = await get('/api/entries?' + q);
  const list = el('div', {});
  const draw = () => list.replaceChildren(...(res.items.length ? res.items.map(t => {
    const shared = t.share < 1;
    return el('div', {className: 'entry'},
      el('div', {className: 'row'}, el('span', {className: 'muted', textContent: t.date}),
        el('span', {className: 'neg', textContent: eur2(t.amount)})),
      el('div', {className: 'desc', textContent: t.description}),
      shared ? el('div', {className: 'muted', textContent: `counts ${Math.round(t.share * 100)}% of ${eur2(Math.abs(t.raw_amount))}`}) : null,
      button(shared ? 'Shared: count all of it' : 'Shared: count half', async () => {
        try { await post('/api/share', {tx_ids: [t.id], share: shared ? 1 : 0.5}); reload(); }
        catch (e) { alert(e.message); }
      }, shared ? 'chip on' : 'chip'));
  }) : [note('No bank spending found for that search.')]));
  draw();
  const search = el('input', {type: 'search', value: split.q, placeholder: 'Search the bank text…', className: 'wide'});
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { split.q = search.value; split.shown = 10; reload(); }, 400); });
  const period = el('select', {}, ...[[30, 'Last 30 days'], [60, 'Last 60 days'], [120, 'Last 4 months'], [365, 'Last year']].map(([n, label]) =>
    el('option', {value: n, textContent: label, selected: n === split.days})));
  period.addEventListener('change', () => { split.days = Number(period.value); split.shown = 10; reload(); });
  return card('Split bank entries with your partner',
    note('Tap “Shared: count half” on a payment that is a shared cost. Only half of it counts in your spending, averages and forecast. Tap again to count all of it.'),
    period, search, list,
    res.has_more ? button('Show more', () => { split.shown += 15; reload(); }, 'ghost') : null);
}
