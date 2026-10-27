import {card, el, get, note, button, downloadPost} from '../ui.js';

// Downloads an encrypted copy of the vault. Move it to a cloud drive: if the phone is lost,
// this file plus your passphrase is the only way back.
export async function backupCard() {
  const {last} = await get('/api/backup/status');
  const info = note(last ? `Last backup: ${last.slice(0, 10)}` : 'No backup made yet.');
  const out = el('div', {className: 'muted'});
  const make = button('Create backup', async () => {
    out.textContent = 'Creating…';
    try {
      const name = await downloadPost('/api/backup');
      out.textContent = `Saved as ${name} in your Downloads. Now move it to your cloud drive.`;
    } catch (e) { out.textContent = 'Failed: ' + e.message; }
  });
  return card('Backup',
    note('Everything lives only on this phone. The backup is encrypted with your passphrase, so it is safe to store online, but the passphrase cannot be recovered.'),
    info, make, out);
}
