#!/usr/bin/env python3
"""GPS742 (MTK chipset GPS logger) management CLI.

Reads/changes logger settings and downloads the log memory over NMEA/PMTK.
The raw dump (.bin) is converted to CSV and GPX.

Menu navigation: type a number and press Enter to select, 0 goes back
(exits at the top menu).
"""
import argparse
import glob
import json
import os
import re
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import reduce

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required: pip install pyserial  (or: sudo apt install python3-serial)")

DEFAULT_BAUD = 115200
SECTOR_SIZE = 0x10000
SECTOR_HEADER = 0x200
GPS_WEEK_ROLLOVER = 1024 * 7 * 86400
ROLLOVER_THRESHOLD = datetime(2012, 1, 1, tzinfo=timezone.utc).timestamp()
MTK_VID = 0x0E8D

# Logger values written by "Restore factory defaults" (PMTK182,1,<param>,<value>).
# PMTK104 does not reset these on the GPS742, so they are written one by one.
# key: (param, value, label)
FACTORY_DEFAULTS = {
    "format": (2, 0x0002003F, "Fields"),     # UTC, valid, lat, lon, height, speed, RCR
    "period": (3, 10, "Time interval"),      # 0.1 s units -> 1 s
    "distance": (4, 20, "Distance"),         # 0.1 m units -> 2 m
    "speed": (5, 0, "Speed"),                # disabled
    "mode": (6, 2, "When full"),             # 1 overwrite, 2 stop logging
}

CONFIG = {
    "rollover": "auto",      # auto | off
    "chunk": 0x800,          # download request size over USB (bytes)
    "bt_chunk": 0x1000,      # download request size over Bluetooth (bytes)
    "bt_retries": 1,         # quick retries before asking to power-cycle and reconnect
    "bt_delay": 0.3,         # pause between Bluetooth requests (s); back-to-back requests
                             # stall the logger's Bluetooth module, so keep this pause
    "outdir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
}
MIN_CHUNK = 0x80             # smallest download block size that can be set


# ---------------------------------------------------------------- NMEA/PMTK

def nmea_xor(body):
    return reduce(lambda a, c: a ^ ord(c), body, 0)


def make_cmd(body):
    return f"${body}*{nmea_xor(body):02X}\r\n".encode()


def check_line(line):
    """Return the field list if the checksum is valid, otherwise None."""
    if not line.startswith("$") or "*" not in line:
        return None
    body, cs = line[1:].rsplit("*", 1)
    try:
        if nmea_xor(body) != int(cs[:2], 16):
            return None
    except ValueError:
        return None
    return body.split(",")


class DeviceError(Exception):
    pass


ACK_TEXT = {0: "invalid command", 1: "unsupported command", 2: "failed", 3: "success"}


def bt_port_info(p):
    r"""(is_bluetooth, MAC address or None, is Windows incoming port) for a list_ports entry.

    Windows creates two COM ports per paired SPP device. Their hwid is the device instance ID:
      outgoing  BTHENUM\{00001101-...}_LOCALMFG&000F\7&2A6B8F2C&0&001C881304F8_C00000000
      incoming  BTHENUM\{00001101-...}_LOCALMFG&0000\7&2A6B8F2C&0&000000000000_00000000
    Only the outgoing port (it carries the remote address) can connect to the logger.
    """
    hwid = (p.hwid or "").upper()
    if hwid.startswith("BTHENUM"):
        m = re.search(r"&([0-9A-F]{12})_", hwid)
        if not m:
            return True, None, False
        mac = m.group(1)
        if mac == "0" * 12:
            return True, None, True
        return True, ":".join(mac[i:i + 2] for i in range(0, 12, 2)), False
    bt = "rfcomm" in p.device or "bluetooth" in f"{p.device} {p.description}".lower()
    return bt, None, False


def is_bluetooth(port):
    """Linux /dev/rfcomm*, Windows COM ports enumerated by BTHENUM, macOS Bluetooth ports."""
    if "rfcomm" in port or "bluetooth" in port.lower():
        return True
    for p in list_ports.comports():
        if p.device == port:
            return bt_port_info(p)[0]
    return False


