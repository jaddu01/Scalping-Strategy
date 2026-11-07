# Deploying to a server

This puts the platform on a cloud server in Mumbai with a fixed (static) IP, behind your own domain
with HTTPS. It runs as a service that starts on boot and restarts if it ever stops, and the database
is backed up daily. You do steps 1–3 in web consoles. Steps 4–5 are one command each, and the scripts
in [`deploy/`](../deploy) do the rest.

```
your browser ──HTTPS──► Caddy (certificate, port 443) ──► platform (127.0.0.1:8000) ──► Angel One API
                         on the server                     systemd service "algo"        from the static IP
```

---

## 1. Create the server

Any Ubuntu 24.04 server works. The example uses **AWS Lightsail**, which gives a free static IP.

1. Lightsail → **Create instance** → region **Mumbai (ap-south-1)** → **Linux/Unix** → **OS only: Ubuntu 24.04 LTS**.
2. Plan: **2 GB RAM** is enough (1 GB also works). Keep the default SSH key, or upload your own.
3. After it starts: **Networking → Create static IP**, attach it to the instance, and note the IP.
4. On the instance's **Networking** tab, IPv4 firewall: allow **SSH (22)**, **HTTP (80)** and **HTTPS (443)**.
   The server's own firewall is set up by the script too.
5. Check you can log in from the Mac: `ssh ubuntu@YOUR_STATIC_IP`. With Lightsail's default key, download it
   and use `ssh -i ~/Downloads/LightsailDefaultKey-ap-south-1.pem ubuntu@YOUR_STATIC_IP`. It's easiest to add it
   to `~/.ssh/config`.

Other providers work the same way: DigitalOcean (Bangalore, *reserved IP*), Hetzner, a Google Cloud or Azure VM
in India. What matters is Ubuntu 24.04, a static IP, and ports 22/80/443 open.

## 2. Point your domain at it

At your domain registrar or DNS provider, add an **A record**:

| Name | Type | Value |
|---|---|---|
| `algo` (for `algo.yourdomain.com`) | A | YOUR_STATIC_IP |

Wait until `dig +short algo.yourdomain.com` on the Mac prints the IP. That usually takes a few minutes. The
HTTPS certificate can only be issued once this works.

## 3. Register the static IP with Angel One

Under SEBI's retail-algo rules, Angel One accepts API **orders** only from an IP registered on your SmartAPI app.
Sign in to the SmartAPI developer portal, open **My Apps**, edit the app whose API key the platform uses, and set
the **static IP** to the server's IP. Market data and paper trading work without this, but **live orders will be
rejected** until it's registered. Confirm the exact steps in Angel's portal, because they change it from time to time.

## 4. Upload from the Mac

From the project folder on the Mac:

```bash
cd "/Users/jaddu/Scalping Strategy/algo_platform"
deploy/push.sh ubuntu@YOUR_STATIC_IP --with-data
```

`--with-data` copies this Mac's database (users, broker accounts, strategy instances, settings, trade history)
and its `.env`. The `.env` matters because it holds the **master key** that decrypts the stored broker
credentials. Use it only the first time; the script refuses to overwrite a database already on the server.
Without `--with-data` the server starts empty with a new master key, and you add the admin and accounts again.

> **Stop the platform on the Mac after this.** Two copies logged into the same Angel account fight over the
> session, and in Live mode both would place orders.

## 5. One-time server setup

```bash
ssh ubuntu@YOUR_STATIC_IP 'sudo bash /opt/algo_platform/deploy/setup_server.sh algo.yourdomain.com'
```

This takes 2–3 minutes and is safe to run again. It sets the IST timezone and time sync, turns on the firewall
and automatic security updates, creates the `algo` user, installs pinned Python packages, writes `.env` for
server use, installs the `algo` systemd service, configures Caddy with an automatic HTTPS certificate, and adds a
daily backup. At the end it prints the server's IP; check it matches the one registered with Angel.

If you **didn't** copy data, create the admin from the server shell. The web `/setup` page is blocked on the server:

```bash
ssh ubuntu@YOUR_STATIC_IP 'sudo -u algo /opt/algo_platform/.venv/bin/python /opt/algo_platform/run.py create-admin admin'
```

## 6. First login and checks

Open `https://algo.yourdomain.com` and sign in.

1. **Profile → enable two-factor (TOTP).** The page is now on the internet, so do this first.
2. **Accounts:** *Angel A/c 1* should show **Connected**, and the dashboard's data feed should be **Angel A/c 1**.
3. **Accounts → Paper Trade** (the simulated account): **disable** it. Your instance runs paper trades on
   *Angel A/c 1* in Paper mode, with real prices, so the simulator isn't needed.
4. **Strategies:** the instance should be **Paper** mode on *Angel A/c 1*. Check **Lots** (it is currently 10;
   the strategy sheet says 5).
5. Leave it in Paper mode for a few trading days and compare its trades with the market.

## 7. Going live

When the paper results look right and Angel has the static IP: **Strategies → edit the instance → Mode: Live**
(or create a separate Live instance). Set the risk limits first: max daily loss, max lots per order, and max
premium per trade.

---

## Day-to-day

