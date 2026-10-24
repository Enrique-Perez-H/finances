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

## Remove the demo data from your real vault

Demo accounts ended up in the real vault if you tapped Sync before `sources` was set.
Delete them (this touches only the `demo` provider, never Trade Republic):

```sh
cd ~/finances && git pull origin ccr-a802b8c6-fkp9l3
finances forget demo
finances secret sources     # type: ["trade_republic", "enable_banking"]
```

## Santander (via Enable Banking)

One-time registration on your computer or phone browser (free, own accounts only):

1. Sign in at https://enablebanking.com (control panel, email login).
2. **API applications → Add a new application.** Choose **Production** (not Sandbox).
   Name it e.g. "My finances". Redirect URL: `http://localhost:8765/callback`
   (if the form refuses `http://localhost`, use `https://example.com/callback` instead;
   the dashboard also accepts the address pasted back by hand).
3. On Register, a file `<application id>.pem` is downloaded. That is your **private key**:
   keep it out of git and delete it once it is stored in the vault.
4. Click **Activate by linking accounts** and approve in Santander. This makes the app
   usable for your own accounts (restricted mode).

Store the details in the vault. The key is a multi-line file, so it is read from the file
rather than typed (copy the downloaded `.pem` into Termux's storage first, e.g. with
`termux-setup-storage`, then it appears under `~/storage/downloads`):

```sh
finances secret eb_app_id          # the application id (uuid)
finances secret-file eb_private_key ~/storage/downloads/<application id>.pem
```

Then `finances serve`, tap **Sync**. You get an **Open bank** link: approve in Santander,
and you return to the dashboard, which finishes the connection. If you land on a page that
does not load, copy the address from the browser bar, paste it in the box and tap Send.
Consent lasts up to what the bank allows (often 90 to 180 days); then the same step repeats.