class Mtk:
    def __init__(self, port, baud):
        self.port, self.baud = port, baud
        self.bluetooth = is_bluetooth(port)
        # A Bluetooth SPP link is set up when the port is opened and can stall for seconds
        self.tscale = 2 if self.bluetooth else 1
        try:
            self.ser = serial.Serial(port, baud, timeout=0.2,
                                     write_timeout=10 if self.bluetooth else 2)
        except serial.SerialException as e:
            raise DeviceError(f"cannot open port: {e}")
        self.buf = b""
        if self.bluetooth:
            self.wait_link()

    def close(self):
        self.ser.close()
        if self.bluetooth:
            time.sleep(1)    # let the RFCOMM link close before the port is opened again

    def wait_link(self, timeout=15):
        """Wait for the first NMEA sentence so commands are not sent before the link is up."""
        for line in self.lines(timeout):
            if check_line(line):
                return
        # NMEA output may be off (e.g. after an interrupted download): try a test command
        try:
            self.write("PMTK000")
            for line in self.lines(5):
                if line.startswith("$PMTK001,0,"):
                    return
        except DeviceError:
            pass
        self.ser.close()
        raise DeviceError("no data over Bluetooth. Power-cycle the logger and make sure no other "
                          "PC or phone is connected to it.")

    def stalled_hint(self):
        if self.bluetooth:
            return "Bluetooth link stalled. Power-cycle the logger or reconnect Bluetooth."
        return "Reconnect the USB cable."

    def write(self, body):
        try:
            self.ser.write(make_cmd(body))
        except serial.SerialTimeoutException:
            raise DeviceError(f"device does not accept commands (write timeout). {self.stalled_hint()}")
        except serial.SerialException as e:
            raise DeviceError(f"write error: {e}")

    def readline(self, deadline):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i], self.buf[i + 1:]
                return line.decode("ascii", "replace").strip()
            if time.time() >= deadline:
                return None
            try:
                self.buf += self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                raise DeviceError(f"read error: {e}")

    def lines(self, timeout):
        deadline = time.time() + timeout
        while True:
            line = self.readline(deadline)
            if line is None:
                return
            yield line

    def flush_input(self):
        self.ser.reset_input_buffer()
        self.buf = b""

    def query(self, body, prefix, timeout=2.0, retries=2):
        """Send a command and return the fields of the first reply starting with prefix."""
        for _ in range(retries + 1):
            self.write(body)
            for line in self.lines(timeout * self.tscale):
                if line.startswith("$" + prefix):
                    f = check_line(line)
                    if f:
                        return f
        raise DeviceError(f"no response: {body}")

    def command(self, body, timeout=3.0, retries=1):
        """Send a command and return the flag (0-3) of its $PMTK001 acknowledgement."""
        parts = body.split(",")
        num = parts[0][4:]
        sub = parts[1] if num == "182" else None
        for _ in range(retries + 1):
            self.write(body)
            for line in self.lines(timeout * self.tscale):
                if not line.startswith("$PMTK001,"):
                    continue
                f = check_line(line)
                if not f or f[1] != num:
                    continue
                if sub is not None:
                    if len(f) < 4 or f[2] != sub:
                        continue
                    return int(f[3])
                return int(f[2])
        raise DeviceError(f"no acknowledgement: {body}")

    # ---- logger (PMTK182)

    def log_get(self, param):
        return self.query(f"PMTK182,2,{param}", f"PMTK182,3,{param},")[3]

    def log_set(self, param, value):
        return self.command(f"PMTK182,1,{param},{value}")

    def log_status(self):
        g = self.log_get
        return {
            "format": int(g(2), 16),
            "period": int(g(3)),
            "distance": int(g(4)),
            "speed": int(g(5)),
            "mode": int(g(6)),
            "status": int(g(7)),
            "next_addr": int(g(8), 16),
            "count": int(g(10), 16),
        }

    def firmware(self):
        return ",".join(self.query("PMTK605", "PMTK705,")[1:])

    def flash_id(self):
        return int(self.log_get(9), 16)

    def read_memory(self, addr, length, timeout=None):
        """Read one memory block. Returns None on failure."""
        self.write(f"PMTK182,7,{addr:08X},{length:08X}")
        chunks = {}
        for line in self.lines(10 * self.tscale if timeout is None else timeout):
            if line.startswith("$PMTK182,8,"):
                f = check_line(line)
                if not f:
                    return None
                try:
                    chunks[int(f[2], 16)] = bytes.fromhex(f[3])
                except ValueError:
                    return None
            elif line.startswith("$PMTK001,182,7,"):
                f = check_line(line)
                if not f or f[3] != "3":
                    return None
                break
        else:
            return None
        if not chunks or min(chunks) != addr:
            return None
        data = b"".join(chunks[k] for k in sorted(chunks))
        return data if len(data) == length else None

    def drain(self, quiet=1.0, maxwait=20):
        """Wait until an in-progress memory dump stops streaming."""
        end = time.time() + maxwait
        last = time.time()
        while time.time() < end and time.time() - last < quiet:
            line = self.readline(time.time() + 0.2)
            if line and line.startswith("$PMTK182,8"):
                last = time.time()


# ---------------------------------------------------------------- log format

# (bit, key, label, size)
FIELDS = [
    (0, "utc", "UTC time", 4),
    (1, "valid", "Fix validity", 2),
    (2, "lat", "Latitude", 8),
    (3, "lon", "Longitude", 8),
    (4, "height", "Height", 4),
    (5, "speed", "Speed", 4),
    (6, "heading", "Heading", 4),
    (7, "dsta", "DGPS station ID", 2),
    (8, "dage", "DGPS data age", 4),
    (9, "pdop", "PDOP", 2),
    (10, "hdop", "HDOP", 2),
    (11, "vdop", "VDOP", 2),
    (12, "nsat", "Satellite count", 2),
    (13, "sid", "Satellite ID", 4),
    (14, "elevation", "Satellite elevation", 2),
    (15, "azimuth", "Satellite azimuth", 2),
    (16, "snr", "Satellite SNR", 2),
    (17, "rcr", "Record reason", 2),
    (18, "millisecond", "Millisecond", 2),
    (19, "distance", "Distance", 8),
]

VALID_TEXT = {1: "NoFix", 2: "SPS", 4: "DGPS", 8: "PPS", 0x10: "RTK", 0x20: "FRTK",
              0x40: "Estimated", 0x80: "Manual", 0x100: "Simulator"}
RCR_TEXT = {1: "T", 2: "S", 4: "D", 8: "B"}  # Time, Speed, Distance, Button

SPECIAL_TEXT = {2: "format change", 3: "time interval", 4: "distance interval",
                5: "speed interval", 6: "record method", 7: "logging start/stop"}


def format_names(fmt):
    return [label for bit, _, label, _ in FIELDS if fmt & (1 << bit)]