| Task | Command (from the Mac unless noted) |
|---|---|
| Deploy a code update | `deploy/push.sh ubuntu@IP` (it won't restart during market hours 09:00–15:35; re-run after, or add `--force`) |
| Live log | `ssh ubuntu@IP 'journalctl -u algo -f'` |
| Event log files | `/opt/algo_platform/data/logs/events.jsonl` (also on the Logs page) |
| Restart / stop / start | `ssh ubuntu@IP 'sudo systemctl restart algo'` (`stop`, `start`, `status`) |
| Backups | daily at 16:05 IST in `/opt/algo_platform/data/backups/`, 30 days kept |
| Copy a backup to the Mac | `scp ubuntu@IP:/opt/algo_platform/data/backups/algo-2026-11-06.db .` |
| Locked out of the web UI | `ssh ubuntu@IP 'sudo -u algo /opt/algo_platform/.venv/bin/python /opt/algo_platform/run.py reset-password admin'` |

**Restoring a backup:** `sudo systemctl stop algo`, copy the backup over `/opt/algo_platform/data/algo.db`,
run `sudo chown algo:algo` on it, then `sudo systemctl start algo`.

**Keep safe, offline:** a copy of `/opt/algo_platform/.env` (master key). Without it, the stored broker credentials
in any backup can't be decrypted.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Browser can't connect / certificate error | DNS not pointing at the server yet, or port 80/443 closed in the provider's firewall. Check `sudo journalctl -u caddy -n 50` |
| "502 Bad Gateway" | The app isn't running. Check `sudo systemctl status algo` and `journalctl -u algo -n 100` |
| Angel login fails every morning | Wrong TOTP secret, or the server clock is off (`timedatectl` should say *synchronized: yes*) |
| Live orders rejected by Angel | Static IP not registered on the SmartAPI app (step 3) |
| Dashboard doesn't update live | Something between you and the server blocks WebSockets (Caddy itself handles them) |

---

## Docker on a shared server (current deployment)

The live deployment shares `65.0.62.220` (Ubuntu 26.04, Mumbai) with another Docker app, `algo-trading`, in
`/home/ubuntu/algo-trading`. That app's nginx container `algo_web` owns ports 80/443. So the platform runs as its
**own container**, and `algo_web` forwards `algo.cloudusweb.com` to it. The systemd/Caddy steps above are **not**
used on this server.

```
browser ─HTTPS─► algo_web (existing nginx, 80/443) ─► algo_platform:8000 (Docker network algo-trading_default)
                 conf.d/algo-platform.conf             /opt/algo_platform (code)  data/ .env  (mounted volumes)
```

| What | Where |
|---|---|
| Code | `/opt/algo_platform` (uploaded by `deploy/push_docker.sh`) |
| Database, logs, cache | `/opt/algo_platform/data` (owned by uid 10001, the container user) |
| Master key | `/opt/algo_platform/.env` (mode 600). **Keep an offline copy** |
| Container definition | `deploy/docker/compose.yml`, `deploy/docker/Dockerfile` (memory cap 600 MB, auto-restart) |
| nginx server block | `/etc/algo-platform/nginx.conf`, mounted into `algo_web` by `/home/ubuntu/algo-trading/docker-compose.override.yml` (their `deploy.sh` doesn't overwrite it) |
| Certificate | `/etc/letsencrypt/live/algo.cloudusweb.com/`, webroot renewal via `/etc/letsencrypt/acme-webroot` (no nginx downtime) |
| Daily backup | `/etc/cron.d/algo-platform-backup`, 16:05 IST weekdays → `/opt/algo_platform/data/backups/`, 30 days |
| Local-only port | `127.0.0.1:8100` on the server (for an SSH tunnel; not reachable from the internet) |

**Common commands** (run from the Mac; key `~/Downloads/algo-trading.pem`):

```bash
# update code (refuses during market hours or with an open trade unless --force)
deploy/push_docker.sh

# live log / status / restart
ssh -i ~/Downloads/algo-trading.pem ubuntu@65.0.62.220 'sudo docker logs -f --tail 100 algo_platform'
ssh -i ~/Downloads/algo-trading.pem ubuntu@65.0.62.220 'sudo docker ps --filter name=algo_platform; sudo docker stats --no-stream algo_platform'
ssh -i ~/Downloads/algo-trading.pem ubuntu@65.0.62.220 'sudo docker restart algo_platform'

# open the dashboard without the domain (SSH tunnel), then browse http://localhost:8100
ssh -i ~/Downloads/algo-trading.pem -N -L 8100:127.0.0.1:8100 ubuntu@65.0.62.220

# reset a password
ssh -i ~/Downloads/algo-trading.pem ubuntu@65.0.62.220 -t 'sudo docker exec -it algo_platform python run.py reset-password admin'
```

**Things to know about this server**
- **Same Angel account as the old app.** `algo-trading` also logs into Angel client AACH255417. Running two programs
  on one account causes session and rate-limit conflicts ("access rate exceeded", tick-stream drops), and in Live
  mode both could place orders. Stop the old app's Angel use, or give one of them a different account, before
  switching this platform to Live.
- **Memory:** 908 MB total. The platform uses ~200–260 MB, with a peak of ~370 MB while loading Angel's instrument
  list each morning. 2 GB RAM is recommended.
- **The old app's certificate** (`cloudusweb.com`, expires 9 Jan 2027) uses certbot's *standalone* mode, which can't
  renew while nginx holds port 80. Switch it to webroot like the subdomain (`certbot reconfigure` or re-issue with
  `--webroot -w /etc/letsencrypt/acme-webroot`) before December.
- PostgreSQL (5432), Redis (6379), 3000 and 5173 of the old app are open to the internet (left as is, by choice).
