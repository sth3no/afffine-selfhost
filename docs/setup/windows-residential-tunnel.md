# Residential tunnel on Windows 11 (via WSL2)

Use your Windows 11 PC as the residential-IP gateway for the Affine
ingest stack. Same architecture as the Pi-based setup
(Tailscale + gost), but running inside WSL2 Ubuntu so the existing
Linux setup script works as-is.

> **Caveat:** Windows PCs typically aren't always-on. When the PC
> sleeps, hibernates, or is powered off, the tunnel drops and YT
> captures fall back to the cloud-IP path (where most fail). This
> setup is great for **testing** and **active-use bursts**, but for
> 24/7 capture you want a Pi or NAS instead. See
> [`raspberry-pi-5-spec.md`](raspberry-pi-5-spec.md) for the
> always-on hardware path.

---

## What you'll have when you're done

- Windows hosts WSL2 Ubuntu with Tailscale + gost running.
- Your Windows PC appears in your Tailscale admin console as a
  "node" — your Affine VPS can reach it over WireGuard.
- VPS containers route YT traffic through this PC, egressing via
  your home internet.

Total time: ~15 minutes (most of it is WSL install + reboot).

---

## Step 1: Install WSL2 Ubuntu

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu
```

This installs WSL2 (if not already enabled) plus Ubuntu. Reboot
when prompted.

After reboot, Windows will finish setup and prompt you for an
Ubuntu username + password. Pick anything memorable — you'll only
use this account for tunnel maintenance.

> **Already have WSL2?** Skip to step 2. Verify with:
> ```powershell
> wsl --list --verbose
> ```
> You want a distro listed with `VERSION 2`. If it's `1`, upgrade:
> ```powershell
> wsl --set-version <distro-name> 2
> ```

---

## Step 2: Run the residential-tunnel setup inside WSL2

Open **Ubuntu** from the Start menu (it'll launch a WSL terminal).
Inside that terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/sth3no/afffine-selfhost/main/scripts/pi-residential-tunnel-setup.sh -o tunnel-setup.sh
sudo bash tunnel-setup.sh
```

The script:
1. Installs Tailscale + gost.
2. Configures gost as a systemd service listening on `:8118`.
3. Brings up Tailscale.

When prompted, it'll print a URL like `https://login.tailscale.com/a/...`.
Open it in any browser, sign in (free account if you don't have one),
and approve the device. Name it something memorable like
`affine-windows-tunnel`.

When the script finishes it prints the WSL2 instance's Tailscale IP —
note it down.

---

## Step 3: Make WSL auto-start with Windows (optional but recommended)

WSL2 doesn't auto-start at Windows boot by default. Without this
step, the tunnel won't run unless you manually open Ubuntu from
the Start menu after each reboot.

The simplest way to auto-start:

1. Press `Win + R` → type `taskschd.msc` → Enter.
2. **Action → Create Basic Task…**
3. Name: `WSL Tunnel Auto-Start`
4. Trigger: **When I log on**
5. Action: **Start a program**
6. Program/script: `wsl.exe`
7. Arguments: `-d Ubuntu -u root systemctl start affine-gost-proxy tailscaled`
8. Finish.

After the next reboot/login, WSL spins up silently with both services
running.

> **Even better (Windows 11 22H2+):** You can enable systemd
> auto-start by setting `boot.systemd=true` in `/etc/wsl.conf` inside
> WSL. Then the gost service starts automatically when WSL is
> launched. Combined with the scheduled task above, this is the
> closest WSL gets to "always on."

---

## Step 4: Install Tailscale on the VPS

SSH into your Hetzner VPS (or whatever cloud you're on) as root:

```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --hostname=affine-vps
```

Sign in with the same Tailscale account. Both nodes are now on the
same private network.

Verify reachability from the VPS:

```bash
tailscale status
# you should see:
# 100.x.x.x   affine-windows-tunnel   ...
# 100.y.y.y   affine-vps              ... (this machine)

# ping the Windows machine over Tailscale
tailscale ping affine-windows-tunnel
```

---

## Step 5: Wire it into the Affine stack

In Portainer → your AFFiNE stack → **Environment variables**, set:

```
RESIDENTIAL_PROXY_URL=http://<windows-tunnel-tailscale-ip>:8118
```

Replace `<windows-tunnel-tailscale-ip>` with the IP from step 2.

Click **Update the stack** with **Re-build** ✅.

---

## Step 6: Verify the egress IP

```bash
docker exec affine_cobalt env | grep -i proxy
# should show: HTTP_PROXY=http://100.x.x.x:8118  HTTPS_PROXY=...

docker exec affine_ingest python3 -c \
  'import urllib.request; print(urllib.request.urlopen("https://api.ipify.org",timeout=10).read().decode())'
```

The IP printed should be **your home WAN IP** (the one your
Windows PC sees as its public IP — check at
<https://whatismyipaddress.com/> in your normal browser).

If it's still your VPS IP (`135.181.22.124`-ish), the proxy isn't
being applied — recheck `RESIDENTIAL_PROXY_URL` in the stack env
and force a redeploy.

---

## Step 7: Retry a previously-failing capture

Use any YT URL that failed before with `error.api.youtube.login`.
Watch the ingest logs:

```bash
ssh root@your-vps
docker logs affine_ingest --tail 100 -f | grep -iE 'capture_id|cobalt|transcript|whisper|bot-block'
```

Expected progression:
- `cobalt: HTTP/1.1 200 OK` (no more 400 with `error.api.youtube.login`)
- A real Whisper transcript lands in the doc body.

---

## Troubleshooting

**"Tailscale ping fails between VPS and Windows"**
- Check both nodes are listed in <https://login.tailscale.com/admin/machines>.
- If Windows shows "expired", `tailscale up` again on the WSL terminal.

**"docker exec affine_cobalt sees the env but the proxy doesn't seem applied"**
- Containers cache env vars at start. After updating the stack,
  if Portainer didn't recreate the containers (e.g. partial update),
  do `docker compose up -d --force-recreate cobalt yt_session_server ingest`.

**"WSL2 stops working after a Windows update"**
- Run `wsl --update` from PowerShell. Reboot. WSL preserves data
  across updates.

**"My Windows PC went to sleep and now captures are failing again"**
- Expected. Either:
  - Set Windows to never sleep (Settings → System → Power → Sleep → Never).
  - Buy a Pi (see `raspberry-pi-5-spec.md`).

**"How do I check if gost is actually running inside WSL?"**
```bash
wsl -d Ubuntu -u root systemctl status affine-gost-proxy
```
