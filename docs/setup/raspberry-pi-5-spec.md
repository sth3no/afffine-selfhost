# Raspberry Pi 5 spec for the Affine residential tunnel

A spec sheet for buying a Pi (or equivalent) to host the residential
tunnel that bypasses YouTube's cloud-IP wall. See
[`windows-residential-tunnel.md`](windows-residential-tunnel.md) if
you want to use a Windows PC temporarily before committing to
hardware.

---

## TL;DR shopping list (Czech market, prices May 2026)

| Item | Where | Price (CZK) | Notes |
|---|---|---|---|
| **Raspberry Pi 5 / 8 GB** | Alza, RPishop, Aliexpress | ~3 760 | Right pick: real gigabit ethernet, M.2 PCIe via HAT, future-proof |
| Official 27 W USB-C PSU | Alza | ~550 | **Don't skimp** — Pi 5 is picky about power. Off-brand chargers cause random reboots. |
| Active cooler (fan + heatsink) | Alza | ~250 | Pi 5 throttles without it under sustained load. Tunnel load is light but the case temperature still climbs. |
| Argon ONE V3 case (or similar) | Alza, RPishop | ~900 | Optional. Aluminium case + integrated cooler. Looks nice, dampens fan noise. |
| **Storage option A:** microSD 64 GB A2 | Alza | ~250 | Cheapest. SD cards die under sustained writes — fine for low-write workloads like this tunnel, but plan to replace every ~2 years. |
| **Storage option B:** USB SSD enclosure + 256 GB SATA SSD | Alza | ~700 + ~600 = 1 300 | More reliable. SSD survives years of writes. Boot from it. |
| Cat 6 ethernet cable (1 m) | Alza | ~80 | Hardwire to your router. Skip WiFi. |
| **Total (minimum)** | | **~4 890** | Pi + PSU + cooler + SD card + ethernet |
| **Total (recommended)** | | **~6 200** | Above + USB SSD instead of SD card + Argon case |

Cheaper alternative if you don't want to spend on a Pi 5 specifically:
**Pi 3B+ at 1 199 CZK** is technically enough for this workload (low
CPU, low RAM, light network). Wired ethernet is 100 Mbit on Pi 3
which is the only real downside vs Pi 5's gigabit — irrelevant for
audio captures, marginally limiting if you push heavy phase 13 video
volume through it.

---

## Why these choices

**Pi 5 over Pi 4:** Pi 4 8 GB at Alza is currently ~4 900 CZK —
*more* expensive than Pi 5 8 GB. No reason to pick the older board
at this price.

**Pi 5 over Pi 3B+:** mostly future-proofing. The tunnel itself
runs on a Pi Zero 2 W if you really squeezed. If you'll do anything
else with this Pi over the next 5 years (Home Assistant, Jellyfin,
NAS, ad-blocker, dev sandbox), Pi 5 has the headroom; Pi 3B+ doesn't.

**8 GB RAM:** overkill for the tunnel (uses ~50 MB), justified for
secondary uses. The Pi 5 4 GB at ~3 200 CZK is also fine.

**SSD over SD card:** SD cards die under sustained writes. Container
logs, journald, swap thrash — they all chew through SD lifespan.
USB SSDs cost an extra ~700 CZK and last 5+ years.

**Active cooling:** Pi 5 idles at ~50°C and throttles at ~85°C. Even
under light load, summer ambient + a closed plastic case can push it
into throttle. A passive heatsink works for ambient-cool rooms;
active cooler (fan) is bulletproof. Argon case bundles both.

**Wired ethernet only:** 24/7 reliability >> WiFi convenience. If
your router is in a different room from where you want to put the
Pi, get a powerline adapter (~600 CZK pair) instead of WiFi.

---

## Power consumption / running cost

- Pi 5 idle: ~3 W
- Pi 5 under tunnel load: ~5 W
- 24/7 average: ~4 W = 35 kWh/year

At ~5 CZK/kWh (Czech residential rate, 2026) = **~175 CZK/year** in
electricity. Negligible.

---

## Setup workflow once it arrives

1. Flash **Raspberry Pi OS Lite (64-bit)** to your boot media using
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
   In the Imager:
   - Click the gear icon (advanced options) BEFORE writing.
   - Set hostname (e.g. `affine-tunnel`).
   - **Enable SSH with public-key auth** — paste your laptop's
     `~/.ssh/id_*.pub` here.
   - Set username + password (you won't use the password if SSH key
     is set up).
   - Configure WiFi only if no ethernet — this guide assumes wired.
   - Set locale + timezone.
2. Insert SD card / USB SSD into the Pi, plug in ethernet, plug in
   power.
3. Wait ~60 seconds for first boot. Find its IP from your router's
   DHCP table (or run `arp -a` from any other machine on the network).
4. SSH in: `ssh affine-tunnel.local` (or use the IP).
5. Run the same setup script as documented in the main README:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/sth3no/afffine-selfhost/main/scripts/pi-residential-tunnel-setup.sh -o tunnel-setup.sh
   sudo bash tunnel-setup.sh
   ```
6. Authenticate Tailscale via the printed URL.
7. Set `RESIDENTIAL_PROXY_URL=http://<pi-tailscale-ip>:8118` on the VPS.

---

## Migrating from Windows-WSL to Pi

If you set up the Windows tunnel first as a temporary path, switch to
the Pi later by:

1. Set up the Pi with the same script.
2. Update `RESIDENTIAL_PROXY_URL` on the VPS to the Pi's tailnet IP.
3. Redeploy the stack.
4. (Optional) On Windows: stop the WSL services or remove the
   Tailscale node from your tailnet admin to keep the device list
   clean. The WSL stuff doesn't hurt anything if left running — it's
   just unused.

The Tailscale node names tell you which is which: `affine-residential-pi`
(the Pi) vs `affine-windows-tunnel` (the WSL instance) per how the
script names them.