def parse_record(buf, pos, fmt):
    """Decode one record. Returns (record, next position) or None."""
    p = pos
    rec = {}
    n = len(buf)
    for bit, key, _, size in FIELDS:
        if not fmt & (1 << bit) or bit in (14, 15, 16):
            continue
        if bit == 13:
            if p + 4 > n:
                return None
            cnt = struct.unpack_from("<H", buf, p + 2)[0]
            if cnt == 0:
                p += 4
                continue
            if cnt > 32:
                return None
            sats = []
            for _ in range(cnt):
                if p + 4 > n:
                    return None
                sat = {"sid": buf[p], "inuse": buf[p + 1]}
                p += 4
                for b2, k2 in ((14, "elevation"), (15, "azimuth"), (16, "snr")):
                    if fmt & (1 << b2):
                        if p + 2 > n:
                            return None
                        sat[k2] = struct.unpack_from("<h" if b2 == 14 else "<H", buf, p)[0]
                        p += 2
                sats.append(sat)
            rec["sats"] = sats
            continue
        if p + size > n:
            return None
        if key == "utc":
            rec[key] = struct.unpack_from("<I", buf, p)[0]
        elif key in ("lat", "lon", "distance"):
            rec[key] = struct.unpack_from("<d", buf, p)[0]
        elif key in ("height", "speed", "heading", "dage"):
            rec[key] = struct.unpack_from("<f", buf, p)[0]
        elif key in ("pdop", "hdop", "vdop"):
            rec[key] = struct.unpack_from("<H", buf, p)[0] / 100
        elif key == "nsat":
            rec["nsat_inview"], rec["nsat_inuse"] = buf[p], buf[p + 1]
        else:
            rec[key] = struct.unpack_from("<H", buf, p)[0]
        p += size
    if p + 2 > n or buf[p] != 0x2A or buf[p + 1] != reduce(lambda a, b: a ^ b, buf[pos:p], 0):
        return None
    if "lat" in rec and not -90 <= rec["lat"] <= 90:
        return None
    if "lon" in rec and not -180 <= rec["lon"] <= 180:
        return None
    return rec, p + 2


def is_special(buf, p):
    return buf[p:p + 7] == b"\xaa" * 7 and buf[p + 12:p + 16] == b"\xbb" * 4


def parse_sector(data, start, end, fmt):
    """Decode the data area of one sector. Returns (records, specials, bad bytes, last fmt)."""
    records, specials, bad = [], [], 0
    p = start
    while p < end:
        if data[p:p + 16] == b"\xff" * 16 or (end - p < 16 and set(data[p:end]) <= {0xFF}):
            break
        if is_special(data, p):
            typ = data[p + 7]
            val = struct.unpack_from("<I", data, p + 8)[0]
            specials.append((p, typ, val))
            if typ == 2:
                fmt = val
            p += 16
            continue
        r = parse_record(data, p, fmt)
        if r is None:
            bad += 1
            p += 1
            continue
        records.append(r[0])
        p = r[1]
    return records, specials, bad, fmt


def parse_log(data, fallback_fmt=None):
    """Decode a whole dump. fallback_fmt is tried when the sector header format fails."""
    result = {"records": [], "specials": [], "bad": 0, "sectors": 0}
    fmt = None
    for s in range(0, len(data), SECTOR_SIZE):
        header = data[s:s + SECTOR_HEADER]
        if len(header) < 20:
            break
        if header[:6] == b"\xff" * 6:
            continue      # blank sector (this device uses every other sector)
        result["sectors"] += 1
        hdr_fmt = struct.unpack_from("<I", header, 2)[0]
        end = min(s + SECTOR_SIZE, len(data))
        candidates = [f for f in (fmt, hdr_fmt, fallback_fmt) if f]
        best = None
        for cand in dict.fromkeys(candidates):
            r = parse_sector(data, s + SECTOR_HEADER, end, cand)
            if best is None or len(r[0]) > len(best[0]):
                best = r
            if r[2] == 0 and r[0]:
                break
        records, specials, bad, fmt = best
        result["records"] += records
        result["specials"] += [(s + p, t, v) for p, t, v in specials]
        result["bad"] += bad
    return result


def fix_time(t):
    if CONFIG["rollover"] == "auto" and 0 < t < ROLLOVER_THRESHOLD:
        return t + GPS_WEEK_ROLLOVER
    return t


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_csv(path, records):
    cols = ["time", "valid", "lat", "lon", "height", "speed", "heading", "pdop", "hdop",
            "vdop", "nsat_inuse", "nsat_inview", "rcr", "distance"]
    present = [c for c in cols if c == "time" or any(c in r for r in records)]
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(present) + "\n")
        for r in records:
            row = []
            for c in present:
                if c == "time":
                    v = iso(fix_time(r["utc"]) + r.get("millisecond", 0) / 1000) if "utc" in r else ""
                elif c == "valid":
                    v = VALID_TEXT.get(r.get("valid"), r.get("valid", ""))
                elif c == "rcr":
                    v = "".join(t for b, t in RCR_TEXT.items() if r.get("rcr", 0) & b)
                elif c in ("lat", "lon"):
                    v = f"{r[c]:.7f}" if c in r else ""
                elif isinstance(r.get(c), float):
                    v = f"{r[c]:.2f}"
                else:
                    v = r.get(c, "")
                row.append(str(v))
            f.write(",".join(row) + "\n")


def local_day(t):
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d")


def split_tracks(records):
    """Split GPX points into one track per local date, dropping points whose time goes backwards."""
    tracks = {}
    last = None
    for r in records:
        if "lat" not in r or "lon" not in r or r.get("valid", 2) == 1:
            continue
        t = fix_time(r["utc"]) if "utc" in r else None
        if t is not None:
            if last is not None and t <= last:
                continue
            last = t
        tracks.setdefault(local_day(t) if t is not None else "unknown", []).append((t, r))
    return tracks


def write_gpx(path, tracks, gap=300):
    times = [t for pts in tracks.values() for t, _ in pts if t is not None]
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<gpx version="1.1" creator="gps742.py" xmlns="http://www.topografix.com/GPX/1/1">\n')
        if times:
            f.write(f"<metadata><time>{iso(min(times))}</time></metadata>\n")
        for day, pts in tracks.items():
            ts = [t for t, _ in pts if t is not None]
            name = day
            if ts:
                fmt_hm = lambda x: datetime.fromtimestamp(x).astimezone().strftime("%H:%M")
                name = f"{day} {fmt_hm(ts[0])}~{fmt_hm(ts[-1])}"
            f.write(f"<trk><name>{name}</name>\n<trkseg>\n")
            prev = None
            for t, r in pts:
                if prev is not None and t is not None and t - prev > gap:
                    f.write("</trkseg>\n<trkseg>\n")
                prev = t
                f.write(f'<trkpt lat="{r["lat"]:.7f}" lon="{r["lon"]:.7f}">')
                if "height" in r:
                    f.write(f"<ele>{r['height']:.1f}</ele>")
                if t is not None:
                    f.write(f"<time>{iso(t)}</time>")
                if "hdop" in r:
                    f.write(f"<hdop>{r['hdop']:.2f}</hdop>")
                f.write("</trkpt>\n")
            f.write("</trkseg>\n</trk>\n")
        f.write("</gpx>\n")
    return sum(len(p) for p in tracks.values())


