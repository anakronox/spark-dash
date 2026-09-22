# Fleet updates: the quick setup

**This is optional.** The dashboard is complete without it. Skip this page and
there is no extra mount, no extra login, and no Updates button — nothing else
behaves differently.

Turn it on and the dashboard can do for every Spark what NVIDIA's DGX Dashboard
does for one: check hourly whether a release is available, show you exactly
what would be installed, and — when you press the button and type your
password — run `apt full-upgrade`, `fwupdmgr upgrade` and the restart, one
Spark at a time.

---

## Before you start

| you need | why |
|---|---|
| **An SSH key the dashboard can use** | Updates reach a Spark over SSH. That is NVIDIA's own fleet mechanism, not something invented here — see [fleet-updates.md](fleet-updates.md) §5. |
| **A login on each Spark that can run `apt`** | The same login you use yourself is fine. It needs `sudo`; whether it needs a *password* is your choice, covered below. |
| **The ability to edit your compose file and redeploy** | The key has to be mounted into the backend container. The dashboard cannot do this for itself, which is why this page exists. |
| **A few minutes per Spark, once** | After that it is a checkbox. |

**What it will never do on its own.** It checks hourly; it does not install.
Every update is something a person pressed, after a confirmation naming the
release and the packages. Nothing is on a timer except the read-only check.

---

## 1. Make a key

On the machine that runs your dashboard's central stack:

```bash
mkdir -p central/fleet-ssh central/fleet-state
ssh-keygen -t ed25519 -N '' -C spark-dash-fleet -f central/fleet-ssh/id_ed25519
chmod 700 central/fleet-ssh
sudo chown -R 10002:10002 central/fleet-ssh central/fleet-state
```

No passphrase: nobody is there to type one when the container starts.

**That `chown` is not optional, and skipping it fails confusingly.** The
dashboard runs as uid 10002 inside its container, and `ssh` refuses to use a
private key that belongs to somebody else — so a key left owned by you reads as
"unreadable" in Settings even though it is plainly there. The state directory
needs it for a different reason: that is where `ssh` records each Spark's host
key the first time it connects.

## 2. Put it on each Spark

```bash
ssh-copy-id -i central/fleet-ssh/id_ed25519.pub you@your-spark
```

Repeat for every Spark you want checked. Test it:

```bash
ssh -i central/fleet-ssh/id_ed25519 you@your-spark 'echo it works'
```

## 3. Decide about `sudo`

Two honest options, and the second is not the wrong one:

- **Type your password each time.** Nothing more to do. The dashboard asks for
  it when you press Update, holds it for that one run, and writes it nowhere.
  It is **refused unless the page is on HTTPS** — through a tunnel, or with TLS
  in front — because a sudo password should not cross a LAN in the clear.
- **Passwordless sudo for that login**, if you would rather not be asked. That
  is a change to the Spark, so it is yours to make and not ours to suggest
  lightly.

## 4. Add the mounts and redeploy

The fleet bits live in their own compose file so that leaving them out is the
default rather than a deletion. In `central/.env`:

```
COMPOSE_FILE=compose.yaml:compose.fleet.yaml
SPARK_FLEET_SSH_USER=you          # the login from step 2
```

Then redeploy the central stack the way you normally do.

## 5. Switch it on

Open the dashboard, then **Settings → Fleet updates**. It will say the key is
found. Turn the switch on, then tick **fleet updates** under each Spark you
want checked — the dashboard already knows their names and addresses from
`cluster.yml`, so there is nothing to type twice.

Within a minute or so an **Updates** button appears in the header.

---

## Check it worked

```bash
curl -s localhost:8080/health | grep fleet
```

`"fleet_updates": "ok"` means the key is mounted, the login is set and the
checker is running. `"not configured"` means something in step 4 has not
arrived — Settings will name which piece.

---

## One thing to know about the DGX Dashboard

Each Spark's own DGX Dashboard is what refreshes its package lists, hourly. If
you have turned its updater off — or something turned it off for you — that
Spark stops seeing new packages entirely, and this tool, which reads the same
lists, goes blind with it. **Leave the DGX Dashboard's updates enabled.**

While an update installs, this tool pauses that updater so two installers never
fight over the package manager, and puts it back exactly as it found it the
moment the install ends. If it ever cannot, the dashboard says so on the node's
row and in `/health` — and the one-line fix is on the Spark:

```bash
sudo rm /opt/nvidia/dgx-dashboard/settings.json
sudo systemctl restart dgx-dashboard-admin.service
```

---

## Turning it off

- **For now:** the switch in Settings. Checking stops; nothing is un-mounted,
  and a Spark mid-update is left alone to finish. This is the right setting for
  "I know there is a bad kernel in the repos this week".
- **For good:** remove the `COMPOSE_FILE` line from `.env` and redeploy. The
  button and the section go with it.
- **On a Spark:** delete the dashboard's key from its `~/.ssh/authorized_keys`.
  That is the whole of the access it had.

---

## If something is wrong

| what you see | what it means |
|---|---|
| Settings says a requirement is missing | Exactly that one. **`ssh_key_readable` false with the key clearly present is the `chown` from step 1** — uid 10002 has to own it. The others are step 4. |
| A Spark reads **could not connect** | The key is not on that Spark, or the login is wrong. Re-run step 2 and test it by hand. |
| **firmware needs the two read-only sudo rules** | Firmware versions come from `dmidecode` and `mstflint`, which need root to read. The row still scores everything else. |
| Update refuses your password | The page is not on HTTPS. Reach the dashboard through your tunnel, or use passwordless sudo. |
| A Spark says **N packages pinned here** | Somebody ran `apt-mark hold` there. Those will not be installed. `sudo apt-mark unhold <package>` on the Spark undoes it. |

Deeper detail — what NVIDIA actually ships, how releases are scored, what the
update sequence runs — is in [fleet-updates.md](fleet-updates.md).
