# Running on Android with Termux

1. Install **Termux** from F-Droid (not the Play Store), plus optionally Termux:Widget.
2. In Android settings, disable battery optimisation for Termux and set a screen lock.
3. In Termux:
   ```sh
   pkg update && pkg install python python-cryptography git
   git clone <this repo> && cd finances
   pip install --no-deps -e .
   ```
4. Try it with fake data (separate vault, nothing real involved):
   ```sh
   finances --demo serve
   ```
   Open `http://localhost:8765` in Chrome, then menu → *Add to Home screen*.
5. Your real data and secrets live in `~/.finances` inside Termux's private storage,
   encrypted with your passphrase. Keep the passphrase out of shell history: let the
   app prompt for it rather than exporting `FINANCES_PASSPHRASE`.

Run `termux-wake-lock` while a long sync is running.