def convert(bin_path, fallback_fmt=None):
    with open(bin_path, "rb") as f:
        data = f.read()
    meta_path = os.path.splitext(bin_path)[0] + ".json"
    if fallback_fmt is None and os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            fallback_fmt = json.load(f).get("format")
    res = parse_log(data, fallback_fmt)
    recs = res["records"]
    base = os.path.splitext(bin_path)[0]
    write_csv(base + ".csv", recs)
    tracks = split_tracks(recs)
    npts = write_gpx(base + ".gpx", tracks)
    print(f"  {res['sectors']} sectors, {len(recs)} records ({npts} valid GPX points), "
          f"{len(res['specials'])} setting entries, {res['bad']} bad bytes")
    times = [r["utc"] for r in recs if "utc" in r]
    if times:
        if CONFIG["rollover"] == "auto" and any(t < ROLLOVER_THRESHOLD for t in times):
            print("  * GPS week rollover corrected: added 1024 weeks to times before 2012.")
        print(f"  Period: {iso(fix_time(min(times)))} ~ {iso(fix_time(max(times)))}")
    print(f"  Saved: {base}.csv")
    print(f"         {base}.gpx  (all, {len(tracks)} daily tracks)")
    for day, pts in tracks.items():
        write_gpx(f"{base}_{day}.gpx", {day: pts})
        print(f"         {base}_{day}.gpx  ({len(pts)} points)")
    return res


# ---------------------------------------------------------------- UI helpers

def pause():
    try:
        input("\n(Press Enter to return to the menu)")
    except EOFError:
        pass


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_number(prompt, current, lo, hi, unit=""):
    """Empty input cancels (returns None)."""
    s = ask(f"{prompt} [current {current}{unit}, range {lo}~{hi}{unit}, Enter=cancel]: ")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        print("  Please enter a number.")
        return None
    if not lo <= v <= hi:
        print("  Out of range.")
        return None
    return v


def confirm(prompt, word="yes"):
    return ask(f"{prompt} Type '{word}' to continue: ") == word


def report_ack(flag):
    print(f"  Result: {ACK_TEXT.get(flag, flag)}")


class Menu:
    def __init__(self, title, items, header=None, top=False):
        self.title, self.items, self.header, self.top = title, items, header, top

    def run(self):
        while True:
            print("\n" + "=" * 50)
            print(f" {self.title}")
            print("=" * 50)
            if self.header:
                try:
                    self.header()
                except DeviceError as e:
                    print(f"  [Error] {e}")
                print("-" * 50)
            for i, (label, _) in enumerate(self.items, 1):
                print(f" {i:2d}. {label() if callable(label) else label}")
            print(f"  0. {'Exit' if self.top else 'Back'}")
            choice = ask("Select> ")
            if choice == "0":
                return
            if not choice.isdigit() or not 1 <= int(choice) <= len(self.items):
                if choice:
                    print("  Invalid selection.")
                continue
            target = self.items[int(choice) - 1][1]
            if isinstance(target, Menu):
                target.run()
                continue
            try:
                if target() == "nopause":
                    continue
            except DeviceError as e:
                print(f"\n  [Error] {e}")
            except KeyboardInterrupt:
                print("\n  Interrupted.")
            pause()


