[🇬🇧 English](README.md) | [🇩🇪 Deutsch](README.de.md)

**Current Version 1.1**

[![Debian CI](https://github.com/roolrz/OpenDVM/actions/workflows/ci.yml/badge.svg)](https://github.com/roolrz/OpenDVM/actions/workflows/ci.yml)

tested on:

MMDVM-USB Boards:
- Debian-based distributions (PC)
- minimal Debian (console only)
- Debian VM in Proxmox

MMDVM-GPIO Boards & MMDVM-USB Boards:
- Raspberry Pi Zero 2W
- Raspberry Pi 3/A/B/+
- Raspberry Pi 4
- Raspberry Pi 5

# 🛰️ OpenDVM MultiMode Repeater/Station for Debian/Linux and MMDVM Boards

This project builds upon the **Digital Voice (DV) modules by G4KLX**, which are combined into a complete solution like **Pi-Star**.  
However, Pi-Star is designed **exclusively for the Raspberry Pi**.

👉 **The goal of this project** is to create a **fully functional, platform-independent multimode DV solution** that runs on **any Debian-based system** – highly reliable on real PCs or servers (PC, virtual machines, Raspberry, Odroid, Orange Pi and many more).

The current version supports **D-Star, DMR and YSF (C4FM) on MMDVM Boards** (USB Boards or HATs).

It uses the following G4KLX repositories:

- [MMDVMHost](https://github.com/roolrz/MMDVMHost)  this is a modified version of the original MMDVMHost to support simplex D-Star operation
- [ircDDBGateway](https://github.com/roolrz/ircDDBGateway)
- [DMRGateway](https://github.com/roolrz/DMRGateway)
- [YSFClient](https://github.com/roolrz/YSFClients) *(used as a gateway)*

The **entire installation** is automated through **shell scripts** that correctly set up all components.  

Additionally, the project includes a **logfile parser** that reads all operational data from MMDVM, YSF, and DMR logs and writes them into a **MariaDB database**.  
These data serve as the backend for a modern **web dashboard** that displays the repeater or hotspot status in real time.

This project was originally developed for the MMDVM Repeater Builder board used in the DB0SL multimode repeater. It also runs with common MMDVM Raspberry Pi HATs. For other specialized hardware or use cases, adjust the configuration files as needed (see the [Modem] section in MMDVMHost.ini).

---

## 📖 Table of Contents

1. [Overview](#-overview)  
2. [Architecture](#-architecture)  
3. [Backend – Log Monitor & Database](#-backend--log-monitor--database)  
4. [Installation & Dependencies](#-installation--dependencies)  
5. [Update](#-Update)
6. [Configuration](#-configuration)  
7. [Web Frontend](#-web-frontend)  
8. [System Setup & Maintenance](#-system-setup--maintenance)  
9. [Credits & License](#-credits--license)

---

<a href="gui.png">
  <img src="gui.png" alt="Systemübersicht" width="250">
</a>

<a href="gui1.png">
  <img src="gui1.png" alt="Systemübersicht" width="250">
</a>

🔗 **Live Installation:** [digital.db0sl.de](https://digital.db0sl.de/)

## 🔍 Overview

**Main features:**

- Real-time monitoring of MMDVMHost, YSFGateway, and DMRGateway logs  
- D-Star Transceiver work in Repeater AND **Simplex** mode. **The dup-, shift 0 hack is no more required**
- Automatic storage of detected events in MariaDB  
- Graphical presentation via a modern web frontend  
- Central configuration through a unified `site.conf` file  
- Automatic generation of all gateway configuration files  
- Fully passwordless, secure database access  
- No frameworks, no Pi dependency – runs on any Debian system

---

## ⚙️ Architecture

<img src="flowchart.png" alt="Systemübersicht" width="200">

---

## 🧠 Backend – Log Monitor & Database

The main program continuously monitors the following log files:

- `/var/log/mmdvm/MMDVM-YYYY-MM-DD.log`  
- `/var/log/mmdvm/YSFGateway-YYYY-MM-DD.log`  
- `/var/log/mmdvm/DMRGateway-YYYY-MM-DD.log`

New entries are immediately detected, parsed, and written into the database.

### Captured Information

- TX activities and callsigns for D-Star, DMR, and System Fusion  
- Duration and BER of each transmission  
- Current mode of operation  
- Reflector status for D-Star, Fusion, and DMR  
- Automatic detection of log rotation and truncation  
- Reconnects automatically after database errors  

### Database Tables

| Table | Description |
|--------|--------------|
| `status` | Current status and independent DMR TS1/TS2 status (callsign, TG, RF/NET, duration, BER) |
| `lastheard` | Every transmission with timestamp |
| `reflector` | Current reflector per mode |
| `config_inbox` | Configuration |

### Special Features

- Detection of interrupted transmissions (heuristic timing)  
- Callsign validation (min. 3 characters, at least 1 digit)  
- D-Star does not store DG-ID, Fusion does  
- “Watchdog expired” messages are treated as EOT  
- DMR master names (e.g., `BM_2621_Germany`) are recognized automatically  

D-Star link announcements are recognized in English and German. DMR TS1 and TS2
are tracked independently and displayed together, including overlapping calls.

---

### Duplex

There are two types of MMDVM HATs:

Simplex: most MMDVM boards are "simplex" boards with one or two antennas<br>
Duplex: special MMDVM boards e.g., "repeater builder" boards

The “Duplex” setting must match the hardware in use; otherwise, operation may only work in one direction. Usually "0" will be used for homebrew stations.

## 🧰 Installation & Dependencies

Installation is fully automated through **shell scripts**, which install all dependencies, programs, and configuration files.

First, download this repository from GitHub:

```bash
sudo apt update
sudo apt install git -y
git clone https://github.com/roolrz/OpenDVM.git
cd OpenDVM
```

Now run the scripts (all with sudo) as follows:

### Installation Order

👉 **Important:**  
These scripts must be executed **in this order**.

1. 🧩 **for Raspberry PI with MMDVM HAT ONLY (not for USB boards)**
   - Skip this section if you **don’t use a Raspberry Pi** or if your MMDVM is connected **via USB**. In that case, go directly to **2. Install the serial port.**
   - If you **do** have a Raspberry Pi with an **MMDVM HAT mounted directly on the GPIO header**, you need to **enable the internal serial port**. 

   Run the following command on a Raspberry Zero2W, 3 or 4 and reboot afterward:
   ```bash
   sudo ./install_raspi34.sh
   sudo reboot
   ```
   Run the following command on a Raspberry 5 and reboot afterward:

   **for Raspberry PI 5 with MMDVM HAT ONLY (not for USB boards)**
   ```bash
   sudo ./install_raspi5.sh
   sudo reboot
   ```
   - After reboot continue with **2. Install the serial port**

2. 🔌 **Install the serial port**  
   - Run the script:
   ```bash
   sudo ./install_serial.sh
   ```
   - Detects your serial device (USB, onboard UART, etc.), lets you pick the correct one
   - re-run this script to switch to a different device (e.g. a new hardware). In most cases, the shown default device is correct and can be used as is.

3. ⚙️ **Install the MMDVM System and all Gateways**  
   - Run the script:
   ```bash
   sudo ./install.sh
   ```
   - Installs all system dependencies  
   - Prepares directories (e.g., `/var/log/mmdvm`)  
   - Sets up the MariaDB database  
   - Compiles and installs the C++ backend  
   - Installs the central DV interface **MMDVMHost**
   - Installs and configures the **System Fusion Gateway**
   - Installs and configures the **D-Star Gateway**
   - Installs and configures the **DMR Gateway**

After completion, **default configuration files** are automatically copied to `/etc`.  
They must then be adjusted to match your setup – see [Configuration](#-Configuration).

---

## 🧰 Update

Works with version 1.1 and higher

If OpenDVM is already installed, performing an update is usually the best way to bring the system up to date. During an update, the configuration is **NOT** changed (unlike a fresh installation).

First, download the latest repository from GitHub:

   ```bash
   git clone https://github.com/roolrz/OpenDVM.git
   cd OpenDVM
   ```

Run this script:

   ```bash
   sudo ./install_update.sh
   ```
   - installs the latest DJ0ABR extensions and user interface
   - keeps the G4KLX modules unchanged
   - preserves your existing configuration
   - no reboot required — the system is ready for use immediately after the update. Just reload the page in your browser (Key F5)

---

## 🧾 Configuration

All site and system parameters for the G4KLX modules are stored in the following configuration files:

   /etc/MMDVMHost.ini  
   /etc/ircddbgateway  
   /etc/ysfgateway  
   /etc/dmrgateway

Sample versions of these files are included in this package and must be customized to match your station or repeater setup.

The most important settings can be configured in the SETUP window:

### Steps

1. **Open the GUI in a browser:**
   ```
   Enter the board's IP address in your browser.
   Locate the “SETUP” button in the top-right corner.  
   Click “SETUP”.
   ```

2. **Edit the Configuration:**

   The minimum required settings are:

   * Your callsign
   * Your DMR ID (if required, click the link below the DMR ID field to open the DMR database)
   * Under **Radio boards**, choose **Simplex** or **Duplex** for each board to match its hardware.
   * Set each board’s RX and TX frequencies. **Simplex** requires the same RX and TX frequency; **Duplex** supports separate frequencies.
   * Enter your Brandmeister password (as configured in SelfCare on the BM Dashboard)
   * Enter the Config Password (default: `setuppassword`). To change it, run `sudoedit /etc/opendvm/config-password`. Installation migrates an existing password from `save_config.php`; updates preserve it.
   * Select a board for each mode you want to use, choose **Start on boot**, then click **Save & apply**.

   If you receive a green confirmation message, the configuration has been successfully stored.

Each mode's **Start on boot** checkbox controls both its gateway service and its
RF/network `Enable` flags in MMDVMHost. **Save & apply** saves the boot policy and
immediately starts or stops the selected modes. The page waits for the backend
to apply the settings and reports failures.

The **Start** / **Stop** buttons act immediately and leave the saved boot policy
unchanged. The next reboot restores the boot selection. With board assignments,
Start/Stop reloads the supervisor and changes only the affected board's MMDVMHost;
other modes sharing that board are briefly interrupted. Independent boards keep
running. Legacy single-host setups still restart the shared host. Gateway connection
status can take longer to settle. These controls use the Config Password entered on the page.

Run `sudo ./install_update.sh` once to install the mode controller, service hook
and permissions. Updates preserve boot policy and avoid starting stopped gateways.
On the first update, a mode already disabled in MMDVMHost stays disabled at boot.

NXDN is installed but disabled by default. In its settings card, enter your
**NXDN ID** (1–65535; separate from DMR ID), **RAN** (0–63, matching your radio)
and optional comma-separated **Static talkgroups**. Save before using the runtime
Start button. Empty static TGs allow selection by RF traffic. The dashboard shows
the currently selected NXDN TG, every configured static TG, RF/NET calls and BER
when the host reports it. Modes assigned to the same board share its modem; concurrent RF protocols cannot
be received at the same time.

An existing installation gains NXDNGateway by running `sudo ./install_update.sh`.
Its source is built from [roolrz/NXDNClients](https://github.com/roolrz/NXDNClients)
at the revision tested by CI, recorded in `scripts/install_nxdn.sh`. Existing
`/etc/nxdngateway` settings are preserved. The current gateway requires a local
MQTT broker, so the installer includes and enables Mosquitto. Gateway diagnostics
are in `/var/log/mmdvm/NXDNGateway.log` and `journalctl -u nxdngateway`.

The DMR card also includes a **BrandMeister static talkgroups** manager. Create
an API v2 key in [BM Profile Settings → API Keys](https://brandmeister.network/?page=profile),
enter the OpenDVM Config Password, paste the key and click **Save key & load**.
The key stays on the device; the settings API returns only whether a key is
configured. A blank key field preserves it. **Remove saved key** deletes the
device's copy without revoking the key at BM. Station saves and backend restarts
preserve the key.

Click **Load from BM**, edit the comma-separated static TG lists for **TS1** and
**TS2**, then click **Apply static TGs to BM**. Simplex hotspots use a single
**BM slot 0** list. Emptying a list removes its static bindings. The manager uses
the saved `DMR Network 1` ID in `/etc/dmrgateway`, including any ESSID; the target
device ID is shown above the lists. Save station ID/duplex changes before loading
BM settings. Unrelated station saves preserve explicitly configured network IDs.

These changes go directly to BM and do not restart MMDVMHost or DMRGateway.

### Bind modes to USB hotspot boards

In **Settings**, **Station** contains identity information only. Configure RX/TX
frequencies and simplex/duplex once per board under **Radio boards**, then select
those boards in D-Star, DMR, YSF and NXDN. Serial speed and calibration offsets are
under each board's **Device details & calibration**. A non-USB serial board has
its hardware fields in this same section.

Enter **Config Password** and click the single **Save & apply** button to save board
changes and station settings. Board-save failures retain edits and stop the station
submission. If the subsequent station save fails, the page explicitly reports that
board settings have already been saved. **Rescan boards** preserves unsaved edits.
Mode Start is unavailable while board edits are pending; Stop remains available.
Legacy USB alias assignments are displayed as the concrete board they currently
resolve to; review them before saving, which pins them to the stable device paths.

- Different boards run independent MMDVMHost processes and can operate simultaneously.
  Modes assigned to the same board share one process and take turns as before.
- **Current hotspot** uses the serial device in `/etc/MMDVMHost.ini`. Leaving all modes
  on this option uses one host. If that device aliases a USB board with saved settings,
  that board profile also applies to modes using **Current hotspot**.
- Prefer `/dev/serial/by-id` identities. A board without a unique serial identity can
  use `/dev/serial/by-path`; keep it plugged into the same USB socket/hub port. USB port
  renumbering does not change saved assignments. Disconnected saved boards stay visible.
- The **Radio boards** section sets RX/TX frequencies in Hz, simplex/duplex,
  baud rate and RX/TX offsets per detected or saved board. Simplex boards require identical RX/TX
  frequencies. Other modem calibration values inherit `/etc/MMDVMHost.ini`.
  Each board has its own card, even without any mode assigned. Settings remain saved
  when modes are reassigned or the board is unplugged. Modes only select which board
  to use; all modes on a board share its frequencies.
  Frequencies are also applied to each mode's gateway; BM slot selection follows the
  board bound to DMR. The dashboard shows the frequencies for the boards assigned to the modes.
- Applying bindings or station settings restarts the modem service once and
  briefly interrupts its boards. Mode Start/Stop only changes affected boards.
  Unplugging or a host process exiting only affects that
  board; the supervisor retries it without restarting the other boards.
  The service journal records board paths, child PIDs, exits and device availability.

Bindings and profiles persist in `/etc/opendvm-radios.json`. The existing
`mmdvmhost.service` supervises the hosts, with generated configurations under
`/run/opendvm-host/` and separate `MMDVM-board-<id>-YYYY-MM-DD.log` files in
`/var/log/mmdvm`. The status monitor discovers these logs and tracks each board's calls
independently. With USB bindings enabled, generated hosts enable only the four modes
managed by OpenDVM; auxiliary displays and remote-control listeners are disabled to
avoid sharing their resources across processes.

Run `install_update.sh` when upgrading an existing installation: it installs the host
supervisor and systemd override as well as the web controls. Board configuration is
preserved across updates. New board cards start with values from the existing host
configuration; review each board’s values and assignments before Save & apply.
The manager applies only differences, adds before removing, and reads the lists
back before reporting success. If BM rejects a request, cannot confirm the result
or has changed since the lists were loaded, refresh before retrying. A failed
multi-group operation may have applied some earlier changes; BM has no atomic
multi-group replacement endpoint in this integration. The API key's account must
have permission to manage the selected device. Dynamic TGs remain separate on
the dashboard.

You can still manually edit the generated configuration files if needed — although this is rarely necessary. The only setting you might need to adjust is the MMDVM modem baud rate. By default, it’s set to 115200 Bd, which works for most MMDVM boards. However, some boards (for example, Repeater Builder boards) may require 460800 Bd — see the [Modem] section in /etc/MMDVMHost.ini.

D-Star announcements default to English (`language=0` in `/etc/ircddbgateway`).
Updates preserve existing configuration; on an existing installation, set this value
manually and restart `ircddbgateway.service` to switch its announcement language.

5. **Reboot:**
   ```bash
   sudo reboot
   ```
   After reboot, the system will be fully operational.

   Please allow some time for the reflectors to connect after the first reboot — this may take several minutes

---

## 🌐 Web Frontend

The web frontend displays all operational data in real time.  
Completely static – no PHP framework required, just a small `api.php` for JSON output.

### Features

- Live status: mode, callsign, duration, BER, RF/NET  
- Colored status tiles and country flags  
- Reflector status for D-Star, DMR, Fusion  
- “Last Heard” list with callsign, timestamp, duration  
- Activity chart (48h, RF/NET separated)  
- Bar statistics and 30-day heatmap  
- Responsive dark UI  
- Only external library: **Chart.js**

### Technology

- Pure Vanilla JavaScript  
- CSS grid layout  
- Updates every second via AJAX  
- Works on any webserver (nginx, Apache, lighttpd)

---

## CI

GitHub Actions runs on every push and pull request, and can also be started manually.
All checks run inside `debian:stable-slim`, following the latest Debian stable release.

- Install the application's dependencies using `base_packages` from `install.sh`.
- Check Bash/ShellCheck errors, PHP syntax and extensions, and JavaScript syntax (including inline scripts).
- Compile `mmdvm-status` and `DVconfig` using the production build commands.
- Start MariaDB, a local MQTT broker, both backends and the PHP HTTP server; verify D-Star/YSF/DMR log ingestion, MQTT ingestion, dashboard APIs, authentication rejection and configuration submission.
- Replay D-Star reflector announcements and overlapping DMR calls, and verify that Chromium renders both timeslots and their talkgroups.

The smoke test changes `/etc` and starts services only inside the disposable CI container.
It does not exercise MMDVM hardware, systemd service restarts, external radio networks,
or compilation of the four separately maintained gateway repositories.

## 🧱 System Setup & Maintenance

- Database runs via Unix socket
- Installation scripts automatically create users and permissions

### Update YSF reflectors

Installation and updates download the current Pi-Star host list from
`https://www.pistar.uk/downloads/YSF_Hosts.txt` to `/usr/local/etc/YSFHosts.txt`.
To refresh it separately from an application update, run:

```bash
sudo ./update_ysf_hosts.sh
# Or use the installed command from any directory:
sudo /usr/local/sbin/opendvm-update-ysf-hosts
```

The updater requires more than 500 valid host records before replacing the file.
It saves the previous file as `YSFHosts.txt.bak`, replaces the host list atomically,
and restarts `ysfgateway.service` when the content changes. Downloads that fail or
contain invalid data preserve the current file and backup. If reflector `46004`
is present, its record is printed. Use `--no-restart` when another process manages
the restart; the installation scripts use this option and restart services later.

### Update D-Star reflectors and reduce startup DNS delays

ircDDBGateway loads the enabled host tables before connecting to its startup
reflector. Its bundled lists contain old hostnames, resolved synchronously one
by one; obsolete domains can delay the first connection by minutes.

Installation and updates now refresh `DExtra_Hosts.txt`, `DPlus_Hosts.txt` and
`DCS_Hosts.txt` in `/usr/share/ircddbgateway/` from Pi-Star. Run a standalone
refresh without upgrading system packages or restarting the other gateways:

```bash
sudo ./update_dstar_hosts.sh
# Or, after installation/update, from any directory:
sudo /usr/local/sbin/opendvm-update-dstar-hosts
```

All three downloads must pass validation before any active file is replaced.
Each table must contain at least 100 records for its protocol, and every server
address must be a numeric IPv4 address to avoid per-record DNS lookups at gateway
startup. D-Plus repeater calls, alphanumeric reflector IDs, XLX aliases and `L`
lock markers are supported. Changed files are backed up as `*.bak` and replaced
atomically; the gateway restarts once after the refresh. `--no-restart` defers
that restart, and `--dest-dir` supports a different compiled data directory.

The updater preserves existing protocol settings. Per-user host files in the
`mmdvm` account's home directory are also loaded by the gateway; the script
reports those files for inspection and preserves their custom entries.

### Update NXDN reflectors and IDs

Fresh installation and `install_update.sh` automatically refresh NXDN's server
list and RadioID lookup table. To update them separately:

```bash
sudo /usr/local/sbin/opendvm-update-nxdn-hosts
# Or, from the checkout:
sudo python3 ./update_nxdn_hosts.py
```

The updater downloads Pi-Star's `NXDN_Hosts.txt` and RadioID's NXDN CSV. It
validates both before replacing `/usr/local/etc/NXDNHosts.json` and `NXDN.csv`,
with at least 50 distinct valid reflectors and 100 valid IDs. Previous contents
are backed up as `.bak`; download/validation failures retain the active files.
Unchanged lists do not restart the gateway, and changed lists only restart a
gateway that is already running. `--no-restart` defers that restart.

DNS happens during the update, with 16 parallel workers and a three-second limit
per hostname. The gateway reads numeric IPv4/IPv6 addresses, avoiding sequential
DNS waits during startup. Failed lookups reuse that TG's previous numeric entry
when available; otherwise unresolved entries are omitted. If fewer than 50 usable
reflectors remain or more than 20% have no usable address, the update is rejected.

Private entries in `/usr/local/etc/NXDNHosts.private` are preserved. Use
`TG numeric-IP port` per line; private hostnames can still delay gateway startup
and trigger a warning. The updater supports `--hosts-url`, `--ids-url` and
`--dest-dir` for alternate feeds or testing.

---

## 🎯 Credits & License

- Jonathan Naylor G4KLX, for his outstanding DV implementations that form the foundation of this project  
- This software is licensed under **GPL v2** and is primarily intended for amateur radio and educational use. This project includes components from G4KLX licensed under GPL v2.
Therefore, the combined work remains under GPL v2.
