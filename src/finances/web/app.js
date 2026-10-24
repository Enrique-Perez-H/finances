import {el, post, get} from './ui.js';

const TABS = [
  ['overview', 'Overview'],
  ['spending', 'Spending'],
  ['evolution', 'Evolution'],
  ['income', 'Income'],
  ['recurring', 'Recurring'],
  ['forecast', 'Forecast'],
  ['fund', 'Fund'],
];
const loaded = {};
let current = localStorage.getItem('tab') || 'overview';
const root = document.getElementById('app');

async function show(name) {
  if (!TABS.some(t => t[0] === name)) name = 'overview';
  current = name;
  try { localStorage.setItem('tab', name); } catch (e) { /* private mode */ }
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  root.replaceChildren('Loading…');
  try {
    loaded[name] ??= await import(`./views/${name}.js`);
    await loaded[name].render(root);
  } catch (e) {
    root.replaceChildren(el('div', {className: 'card', textContent: `Could not load: ${e.message}`}));
  }
}

document.getElementById('tabs').replaceChildren(...TABS.map(([id, label]) => {
  const b = el('button', {textContent: label});
  b.dataset.tab = id;
  b.addEventListener('click', () => show(id));
  return b;
}));

async function sync(inputs = {}) {
  const btn = document.getElementById('sync'), status = document.getElementById('status');
  btn.disabled = true;
  status.replaceChildren('Syncing… if Trade Republic asks, confirm the login in its app.');
  try {
    const s = await post('/api/sync', {inputs});
    const parts = [];
    for (const [name, v] of Object.entries(s)) {
      if (v.state === 'needs_input' && v.field) {
        const input = el('input', {type: 'text', autocomplete: 'one-time-code',
          placeholder: v.url ? 'paste address or code' : 'code'});
        const go = el('button', {textContent: 'Send'});
        go.addEventListener('click', () => sync({[name]: input.value.trim()}));
        const kids = [`${name}: ${v.prompt} `];
        if (v.url && v.url.startsWith('https://')) {
          kids.push(el('a', {href: v.url, target: '_blank', rel: 'noopener', textContent: 'Open bank'}), ' ');
        }
        parts.push(el('div', {}, ...kids, input, go));
      } else if (v.state === 'needs_input') {
        parts.push(el('div', {textContent: `${name}: ${v.prompt}`}));
      } else {
        parts.push(el('div', {textContent: `${name}: ${v.state}${v.error ? ' – ' + v.error : ''}${v.note ? ' – ' + v.note : ''}`}));
      }
    }
    status.replaceChildren(...parts);
    await show(current);
  } catch (e) { status.textContent = 'Sync failed: ' + e.message; }
  btn.disabled = false;
}

document.getElementById('sync').addEventListener('click', () => sync());

show(current);

// Returning from the bank: /callback?code=...&state=... -> hand the address to the connector.
if (location.pathname === '/callback') {
  const address = location.href;
  history.replaceState(null, '', '/');
  sync({enable_banking: address});
}