def rfcomm_addresses():
    """Map /dev/rfcommN to its bound Bluetooth address using the `rfcomm` tool, if present."""
    try:
        out = subprocess.run(["rfcomm"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    addrs = {}
    for line in out.splitlines():
        name, _, rest = line.partition(":")
        if name.startswith("rfcomm") and rest.split():
            addrs["/dev/" + name] = rest.split()[0]
    return addrs


def serial_ports():
    """Ports with real hardware behind them (skips placeholder /dev/ttyS* entries).
    Bluetooth /dev/rfcomm* ports have no hardware info but are kept."""
    ports = [p for p in list_ports.comports()
             if (p.hwid and p.hwid != "n/a") or "rfcomm" in p.device]
    addrs = rfcomm_addresses()
    for p in ports:
        bt, mac, incoming = bt_port_info(p)
        p.tag = ""
        if "rfcomm" in p.device:
            p.description = f"Bluetooth serial {addrs.get(p.device, '')}".rstrip()
        elif incoming:
            p.tag = "  (Bluetooth incoming port - cannot connect)"
        elif mac:
            p.tag = f"  <- Bluetooth {mac}"
        if p.vid == MTK_VID:
            p.tag = "  <- MTK GPS"
        # order: MTK USB, Bluetooth, other ports, Bluetooth incoming ports
        p.rank = 0 if p.vid == MTK_VID else 1 if bt and not incoming else 3 if incoming else 2
    return sorted(ports, key=lambda p: (p.rank, p.device))


def select_port(current=None):
    """Show the serial port list and return the chosen device, "" for no device, None to quit."""
    while True:
        ports = serial_ports()
        print("\n" + "=" * 50)
        print(" Select serial port")
        print("=" * 50)
        for i, p in enumerate(ports, 1):
            mark = " *" if p.device == current else ""
            print(f" {i:2d}. {p.device:<14} {p.description}{p.tag}{mark}")
        if not ports:
            print("  (no serial ports found - connect the logger and rescan)")
        n = len(ports)
        print(f" {n + 1:2d}. Rescan")
        print(f" {n + 2:2d}. Continue without device (convert saved .bin files)")
        print("  0. Exit")
        s = ask("Select> ")
        if s == "0":
            return None
        if not s.isdigit():
            continue
        k = int(s)
        if 1 <= k <= n:
            p = ports[k - 1]
            if p.rank == 3 and not confirm(
                    "  This is the Bluetooth incoming port; the logger connects through the\n"
                    "  outgoing port (the one showing the Bluetooth address). Use it anyway?"):
                continue
            return p.device
        if k == n + 2:
            return ""


# ---------------------------------------------------------------- app

class App:
    def __init__(self, port, baud):
        self.dev = None
        self.port, self.baud = port, baud
        self.fw = None
        if port:
            self.connect()

    # ---- connection

    def connect(self):
        if self.dev:
            self.dev.close()
            self.dev = None
        self.fw = None
        if not self.port:
            return
        bt = is_bluetooth(self.port)
        if bt:
            print(f"  Connecting to {self.port} over Bluetooth (up to 15 s)...")
        try:
            # The first Bluetooth connection after the logger powers on can drop right away
            for attempt in range(3 if bt else 1):
                try:
                    self.dev = Mtk(self.port, self.baud)
                    break
                except DeviceError as e:
                    if attempt == (2 if bt else 0):
                        raise
                    print(f"  {e} - retrying")
                    time.sleep(3)
            self.fw = self.dev.firmware()
            print(f"  Connected: {self.port} ({self.baud} bps) / {self.fw}")
            if not any(int(x) for x in self.dev.query("PMTK414", "PMTK514,")[1:]):
                print("  * NMEA output is off (an interrupted Bluetooth download can leave it off).")
                print("    Turn it on in 'GPS receiver settings > Restore default NMEA output'.")
        except DeviceError as e:
            print(f"  Connection failed ({self.port}): {e}")

    def need(self):
        if not self.dev:
            raise DeviceError("no device connected. Use 'Connection / program settings'.")
        return self.dev

    def top_header(self):
        state = "not connected"
        if self.dev:
            link = "Bluetooth" if self.dev.bluetooth else "USB/serial"
            state = f"{self.port} ({self.baud} bps, {link})"
        print(f"  Port    : {state}")
        if self.fw:
            print(f"  Firmware: {self.fw}")

    def change_port(self):
        p = select_port(self.port)
        if p is None:
            return "nopause"
        self.port = p
        b = ask(f"  Baud rate [current {self.baud}, Enter=keep]: ")
        if b.isdigit():
            self.baud = int(b)
        self.connect()

    def reconnect(self):
        self.connect()

    # ---- device info / live view

    def show_info(self):
        dev = self.need()
        print(f"  Firmware : {dev.firmware()}")
        fid = dev.flash_id()
        cap = (fid >> 8) & 0xFF
        size = f"~{(1 << cap) // (1024 * 1024)} MB (estimated)" if 16 <= cap <= 30 else "unknown"
        print(f"  Flash ID : 0x{fid:08X} / size {size}")
        st = dev.log_status()
        print(f"  Records  : {st['count']}")
        print(f"  Used     : {st['next_addr']:,} bytes")
        print(f"  Logging  : {'on' if st['status'] & 2 else 'off'} (0x{st['status']:X})")
        fix = dev.query("PMTK400", "PMTK500,")
        print(f"  Fix rate : {fix[1]} ms")

    def live_status(self):
        dev = self.need()
        print("  Live status (Ctrl+C to stop)\n")
        inview = "-"
        gga = None
        while True:
            for line in dev.lines(1.0):
                f = check_line(line)
                if not f:
                    continue
                if f[0].endswith("GSV"):
                    inview = f[3]
                elif f[0].endswith("GGA"):
                    gga = f
                elif f[0].endswith("RMC"):
                    print(format_rmc(f, gga, inview), flush=True)

    def live_raw(self):
        dev = self.need()
        print("  Raw NMEA (Ctrl+C to stop)\n")
        while True:
            for line in dev.lines(1.0):
                if line:
                    print(line, flush=True)

    def restart(self, code, name):
        def run():
            if not confirm(f"  Run {name}? Log data is not erased."):
                print("  Cancelled.")
                return
            self.need().write(f"PMTK{code}")
            print("  Command sent. Getting a new fix may take a while.")
        return run

    # ---- download

    def download(self):
        dev = self.need()
        st = dev.log_status()
        total = st["next_addr"]
        if total == 0:
            print("  No log data stored.")
            return
        if st["status"] & 2:
            print("  * Logging is on. The logger keeps recording during the download.")
        os.makedirs(CONFIG["outdir"], exist_ok=True)
        base = os.path.join(CONFIG["outdir"], datetime.now().strftime("gps742_%Y%m%d_%H%M%S"))
        bt = dev.bluetooth
        chunk = CONFIG["bt_chunk"] if bt else CONFIG["chunk"]
        attempts = CONFIG["bt_retries"] + 1 if bt else 5
        print(f"  Downloading {total:,} bytes over {'Bluetooth' if bt else 'USB/serial'}, "
              f"{chunk} byte blocks (Ctrl+C to stop)")
        data = bytearray()
        t0 = time.time()
        addr = 0
        failures = 0
        saved_rates = self.nmea_off() if bt else None
        try:
            while addr < total:
                try:
                    size = min(chunk, total - addr)
                    # Bluetooth: a block normally takes ~0.1 s + 0.22 s/KB, so give up quickly
                    timeout = 3 + size / 1024 if bt else None
                    block = None
                    for attempt in range(attempts):
                        if bt:
                            time.sleep(CONFIG["bt_delay"])
                        try:
                            block = dev.read_memory(addr, size, timeout)
                        except DeviceError:
                            if bt:
                                failures += 1
                                break        # write timeout: the Bluetooth module has stalled
                            time.sleep(2)
                        if block is not None:
                            break
                        failures += 1
                        dev.drain()
                        dev.flush_input()
                    if block is None:
                        if bt:
                            print(f"\n  Bluetooth transfer stopped at 0x{addr:08X}. The logger's Bluetooth"
                                  "\n  module does not recover by itself: power-cycle the logger and choose 1.")
                        else:
                            print(f"\n  Reading 0x{addr:08X} failed {attempts} times.")
                        choice = self.download_failed(addr, total, saved_rates)
                    else:
                        data += block
                        addr += size
                        pct = addr * 100 // total
                        rate = addr / max(time.time() - t0, 0.001) / 1024
                        bar = "#" * (pct // 4)
                        print(f"\r  [{bar:<25}] {pct:3d}%  {addr:,}/{total:,}  {rate:.1f} KB/s  "
                              f"retries {failures}", end="", flush=True)
                        continue
                except KeyboardInterrupt:
                    print("\n  Download paused. Waiting for the device output to settle...")
                    dev.drain()
                    dev.flush_input()
                    choice = self.download_failed(addr, total, saved_rates)
                if choice == "resume":
                    dev = self.dev      # a new connection after "reconnect and resume"
                    continue
                if choice == "discard":
                    print("  Download discarded.")
                    return
                base += "_partial"
                break
        finally:
            if saved_rates:
                self.nmea_restore(saved_rates)
        print(f"\n  Received {len(data):,} bytes in {time.time() - t0:.1f} s, {failures} retries")
        with open(base + ".bin", "wb") as f:
            f.write(data)
        meta = {"firmware": self.fw, "port": self.port, "downloaded": datetime.now().isoformat(),
                **st}
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"  Raw  : {base}.bin")
        convert(base + ".bin", st["format"])

    def nmea_off(self):
        """Turn NMEA output off for a Bluetooth download. Returns the previous rates."""
        dev = self.dev
        try:
            rates = [int(x) for x in dev.query("PMTK414", "PMTK514,")[1:]]
            if any(rates):
                dev.command("PMTK314," + ",".join("0" * len(rates)))
                time.sleep(0.5)
                dev.flush_input()
                print("  NMEA output turned off during the download.")
                return rates
        except DeviceError as e:
            print(f"  Could not turn NMEA output off ({e}), continuing.")
        return None

    def nmea_restore(self, rates):
        if not self.dev:
            print("\n  [Error] NMEA output could not be restored (not connected). After reconnecting, use "
                  "'GPS receiver settings > Restore default NMEA output'.")
            return
        try:
            self.dev.drain()
            self.dev.flush_input()
            flag = self.dev.command("PMTK314," + ",".join(map(str, rates)))
            print(f"\n  NMEA output restored: {ACK_TEXT.get(flag, flag)}")
        except DeviceError as e:
            print(f"\n  [Error] NMEA output could not be restored ({e}). After reconnecting, use "
                  "'GPS receiver settings > Restore default NMEA output' to turn it back on.")

    def download_failed(self, addr, total, saved_rates=None):
        """Ask what to do after a failed or interrupted download. Data received so far is kept."""
        while True:
            print(f"  Received {addr:,} of {total:,} bytes.")
            print("  1. Reconnect and resume (power-cycle the logger first)")
            print("  2. Resume without reconnecting")
            print("  3. Save what was received\n  0. Discard")
            s = ask("Select> ")
            if s == "1" and self.reconnect_resume(saved_rates):
                return "resume"
            if s == "2":
                return "resume"
            if s == "3":
                return "save"
            if s == "0":
                return "discard"

    def reconnect_resume(self, saved_rates):
        """Reopen the port after the logger was power-cycled. A stalled Bluetooth module only
        recovers this way; the data received so far stays in memory."""
        ask("  Power-cycle the logger, wait until it is on, then press Enter: ")
        self.connect()
        if not self.dev or not self.fw:
            print("  Not connected. Check the logger and try again.")
            return False
        if saved_rates:
            try:
                self.dev.command("PMTK314," + ",".join("0" * len(saved_rates)))
                time.sleep(0.5)
                self.dev.flush_input()
            except DeviceError as e:
                print(f"  Could not turn NMEA output off ({e}), continuing.")
        return True

    def convert_existing(self):
        os.makedirs(CONFIG["outdir"], exist_ok=True)
        files = sorted(glob.glob(os.path.join(CONFIG["outdir"], "*.bin")))
        if not files:
            print(f"  No .bin files in {CONFIG['outdir']}.")
            p = ask("  Enter a file path (Enter=cancel): ")
            if not p:
                return
        else:
            for i, fn in enumerate(files, 1):
                print(f" {i:2d}. {os.path.basename(fn)}  ({os.path.getsize(fn):,} bytes)")
            s = ask("  File number or path to convert (Enter=cancel): ")
            if not s:
                return
            p = files[int(s) - 1] if s.isdigit() and 1 <= int(s) <= len(files) else s
        if not os.path.exists(p):
            print("  File not found.")
            return
        convert(p)

    def erase(self):
        dev = self.need()
        st = dev.log_status()
        print(f"  This erases all {st['count']} records ({st['next_addr']:,} bytes).")
        print("  This cannot be undone. Make sure you have downloaded the log first.")
        if not confirm("  ", "erase"):
            print("  Cancelled.")
            return
        print("  Erasing... (can take up to a minute)")
        report_ack(dev.command("PMTK182,6,1", timeout=90, retries=0))

    # ---- logger settings

    def settings_header(self):
        st = self.need().log_status()
        print(f"  Logging        : {'on' if st['status'] & 2 else 'off'}")
        print(f"  Time interval  : {fmt_interval(st['period'], 's')}")
        print(f"  Distance       : {fmt_interval(st['distance'], 'm')}")
        print(f"  Speed          : {fmt_interval(st['speed'], 'km/h')}")
        print(f"  When full      : {'overwrite' if st['mode'] == 1 else 'stop logging'}")
        print(f"  Fields         : {', '.join(format_names(st['format']))}")
        print(f"  Usage          : {st['count']} records / {st['next_addr']:,} bytes")

    def set_interval(self, param, key, name, unit, hi):
        def run():
            cur = self.need().log_status()[key] / 10
            v = ask_number(f"  {name} (0=disabled)", cur, 0, hi, unit)
            if v is None:
                return
            report_ack(self.dev.log_set(param, int(round(v * 10))))
        return run

    def set_mode(self):
        print("  1. Overwrite (oldest records first)\n  2. Stop logging")
        s = ask("  Select (Enter=cancel): ")
        if s in ("1", "2"):
            report_ack(self.need().log_set(6, int(s)))

    def toggle_logging(self):
        dev = self.need()
        on = dev.log_status()["status"] & 2
        if on:
            report_ack(dev.command("PMTK182,5"))
            print("  Logging stopped.")
        else:
            report_ack(dev.command("PMTK182,4"))
            print("  Logging started.")

    def factory_defaults(self):
        dev = self.need()
        st = dev.log_status()
        print(f"  {'Setting':<15} {'Current':<22} Default")
        for key, (_, value, label) in FACTORY_DEFAULTS.items():
            print(f"  {label:<15} {setting_text(key, st[key]):<22} {setting_text(key, value)}")
        print("\n  GPS receiver settings (DGPS, SBAS, ...) are also reset with a full cold")
        print("  restart (PMTK104). Getting a new fix may take a few minutes.")
        print("  Stored log data is not erased.")
        if not confirm("  Restore factory defaults?"):
            print("  Cancelled.")
            return
        dev.write("PMTK104")
        restarted = any(line.startswith("$PMTK010,001") for line in dev.lines(8))
        print(f"  Full cold restart: {'done' if restarted else 'no restart message (continuing)'}")
        dev.flush_input()
        for key, (param, value, label) in FACTORY_DEFAULTS.items():
            if st[key] == value:
                continue
            flag = dev.log_set(param, f"{value:08X}" if key == "format" else value)
            print(f"  {label:<15} -> {setting_text(key, value)}: {ACK_TEXT.get(flag, flag)}")
        st = dev.log_status()
        bad = [label for key, (_, value, label) in FACTORY_DEFAULTS.items() if st[key] != value]
        print("  All logger settings are at factory defaults." if not bad
              else f"  [Error] still different: {', '.join(bad)}")

    def edit_format(self):
        dev = self.need()
        fmt = dev.log_status()["format"]
        orig = fmt
        while True:
            print("\n  Log fields (number = toggle on/off)")
            for i, (bit, _, label, size) in enumerate(FIELDS, 1):
                mark = "x" if fmt & (1 << bit) else " "
                print(f"  {i:2d}. [{mark}] {label} ({size}B)")
            print(f"  {len(FIELDS) + 1:2d}. Apply to device")
            print("   0. Back (discard changes)")
            print(f"  Record size: {record_size(fmt)} bytes (excluding satellite data)")
            s = ask("Select> ")
            if s == "0":
                return
            if not s.isdigit():
                continue
            n = int(s)
            if 1 <= n <= len(FIELDS):
                fmt ^= 1 << FIELDS[n - 1][0]
            elif n == len(FIELDS) + 1:
                if fmt == orig:
                    print("  Nothing changed.")
                    return
                report_ack(dev.log_set(2, f"{fmt:08X}"))
                return

    # ---- GPS receiver settings

    NMEA_NAMES = ["GLL", "RMC", "VTG", "GGA", "GSA", "GSV", "GRS", "GST", "", "", "", "", "",
                  "MALM", "MEPH", "MDGP", "MDBG", "ZDA", "MCHN"]
    NMEA_EDIT = [0, 1, 2, 3, 4, 5, 17]

    def gps_header(self):
        dev = self.need()
        rates = [int(x) for x in dev.query("PMTK414", "PMTK514,")[1:]]
        fix = dev.query("PMTK400", "PMTK500,")[1]
        sbas = dev.query("PMTK413", "PMTK513,")[1]
        dgps = dev.query("PMTK401", "PMTK501,")[1]
        out = ", ".join(f"{self.NMEA_NAMES[i]}={rates[i]}" for i in self.NMEA_EDIT if i < len(rates))
        print(f"  NMEA output: {out}  (N = every N fixes, 0 = off)")
        print(f"  Fix rate   : {fix} ms")
        print(f"  SBAS       : {'on' if sbas == '1' else 'off'}")
        print(f"  DGPS       : {['none', 'RTCM', 'WAAS/SBAS'][int(dgps)] if dgps in ('0', '1', '2') else dgps}")

    def set_nmea(self):
        dev = self.need()
        rates = [int(x) for x in dev.query("PMTK414", "PMTK514,")[1:]]
        for n, i in enumerate(self.NMEA_EDIT, 1):
            print(f"  {n}. {self.NMEA_NAMES[i]} = {rates[i]}")
        s = ask("  Sentence number to change (Enter=cancel): ")
        if not s.isdigit() or not 1 <= int(s) <= len(self.NMEA_EDIT):
            return
        i = self.NMEA_EDIT[int(s) - 1]
        v = ask_number(f"  {self.NMEA_NAMES[i]} output rate", rates[i], 0, 5)
        if v is None:
            return
        rates[i] = int(v)
        report_ack(dev.command("PMTK314," + ",".join(map(str, rates))))

    def nmea_default(self):
        report_ack(self.need().command("PMTK314,-1"))

    def set_fix_rate(self):
        dev = self.need()
        cur = int(dev.query("PMTK400", "PMTK500,")[1])
        v = ask_number("  Fix rate (ms)", cur, 100, 10000, "ms")
        if v is not None:
            report_ack(dev.command(f"PMTK220,{int(v)}"))

    def toggle_sbas(self):
        dev = self.need()
        on = dev.query("PMTK413", "PMTK513,")[1] == "1"
        report_ack(dev.command(f"PMTK313,{0 if on else 1}"))
        print(f"  SBAS turned {'off' if on else 'on'}.")

    def set_dgps(self):
        print("  0. None\n  1. RTCM\n  2. WAAS/SBAS")
        s = ask("  Select (Enter=cancel): ")
        if s in ("0", "1", "2"):
            report_ack(self.need().command(f"PMTK301,{s}"))

    # ---- program settings

    def toggle_rollover(self):
        CONFIG["rollover"] = "off" if CONFIG["rollover"] == "auto" else "auto"
        return "nopause"

    def set_chunk(self, key, name):
        def run():
            v = ask_number(f"  {name} download block size (bytes)", CONFIG[key], MIN_CHUNK, 0x10000)
            if v is not None:
                CONFIG[key] = int(v)
        return run

    def set_bt_retries(self):
        v = ask_number("  Bluetooth quick retries per block", CONFIG["bt_retries"], 0, 5)
        if v is not None:
            CONFIG["bt_retries"] = int(v)

    def set_outdir(self):
        p = ask(f"  Output folder [current {CONFIG['outdir']}, Enter=keep]: ")
        if p:
            CONFIG["outdir"] = os.path.abspath(os.path.expanduser(p))

    # ---- menu tree

    def build(self):
        device = Menu("Device info / live view", [
            ("Device info", self.show_info),
            ("Live position status", self.live_status),
            ("Raw NMEA view", self.live_raw),
            ("Hot restart", self.restart(101, "hot restart")),
            ("Warm restart", self.restart(102, "warm restart")),
            ("Cold restart", self.restart(103, "cold restart")),
        ])
        download = Menu("Log download", [
            ("Download full log (save .bin/.csv/.gpx)", self.download),
            ("Convert a saved .bin file again", self.convert_existing),
            ("Erase log memory", self.erase),
        ])
        logger = Menu("Logger settings", [
            ("Start / stop logging", self.toggle_logging),
            ("Set time interval", self.set_interval(3, "period", "Time interval", "s", 3600)),
            ("Set distance interval", self.set_interval(4, "distance", "Distance interval", "m", 10000)),
            ("Set speed interval", self.set_interval(5, "speed", "Speed interval", "km/h", 1000)),
            ("When memory is full", self.set_mode),
            ("Select log fields", self.edit_format),
            ("Restore factory defaults", self.factory_defaults),
        ], header=self.settings_header)
        gps = Menu("GPS receiver settings", [
            ("NMEA output sentences", self.set_nmea),
            ("Restore default NMEA output", self.nmea_default),
            ("Fix rate", self.set_fix_rate),
            ("SBAS on / off", self.toggle_sbas),
            ("DGPS mode", self.set_dgps),
        ], header=self.gps_header)
        options = Menu("Connection / program settings", [
            ("Change port / baud rate", self.change_port),
            ("Reconnect", self.reconnect),
            (lambda: f"GPS week rollover fix: {CONFIG['rollover']}", self.toggle_rollover),
            (lambda: f"Download block size (USB): {CONFIG['chunk']} bytes",
             self.set_chunk("chunk", "USB")),
            (lambda: f"Download block size (Bluetooth): {CONFIG['bt_chunk']} bytes",
             self.set_chunk("bt_chunk", "Bluetooth")),
            (lambda: f"Bluetooth quick retries: {CONFIG['bt_retries']}", self.set_bt_retries),
            (lambda: f"Output folder: {CONFIG['outdir']}", self.set_outdir),
        ])
        return Menu("GPS742 logger manager", [
            ("Device info / live view", device),
            ("Log download", download),
            ("Logger settings", logger),
            ("GPS receiver settings", gps),
            ("Connection / program settings", options),
        ], header=self.top_header, top=True)


def record_size(fmt):
    return sum(size for bit, _, _, size in FIELDS if fmt & (1 << bit) and bit not in (14, 15, 16)) + 2


def fmt_interval(v, unit):
    return "disabled" if v == 0 else f"{v / 10:g} {unit}"


def setting_text(key, value):
    if key == "format":
        return f"0x{value:08X}"
    if key == "mode":
        return "overwrite" if value == 1 else "stop logging"
    return fmt_interval(value, {"period": "s", "distance": "m", "speed": "km/h"}[key])


def nmea_coord(v, hemi):
    if not v:
        return None
    d = float(v[:v.index(".") - 2]) + float(v[v.index(".") - 2:]) / 60
    return -d if hemi in ("S", "W") else d


def format_rmc(f, gga, inview):
    hhmmss, status, date = f[1], f[2], f[9]
    t = "--"
    if len(hhmmss) >= 6 and len(date) == 6:
        dt = datetime.strptime(date + hhmmss[:6], "%d%m%y%H%M%S").replace(tzinfo=timezone.utc)
        # 1980-01-06 is the GPS epoch: the receiver has no time yet (e.g. after a cold restart)
        t = "time unknown" if date == "060180" else iso(fix_time(dt.timestamp()))
    if status != "A":
        return f"  {t}  no fix ({inview} satellites in view)"
    lat, lon = nmea_coord(f[3], f[4]), nmea_coord(f[5], f[6])
    kmh = float(f[7] or 0) * 1.852
    alt = sats = hdop = "-"
    if gga:
        sats, hdop, alt = gga[7], gga[8], gga[9]
    return (f"  {t}  {lat:.6f}, {lon:.6f}  alt {alt}m  {kmh:.1f}km/h  "
            f"sats {sats}/{inview}  HDOP {hdop}")


def main():
    ap = argparse.ArgumentParser(description="GPS742 (MTK) GPS logger manager")
    ap.add_argument("-p", "--port", help="serial port (default: choose from a list at startup)")
    ap.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default 115200)")
    ap.add_argument("--convert", metavar="BIN", help="convert a .bin file without a device and exit")
    args = ap.parse_args()
    if args.convert:
        convert(args.convert)
        return
    port = args.port
    if port is None:
        try:
            port = select_port()
        except KeyboardInterrupt:
            port = None
        if port is None:
            print()
            return
    app = App(port, args.baud)
    try:
        app.build().run()
    except KeyboardInterrupt:
        print()
    finally:
        if app.dev:
            app.dev.close()


if __name__ == "__main__":
    main()
