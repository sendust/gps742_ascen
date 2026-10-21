# gps742_ascen

Command-line tool for the **Ascen GPS742** GPS data logger (MTK chipset).
It talks to the logger over USB serial using NMEA/PMTK commands to read and
change logger settings, download the log memory, and convert it to CSV and GPX.

<img src="gps742.png" alt="Ascen GPS742" width="300">

## Features

- Serial port list at startup: pick the logger, then enter the top menu
- Number-driven menus: type a number and press Enter, `0` goes back (exits at the top menu)
- Device info, live position status, raw NMEA view, hot/warm/cold restart
- Full log download with per-block checksum check and retry
- Converts the raw dump to CSV and GPX (one track per local date, plus one GPX file per date)
- Logger settings: start/stop logging, time/distance/speed interval, overwrite or stop when full, log fields
- Restore factory defaults: logger settings plus a full cold restart for the GPS receiver settings
- GPS receiver settings: NMEA output sentences, fix rate, SBAS, DGPS
- GPS week rollover correction (see [Device notes](#device-notes))

## Requirements

- Python 3.8+
- [pyserial](https://pypi.org/project/pyserial/)

```bash
pip install -r requirements.txt     # or: sudo apt install python3-serial
```

On Linux the logger shows up as `/dev/ttyACM0` (USB `0e8d:3329`, `cdc_acm` driver).
Your user must be in the `dialout` group to open it:

```bash
sudo usermod -aG dialout $USER     # then log out and back in
```

## Usage

```bash
python3 gps742.py                       # choose the serial port from a list
python3 gps742.py -p /dev/ttyACM0       # skip the port list
python3 gps742.py -p /dev/ttyACM0 -b 115200
python3 gps742.py --convert logs/gps742_YYYYmmdd_HHMMSS.bin   # convert only, no device
```

Downloaded files are written to `logs/` next to the script:

| File | Content |
|---|---|
| `gps742_<date>_<time>.bin` | raw log memory dump |
| `gps742_<date>_<time>.json` | logger settings at download time |
| `gps742_<date>_<time>.csv` | all records |
| `gps742_<date>_<time>.gpx` | all valid points, one `<trk>` per local date |
| `gps742_<date>_<time>_<YYYY-MM-DD>.gpx` | one file per local date |

## Bluetooth

The GPS742 also offers a Bluetooth serial port (SPP). It works for settings and the live view,
but downloads are slower (about 3 KB/s, roughly 5.5 minutes for 1 MB, versus about 30 s over USB)
and the logger's Bluetooth module can stall in the middle of a transfer.
**Use USB for downloads when you can.**

Linux:

```bash
sudo rfcomm bind 0 <logger MAC address>      # creates /dev/rfcomm0 (group dialout)
python3 gps742.py                            # pick "/dev/rfcomm0  Bluetooth serial ..."
```

Windows: pair the logger in Bluetooth settings. Windows then creates two COM ports for it,
an *outgoing* one (used to connect to the logger) and an *incoming* one. The port list shows
the outgoing port with the logger's address and marks the incoming port as not usable:

```
  2. COM5           Standard Serial over Bluetooth link (COM5)  <- Bluetooth 00:1C:88:13:04:F8
  4. COM6           Standard Serial over Bluetooth link (COM6)  (Bluetooth incoming port - cannot connect)
```

Both are recognised from the `BTHENUM\...` device instance ID; the outgoing port carries the
remote address, the incoming port carries `000000000000`.

On a Bluetooth port the tool switches to a slower, safer mode:

- waits up to 15 s for the link and retries the connection up to 3 times
- 4 KB blocks with a 0.3 s pause between requests, up to 10 retries per block;
  after 3 failures on a block the block size is halved (down to 128 bytes)
- NMEA output is turned off during the download and restored afterwards
- when a block keeps failing, the data received so far is kept and you can
  resume, **reconnect and resume after power-cycling the logger**, or save a `_partial` dump

If a download was cut off while NMEA output was off, the next connection warns about it;
use *GPS receiver settings > Restore default NMEA output*.

ModemManager probes new serial ports with AT commands and can disturb the logger.
If you do not use a modem, exclude rfcomm ports:

```bash
echo 'KERNEL=="rfcomm*", ENV{ID_MM_DEVICE_IGNORE}="1"' | sudo tee /etc/udev/rules.d/99-mm-ignore-rfcomm.rules
sudo udevadm control --reload
```

## Menu structure

```
Select serial port
├── 1..N  serial ports (MTK GPS is marked)
├── Rescan
├── Continue without device (convert saved .bin files)
└── 0 Exit

GPS742 logger manager
├── 1 Device info / live view
│   ├── 1 Device info
│   ├── 2 Live position status
│   ├── 3 Raw NMEA view
│   ├── 4 Hot restart
│   ├── 5 Warm restart
│   └── 6 Cold restart
├── 2 Log download
│   ├── 1 Download full log (save .bin/.csv/.gpx)
│   ├── 2 Convert a saved .bin file again
│   └── 3 Erase log memory
├── 3 Logger settings
│   ├── 1 Start / stop logging
│   ├── 2 Set time interval
│   ├── 3 Set distance interval
│   ├── 4 Set speed interval
│   ├── 5 When memory is full (overwrite / stop)
│   ├── 6 Select log fields
│   └── 7 Restore factory defaults
├── 4 GPS receiver settings
│   ├── 1 NMEA output sentences
│   ├── 2 Restore default NMEA output
│   ├── 3 Fix rate
│   ├── 4 SBAS on / off
│   └── 5 DGPS mode
├── 5 Connection / program settings
│   ├── 1 Change port / baud rate
│   ├── 2 Reconnect
│   ├── 3 GPS week rollover fix (auto / off)
│   ├── 4 Download block size (USB)
│   ├── 5 Download block size (Bluetooth)
│   └── 6 Output folder
└── 0 Exit
```

Settings screens show the current values read from the device each time they are opened.
Erasing the log memory asks you to type `erase`; restarts and restoring factory defaults ask for `yes`.
In prompts that ask for a value, an empty Enter cancels.

## Screenshots

Port selection, top menu and logger settings over Bluetooth (`/dev/rfcomm0`):

![CLI menu](docs/cli_menu.png)

Log download over Bluetooth (4 KB blocks, NMEA output off during the transfer):

![CLI download](docs/cli_download.png)

## Protocol summary

All commands are `$<body>*<XOR checksum>\r\n`.

| Command | Purpose |
|---|---|
| `PMTK605` → `PMTK705` | firmware version |
| `PMTK182,2,<n>` → `PMTK182,3,<n>,<v>` | read logger parameter (2 format, 3 time, 4 distance, 5 speed, 6 method, 7 status, 8 next write address, 9 flash ID, 10 record count) |
| `PMTK182,1,<n>,<v>` | set logger parameter |
| `PMTK182,4` / `PMTK182,5` | start / stop logging |
| `PMTK182,6,1` | erase log memory |
| `PMTK182,7,<addr>,<len>` → `PMTK182,8,<addr>,<hex>` | read log memory |
| `PMTK314` / `PMTK414` | set / query NMEA output |
| `PMTK220` / `PMTK400` | set / query fix rate |
| `PMTK313` / `PMTK413` | set / query SBAS |
| `PMTK301` / `PMTK401` | set / query DGPS mode |
| `PMTK314,-1` | restore default NMEA output |
| `PMTK000` | test command (link check) |
| `PMTK101` / `102` / `103` | hot / warm / cold restart |
| `PMTK104` | full cold restart, resets GPS receiver settings (not logger settings) |

Commands are acknowledged with `$PMTK001,<cmd>,[<sub>,]<flag>` (3 = success).
Time, distance and speed intervals are stored in units of 0.1 s, 0.1 m and 0.1 km/h.

## Device notes

Observed on a GPS742 with firmware `AXN_1.30-B_1.3_C01,000D,TSI_747PRO,1.0`:

- **GPS week rollover.** The receiver reports dates 1024 weeks (about 19.6 years) in the past,
  e.g. 2007-03-06 for 2026-10-20. Times before 2012 are shifted forward by 1024 weeks.
  This can be turned off in *Connection / program settings*.
- **Every other sector is used.** The log memory is made of 64 KB sectors, and every second
  sector is blank (all `0xFF`). The parser skips blank sectors.
- **Corrupted sector header.** Sector 0 may carry a wrong format value in its header.
  When a sector cannot be decoded with its header format, the current device format is tried.
- **Large reads can hang the firmware.** Interrupting a 64 KB read left the logger unresponsive
  until the USB cable was reconnected. The tool reads 2 KB blocks, waits for each acknowledgement,
  and drains pending output before retrying.
- **Bluetooth stalls.** The Bluetooth module stops sending when commands arrive back-to-back:
  with no pause between download requests it stalled within a few requests, with 0.1 s after
  about 110 KB. The GPS chip keeps answering over USB, and only a power cycle of the logger
  brings the Bluetooth link back. With a 0.3 s pause a full download succeeded.
- **Bluetooth throughput.** Each request costs about 0.1 s plus about 0.22 s per KB (the dump is
  sent as hex text), so the link tops out near 4.5 KB/s. Measured with a 0.3 s pause:
  512 B blocks 1.0 KB/s, 1 KB 1.6 KB/s, 2 KB 2.35 KB/s, 4 KB 3.1 KB/s.
- **NMEA output settings survive a power cycle.** A download that is cut off while NMEA output
  is off leaves it off.
- **Factory defaults.** `PMTK104` resets GPS receiver settings (e.g. DGPS mode back to WAAS/SBAS)
  but leaves the logger settings unchanged. *Restore factory defaults* therefore also writes the
  logger values from `FACTORY_DEFAULTS` in `gps742.py`: fields `0x0002003F` (UTC, valid, lat, lon,
  height, speed, RCR), 1 s, 2 m, speed disabled, stop logging when full. Edit that table if your
  unit ships with different values.
- Old tracks stay in memory until it is erased, so a download can contain several trips.
  Use the per-date GPX files when importing into a map app.
