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

## Trade Republic (real data)

`pytr` is unofficial and its dependency `curl-cffi` cannot be built on Termux, so install
the pinned version without its dependencies and add only the pure-Python ones
(the connector works without `curl-cffi`):

```sh
pip install --no-deps pytr==0.4.10
pip install requests websockets certifi coloredlogs packaging babel
```

Then create your real vault (no `--demo`) and store your details. Values are typed hidden
and kept encrypted:

```sh
finances secret tr_phone          # e.g. +34600123456
finances secret tr_pin
finances secret sources           # type: ["trade_republic"]
finances serve
```

Open `http://localhost:8765` and tap **Sync**:

- If your Trade Republic account uses an authenticator app, a code box appears: type the
  current code and tap Send.
- Otherwise open the Trade Republic app and **confirm the login**. The sync waits up to
  about two minutes.
- After a successful login the session is stored encrypted in the vault, so the next syncs
  usually need no confirmation until Trade Republic expires the session.

It reads cash and portfolio positions only. Nothing that trades or changes anything is
called. Transactions history is not imported yet.
