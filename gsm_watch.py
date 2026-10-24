#!/usr/bin/env python3
"""
gsm_watch.py - loop grgsm_scanner over GSM900 / DCS1800, log every result,
keep a baseline of known cells, and alert on anything new or odd.

Usage:
    ./gsm_watch.py                       # offline mode: bladerf=0, gain 35, both bands,
                                         #   cells checked against a local btsearch export
                                         #   (downloaded automatically, refreshed weekly)
    ./gsm_watch.py --mode online         # online mode: every pass bulk-checked live via the
                                         #   btsearch API (needs an API key, see --api-key-file)
    ./gsm_watch.py -g 40 -b GSM900       # one band only
    ./gsm_watch.py --args rtl=0 --ppm 52 # RTL-SDR dongle: GSM900 only (R820T stops at 1766 MHz),
                                         #   data in ./gsm_watch_data_rtl, give it your ppm error
    ./gsm_watch.py --check               # check every baseline cell against btsearch, no scanning
    ./gsm_watch.py --baseline-only       # just (re)build baseline from scans.csv, no scanning
    ./gsm_watch.py --import grgsm_scanner/*.txt   # seed baseline from raw grgsm_scanner logs
    ./gsm_watch.py --fetch-cells         # force a fresh download of the export now

Files (in --dir, default ./gsm_watch_data):
    scans.csv      every cell seen, one row per scan pass
    baseline.json  cells considered "known" (edit by hand to curate)
    alerts.log     human-readable alerts with timestamps
    raw/           raw grgsm_scanner output per pass, for the report
    cells.ntm      local btsearch.pl GSM cell export (offline mode / online fallback)
    btsearch.key   API key for online mode (never committed; gsm_watch_data/ is gitignored)

Before every pass the bladeRF is probed with `bladeRF-cli -p`; if it is missing the loop
waits (alerting once) instead of running the scanner, and alerts again when it is back.

Stop with Ctrl-C (not kill -9, or GNU Radio leaks shm segments).
"""

import argparse, csv, ctypes, json, os, re, shutil, signal, subprocess, sys, time
import email.utils, urllib.error, urllib.parse, urllib.request
from datetime import datetime

LINE_RE = re.compile(
    r"ARFCN:\s*(?P<arfcn>\d+),\s*Freq:\s*(?P<freq>[\d.]+)M,\s*CID:\s*(?P<cid>\d+),"
    r"\s*LAC:\s*(?P<lac>\d+),\s*MCC:\s*(?P<mcc>\d+),\s*MNC:\s*(?P<mnc>\d+),\s*Pwr:\s*(?P<pwr>-?\d+)")
ARFCNS_RE = re.compile(r"\|----\s*Cell ARFCNs:\s*(.*)")
NEIGH_RE  = re.compile(r"\|----\s*Neighbour Cells:\s*(.*)")

MNC_NAMES = {"1": "Plus", "2": "T-Mobile", "3": "Orange", "6": "Play"}
# operators that legitimately share LACs (RAN sharing: T-Mobile + Orange via NetWorks!)
SHARED_LAC_GROUPS = [{"2", "3"}]
# a cell must look odd in this many consecutive passes before the oddity is reported
# (scanner often fails to decode SI3/SI2 in one pass -> CID 0, empty lists)
CONFIRM_PASSES = 2
DEVICE_NAMES = {"bladerf": "bladeRF", "rtl": "RTL-SDR (RTL2832U)", "rtl_tcp": "RTL-SDR via rtl_tcp",
                "hackrf": "HackRF", "uhd": "USRP", "airspy": "Airspy", "soapy": "SoapySDR"}
# btsearch.pl public API; the cell export needs no account but is limited to 10 calls per 5 minutes.
# POST /cells/match needs an API key (X-Api-Key, scopes read:cells + read:stations), 30 requests/min,
# up to 5000 cells per request.
BTSEARCH_API = "https://btsearch.pl/api/v2"
MATCH_BATCH = 5000

stop = False
def _sig(*_):
    global stop
    stop = True
signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def device_kind(args):
    """osmosdr driver name from the device string: 'bladerf=0,antenna=rx2' -> 'bladerf'."""
    first = args.split(",")[0].strip().lower()
    return first.split("=")[0] or "unknown"


def apply_device_defaults(a):
    """Fill in --bands and --dir from the device when the user did not set them, and warn
    about band/device mismatches. Returns the device kind."""
    kind = device_kind(a.args)
    if a.bands is None:
        # R820T-based RTL-SDR dongles tune up to ~1766 MHz: DCS1800 downlink (1805-1880) is out
        a.bands = ["GSM900"] if kind in ("rtl", "rtl_tcp") else ["GSM900", "DCS1800"]
    elif kind in ("rtl", "rtl_tcp") and any(b in ("DCS1800", "PCS1900") for b in a.bands):
        print(f"WARNING: {a.bands} on an RTL-SDR: the R820T tuner stops at ~1766 MHz, "
              "DCS1800/PCS1900 will not decode", flush=True)
    if a.dir is None:
        # a different radio reads different (relative) power levels and sees a different set of
        # cells, so each device keeps its own baseline
        a.dir = "gsm_watch_data" if kind == "bladerf" else f"gsm_watch_data_{kind}"
    return kind


def run_scanner(band, args, gain, ppm, timeout):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = ["grgsm_scanner", "--args", args, "-b", band, "-g", str(gain), "-p", str(ppm)]
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.stdout + "\n" + p.stderr
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired carries stdout/stderr as bytes even with text=True
        parts = []
        for chunk in (e.stdout, e.stderr):
            if isinstance(chunk, bytes):
                chunk = chunk.decode(errors="replace")
            parts.append(chunk or "")
        return "\n".join(parts) + f"\n[gsm_watch] scanner timed out after {timeout}s\n"
    except FileNotFoundError:
        sys.exit("grgsm_scanner not found in PATH (are you inside a venv without system packages?)")


def device_present(args):
    """(ok, detail). bladeRF: probe with `bladeRF-cli -p` (opens the device, so only call it while
    no scanner is running). RTL-SDR: enumerate via librtlsdr without opening anything.
    Other osmosdr devices are assumed present."""
    kind = device_kind(args)
    if kind == "rtl":
        m = re.match(r"rtl=(\d+)", args.split(",")[0].strip().lower())
        want = int(m.group(1)) if m else 0
        try:
            lib = ctypes.CDLL("librtlsdr.so.2")
            lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
            n = lib.rtlsdr_get_device_count()
        except (OSError, AttributeError):
            return True, "librtlsdr not found, RTL-SDR device check skipped"
        if n > want:
            names = [lib.rtlsdr_get_device_name(i).decode(errors="replace") for i in range(n)]
            return True, f"RTL-SDR connected: {names[want]}" + (f" ({n} dongles)" if n > 1 else "")
        return False, (f"RTL-SDR rtl={want} not found: librtlsdr sees {n} dongle(s) on USB")
    if kind != "bladerf":
        return True, f"no device check for '{args}' (only bladerf and rtl are probed)"
    try:
        p = subprocess.run(["bladeRF-cli", "-p"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return True, "bladeRF-cli not found in PATH, device check skipped"
    except subprocess.TimeoutExpired:
        return False, "bladeRF-cli -p timed out (USB stuck?)"
    serials = re.findall(r"Serial:\s*(\S+)", p.stdout)
    if p.returncode == 0 and serials:
        return True, f"bladeRF connected, serial {serials[0]}" + (f" (+{len(serials) - 1} more)" if len(serials) > 1 else "")
    msg = " ".join((p.stdout + p.stderr).split()) or f"bladeRF-cli -p exit code {p.returncode}"
    return False, msg


def parse(output):
    cells, cur = [], None
    for line in output.splitlines():
        m = LINE_RE.search(line)
        if m:
            cur = m.groupdict()
            cur["cell_arfcns"] = ""
            cur["neighbours"] = ""
            cells.append(cur)
            continue
        if cur is None:
            continue
        m = ARFCNS_RE.search(line)
        if m:
            cur["cell_arfcns"] = m.group(1).strip()
            continue
        m = NEIGH_RE.search(line)
        if m:
            cur["neighbours"] = m.group(1).strip()
    return cells


def _hz(s):
    s = s.strip().upper()
    mult = {"K": 1e3, "M": 1e6, "G": 1e9}.get(s[-1], 1)
    return float(s.rstrip("KMG")) * mult


def key(c):
    return f"{c['mcc']}-{c['mnc']}-{c['lac']}-{c['cid']}-{c['arfcn']}"


def save_json(path, obj):
    """Write via a temp file + rename so a Ctrl-C mid-write cannot truncate the file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def undecoded(c):
    """True when the scanner clearly failed to read the SI messages (not a property of the cell)."""
    return c["cid"] == "0" or c["mcc"] == "0"


def band_of(arfcn):
    """Band from ARFCN number (GSM900 incl. E-GSM 975-1023, DCS1800 512-885)."""
    n = int(arfcn)
    if n <= 124 or n >= 955:
        return "GSM900"
    if 512 <= n <= 885:
        return "DCS1800"
    return "?"


def load_cells_db(path):
    """Known-cell database exported from btsearch.pl, .ntm (NetMonitor) or .clf (CLF 3.0 dec).
    Returns {(mcc, mnc, lac, cid): "description (lat,lon)"} for 2G cells only."""
    db = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split(";")
            if len(p) >= 10 and p[0] in ("2G", "3G", "4G", "5G"):
                # NTM: RAT;MCC;MNC;CID;LAC;?;?;LAT;LON;DESC;?
                if p[0] != "2G":
                    continue
                mcc, mnc, cid, lac, lat, lon, desc = p[1], p[2], p[3], p[4], p[7], p[8], p[9]
            elif len(p) >= 8 and len(p[0]) >= 5 and p[0].isdigit():
                # CLF: MCCMNC;CID;LAC;RNC;LAT;LON;POS-RAT;DESC;SYSTEM  (no RAT column:
                # RNC != 0 means 3G, CID > 65535 means LTE/NR, the rest is 2G)
                mcc, mnc, cid, lac, lat, lon, desc = p[0][:3], p[0][3:], p[1], p[2], p[4], p[5], p[7]
                if int(p[3] or 0) or int(cid) > 65535:
                    continue
            else:
                continue
            db[(mcc, str(int(mnc)), str(int(lac)), str(int(cid)))] = f"{desc} ({lat},{lon})"
    return db


def fetch_cells_db(path, country="PL", region_ids=None):
    """Download the GSM cell export (NTM format) from btsearch.pl into `path`.
    A HEAD request first reads Last-Modified; the download is skipped when the local
    file is already that new. The file's mtime is set to the server time afterwards."""
    q = {"format": "ntm", "rats": "gsm", "countryCodes": country}
    if region_ids:
        q["regionIds"] = region_ids
    url = f"{BTSEARCH_API}/cells/export?{urllib.parse.urlencode(q)}"
    hdr = {"User-Agent": "gsm_watch (gr-gsm cell monitor)"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr, method="HEAD"), timeout=60) as r:
            lm = r.headers.get("Last-Modified")
        remote = email.utils.parsedate_to_datetime(lm).timestamp() if lm else None
        if remote and os.path.exists(path) and os.path.getmtime(path) >= remote:
            print(f"cell DB already current: {path} (server {lm})")
            return
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=600) as r, \
                open(path + ".tmp", "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        retry = e.headers.get("X-Retry-After")
        print(f"cell DB download failed: HTTP {e.code}" + (f", retry after {retry}s" if retry else ""))
        return
    except (urllib.error.URLError, OSError) as e:
        print(f"cell DB download failed: {e}")
        return
    os.replace(path + ".tmp", path)
    if remote:
        os.utime(path, (remote, remote))
    print(f"cell DB downloaded: {path} ({os.path.getsize(path) // 1024} kB, server {lm})")


def plmn(c):
    """MCC+MNC as btsearch wants it; Polish MNCs are two digits ("6" -> "26006")."""
    return c["mcc"] + c["mnc"].zfill(2)


def match_cells(api_key, cells):
    """Bulk-match decoded GSM cells against btsearch via POST /cells/match.
    Returns (results, stations): results[i] belongs to cells[i] and has at least
    {"match": "cell"|"none", "stationId", "reason"}; stations maps id -> station dict
    with siteId and location. On any HTTP/network error returns (None, {}) after printing."""
    results, stations = [], {}
    hdr = {"X-Api-Key": api_key, "Content-Type": "application/json",
           "User-Agent": "gsm_watch (gr-gsm cell monitor)"}
    url = f"{BTSEARCH_API}/cells/match?include=stations"
    for i in range(0, len(cells), MATCH_BATCH):
        body = {"cells": [{"rat": "gsm", "plmn": plmn(c), "lac": int(c["lac"]), "cid": int(c["cid"])}
                          for c in cells[i:i + MATCH_BATCH]]}
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)["data"]
        except urllib.error.HTTPError as e:
            try:
                msg = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in json.load(e)["errors"])
            except Exception:
                msg = e.reason
            retry = e.headers.get("X-Retry-After")
            print(f"   btsearch match failed: HTTP {e.code} {msg}" + (f" (retry after {retry}s)" if retry else ""),
                  flush=True)
            return None, {}
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            print(f"   btsearch match failed: {e}", flush=True)
            return None, {}
        results.extend(data["results"])
        for st in data.get("stations") or []:
            stations[st["id"]] = st
    return results, stations


def station_desc(st):
    """'[KRA0587] Kraków, os. Oświecenia 9 - blok mieszkalny (lat,lon)' from a station dict."""
    if not st:
        return ""
    loc = st.get("location") or {}
    place = ", ".join(x for x in (loc.get("city"), loc.get("address")) if x)
    pos = f" ({loc.get('latitude')},{loc.get('longitude')})" if loc.get("latitude") is not None else ""
    return f"[{st.get('siteId') or st.get('id')}] {place}{pos}"


def match_flag(res, stations):
    """(flag_text, site_desc) for one match result. flag_text is a hard oddity or None."""
    if res is None:
        return None, ""
    if res.get("match") == "cell":
        return None, station_desc(stations.get(res.get("stationId")))
    return f"NOT in btsearch ({res.get('reason') or res.get('match')})", ""


def db_lookup(db, c):
    return db.get((c["mcc"], c["mnc"], c["lac"], c["cid"]))


def strong_reason(c, band, baseline, abs_thresh):
    """Flag text when a NEW cell is suspiciously strong, else None.
    The scanner's Pwr is uncalibrated (legit cells here read -18), so by default compare
    against the strongest known cell in the same band: a catcher has to out-shout the real
    network to attract phones. --strong-pwr switches to a fixed absolute level."""
    pwr = int(c["pwr"])
    if abs_thresh is not None:
        return f"very strong for a new cell ({pwr} > {abs_thresh})" if pwr > abs_thresh else None
    known = [int(v["last_pwr"]) for kk, v in baseline.items()
             if v.get("band") == band and "last_pwr" in v and kk != key(c)]
    if not known:
        return None
    top = max(known)
    return f"stronger than any known cell in {band} ({pwr} vs {top})" if pwr >= top else None


def oddities(c, lac_ops):
    """Heuristics that make a cell worth a second look. Returns (soft, hard) lists.
    soft = needs CONFIRM_PASSES consecutive passes; hard = report at once."""
    soft, hard = [], []
    if undecoded(c):
        soft.append("CID=0 / SI3 not decoded")
        return soft, hard
    # empty neighbour list only counts when the rest of the SI was decoded fine
    if not c["neighbours"] and c["cell_arfcns"]:
        soft.append("empty neighbour list")
    if c["mcc"] != "260":
        hard.append(f"foreign MCC {c['mcc']}")
    if c["mnc"] not in MNC_NAMES:
        hard.append(f"unknown MNC {c['mnc']}")
    # one LAC advertised by several operators, unless they are a known sharing group
    ops = lac_ops.get(c["lac"], set()) | {c["mnc"]}
    if len(ops) > 1 and not any(ops <= g for g in SHARED_LAC_GROUPS):
        hard.append(f"LAC {c['lac']} shared by MNC {','.join(sorted(ops))}")
    return soft, hard


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--args", default="bladerf=0", help="osmosdr device string")
    ap.add_argument("-g", "--gain", type=float, default=35)
    ap.add_argument("--ppm", type=int, default=0,
                    help="frequency correction in whole ppm passed to the scanner/capture (grgsm_scanner "
                         "only accepts integers; RTL-SDR crystals need it, measure with kalibrate or rtl_test -p)")
    ap.add_argument("-b", "--bands", nargs="+",
                    help="default: GSM900 + DCS1800, or GSM900 only for an RTL-SDR")
    ap.add_argument("--dir", help="data directory (default gsm_watch_data, or gsm_watch_data_<device> "
                                  "for a non-bladeRF device: each radio keeps its own baseline)")
    ap.add_argument("--interval", type=int, default=60, help="seconds to sleep between passes")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="max seconds per scanner run (a full band often takes ~45 min)")
    ap.add_argument("--skip-device-check", action="store_true",
                    help="don't probe for the SDR before each pass")
    ap.add_argument("--learn", type=int, default=3,
                    help="passes during which new cells are added to baseline silently")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--import", dest="import_files", nargs="+", metavar="FILE",
                    help="seed baseline from raw grgsm_scanner output files, then exit")
    ap.add_argument("--mode", choices=["offline", "online"], default="offline",
                    help="offline: check cells against a local btsearch export, downloaded automatically; "
                         "online: bulk-check every pass live via the btsearch API (needs an API key)")
    ap.add_argument("--cells", metavar="FILE",
                    help="local btsearch export, .ntm or .clf (default <dir>/cells.ntm)")
    ap.add_argument("--refresh-days", type=float, default=7,
                    help="offline: re-download the export when the local file is older than this (0 = never)")
    ap.add_argument("--fetch-cells", action="store_true", help="force a download/refresh of the export now")
    ap.add_argument("--region-ids", help="comma-separated btsearch region ids to limit the download "
                                         "(e.g. 2 = Małopolskie); default: whole country")
    ap.add_argument("--api-key", help="online: btsearch.pl API key (sk_...); "
                                       "or set BTSEARCH_API_KEY, or use --api-key-file")
    ap.add_argument("--api-key-file", metavar="PATH",
                    help="file holding the API key (default <dir>/btsearch.key if it exists)")
    ap.add_argument("--check", "--compare", "--match", dest="check", action="store_true",
                    help="check every baseline cell against btsearch (export or API per --mode), then exit")
    ap.add_argument("--strong-pwr", type=int, default=None,
                    help="absolute Pwr above which a NEW cell is flagged as very strong; "
                         "default: flag only when stronger than every known cell in its band")
    ap.add_argument("--capture-secs", type=int, default=0,
                    help="on alert, record this many seconds of raw I/Q at the cell's BCCH (0 = off)")
    ap.add_argument("--capture-rate", default="2M", help="sample rate for I/Q capture")
    ap.add_argument("--capture-max", type=int, default=20, help="max captures per run (disk guard)")
    a = ap.parse_args()
    kind = apply_device_defaults(a)

    os.makedirs(os.path.join(a.dir, "raw"), exist_ok=True)
    csv_path = os.path.join(a.dir, "scans.csv")
    base_path = os.path.join(a.dir, "baseline.json")
    alert_path = os.path.join(a.dir, "alerts.log")

    baseline = {}
    if os.path.exists(base_path):
        with open(base_path) as f:
            baseline = json.load(f)

    # ---- btsearch: offline (local export) or online (live API) --------------------------
    cells_path = a.cells or os.path.join(a.dir, "cells.ntm")
    api_key = a.api_key or os.environ.get("BTSEARCH_API_KEY")
    # the key is shared between device directories: look in --dir first, then the default dir
    for key_file in [a.api_key_file, os.path.join(a.dir, "btsearch.key"), "gsm_watch_data/btsearch.key"]:
        if api_key or not key_file or not os.path.exists(key_file):
            continue
        with open(key_file) as f:
            api_key = f.read().strip()
    if a.mode == "online":
        if not api_key:
            sys.exit("--mode online needs an API key: --api-key, --api-key-file or BTSEARCH_API_KEY")
        print(f"online mode: btsearch API key loaded ({api_key[:3]}...{api_key[-4:]})")

    db = {}
    stale = (not os.path.exists(cells_path) or
             (a.refresh_days > 0 and time.time() - os.path.getmtime(cells_path) > a.refresh_days * 86400))
    if a.fetch_cells or (a.mode == "offline" and stale):
        fetch_cells_db(cells_path, region_ids=a.region_ids)
    if os.path.exists(cells_path):
        db = load_cells_db(cells_path)
        print(f"{'offline mode' if a.mode == 'offline' else 'fallback'}: {len(db)} 2G cells from {cells_path}")
    elif a.mode == "offline":
        sys.exit(f"offline mode needs a local export and none is at {cells_path} (download failed?)")

    def check_cells(cells):
        """{id(cell): (flag, site)} for decoded cells. Online: one bulk API request, falling back
        to the local export if the API fails. Offline: local export lookups."""
        if not cells:
            return {}
        if a.mode == "online":
            results, stations = match_cells(api_key, cells)
            if results is not None:
                return {id(c): match_flag(r, stations) for c, r in zip(cells, results)}
            if not db:
                return {}
            print("   falling back to the local cell DB for this pass", flush=True)
        out = {}
        for c in cells:
            hit = db_lookup(db, c)
            out[id(c)] = (None, hit) if hit else ("NOT in cell DB", "")
        return out

    if a.check:
        rows = []
        for k, v in sorted(baseline.items(), key=lambda kv: (kv[1].get("band", ""), int(kv[0].split("-")[-1]))):
            mcc, mnc, lac, cid, arfcn = k.split("-")
            rows.append((k, v, dict(mcc=mcc, mnc=mnc, lac=lac, cid=cid, arfcn=arfcn)))
        verdict = check_cells([r[2] for r in rows])
        if rows and not verdict:
            sys.exit("check failed")
        unknown = 0
        for k, v, c in rows:
            flag, site = verdict.get(id(c), (None, ""))
            unknown += bool(flag)
            if site:
                baseline[k]["site"] = site
            print(f"{v.get('band', '?'):8} ARFCN {c['arfcn']:>4} {c['mcc']}-{c['mnc']} {MNC_NAMES.get(c['mnc'], '?'):8} "
                  f"LAC {c['lac']:>5} CID {c['cid']:>5} seen {v['seen']:>3}  {flag or site}")
        save_json(base_path, baseline)
        print(f"{len(rows)} baseline cells checked ({a.mode}), {unknown} unknown to btsearch")
        return

    if a.import_files:
        added = 0
        for path in a.import_files:
            ts = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            with open(path, errors="replace") as f:
                cells = parse(f.read())
            for c in cells:
                if undecoded(c):
                    continue
                k = key(c)
                if k not in baseline:
                    baseline[k] = {"first_seen": ts, "seen": 0, "band": band_of(c["arfcn"])}
                    added += 1
                baseline[k]["seen"] += 1
                if ts >= baseline[k].get("last_seen", ""):
                    baseline[k]["last_seen"] = ts
                    baseline[k]["last_pwr"] = c["pwr"]
            print(f"{path}: {len(cells)} cells")
        save_json(base_path, baseline)
        print(f"baseline: {len(baseline)} cells ({added} new)")
        return

    if a.baseline_only:
        if not os.path.exists(csv_path):
            sys.exit(f"{csv_path} not found: nothing to build a baseline from")
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if undecoded(row):
                    continue  # same rule as the live loop: CID 0 rows never enter the baseline
                k = key(row)
                baseline.setdefault(k, {"first_seen": row["time"], "seen": 0, "band": row["band"]})
                baseline[k]["seen"] += 1
                baseline[k]["last_seen"] = row["time"]
                baseline[k]["last_pwr"] = row["pwr"]
        save_json(base_path, baseline)
        print(f"baseline rebuilt: {len(baseline)} cells")
        return

    new_csv = not os.path.exists(csv_path)
    csv_f = open(csv_path, "a", newline="")
    fields = ["time", "band", "arfcn", "freq", "mcc", "mnc", "op", "lac", "cid", "pwr",
              "cell_arfcns", "neighbours", "flags"]
    w = csv.DictWriter(csv_f, fieldnames=fields)
    if new_csv:
        w.writeheader()

    def alert(msg):
        line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
        print("\a*** " + line, flush=True)
        with open(alert_path, "a") as f:
            f.write(line + "\n")

    captures_done = 0
    cap_dir = os.path.join(a.dir, "iq")

    def capture(c, ts, flags):
        """Record raw I/Q of the suspicious BCCH with grgsm_capture + SigMF-style sidecar."""
        nonlocal captures_done
        if a.capture_secs <= 0 or captures_done >= a.capture_max:
            return
        os.makedirs(cap_dir, exist_ok=True)
        stem = f"{ts.replace(':', '-')}_arfcn{c['arfcn']}_lac{c['lac']}_cid{c['cid']}"
        cfile = os.path.join(cap_dir, stem + ".cfile")
        freq = f"{c['freq']}M"
        cmd = ["grgsm_capture", "--args", a.args, "-f", freq, "-s", a.capture_rate,
               "-g", str(a.gain), "-p", str(a.ppm), "-c", cfile, "-T", str(a.capture_secs)]
        print(f"   recording {a.capture_secs}s of I/Q at {freq} -> {cfile}", flush=True)
        try:
            subprocess.run(cmd, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                           capture_output=True, text=True, timeout=a.capture_secs + 60)
        except Exception as e:
            print(f"   capture failed: {e}", flush=True)
            return
        meta = {
            "global": {"core:datatype": "cf32_le", "core:sample_rate": _hz(a.capture_rate),
                       "core:version": "1.0.0", "core:description": "gsm_watch alert capture",
                       "core:hw": f"{DEVICE_NAMES.get(kind, kind)}, osmosdr args {a.args}, "
                                  f"gain {a.gain}, ppm {a.ppm}"},
            "captures": [{"core:sample_start": 0, "core:frequency": c["freq"] and float(c["freq"]) * 1e6,
                          "core:datetime": ts}],
            "annotations": [{"core:sample_start": 0, "gsm:arfcn": c["arfcn"], "gsm:mcc": c["mcc"],
                             "gsm:mnc": c["mnc"], "gsm:lac": c["lac"], "gsm:cid": c["cid"],
                             "gsm:pwr_dbm": c["pwr"], "gsm:flags": flags}],
        }
        with open(os.path.join(cap_dir, stem + ".sigmf-meta"), "w") as f:
            json.dump(meta, f, indent=1)
        captures_done += 1

    def wait_for_device():
        """Block until the SDR is present (or stop is requested). Returns False if stopping."""
        if a.skip_device_check:
            return True
        warned = False
        while not stop:
            ok, detail = device_present(a.args)
            if ok:
                if warned:
                    alert(f"SDR back: {detail}")
                return True
            if not warned:
                alert(f"SDR not available: {detail} -- waiting, retrying every {a.interval}s")
                warned = True
            for _ in range(max(a.interval, 5)):
                if stop:
                    break
                time.sleep(1)
        return False

    ok, detail = device_present(a.args)
    if not ok and not a.skip_device_check:
        sys.exit(f"SDR not available: {detail}\n"
                 "Connect the radio or use --skip-device-check.")
    print(detail)

    passno = 0
    streak = {}          # (band, arfcn, oddity) -> consecutive passes it was seen
    lac_ops = {}         # lac -> set of MNCs seen advertising it
    for k in baseline:   # rebuild lac->operators map from baseline keys mcc-mnc-lac-cid-arfcn
        p = k.split("-")
        if len(p) == 5:
            lac_ops.setdefault(p[2], set()).add(p[1])

    while not stop:
        passno += 1
        for band in a.bands:
            if stop or not wait_for_device():
                break
            ts = datetime.now().isoformat(timespec="seconds")
            print(f"[{ts}] pass {passno} scanning {band} ...", flush=True)
            out = run_scanner(band, a.args, a.gain, a.ppm, a.timeout)
            with open(os.path.join(a.dir, "raw", f"{ts.replace(':', '-')}_{band}.txt"), "w") as f:
                f.write(out)
            cells = parse(out)
            seen_now = set()
            odd_now = set()
            verdict = check_cells([c for c in cells if not undecoded(c)])   # btsearch, per --mode
            for c in cells:
                soft, hard = oddities(c, lac_ops)
                flags = list(hard)
                db_flag, site = verdict.get(id(c), (None, ""))
                if db_flag:
                    flags.append(db_flag)
                # soft oddities must persist CONFIRM_PASSES passes on the same ARFCN
                for o in soft:
                    sk = (band, c["arfcn"], o)
                    streak[sk] = streak.get(sk, 0) + 1
                    odd_now.add(sk)
                    if streak[sk] >= CONFIRM_PASSES:
                        flags.append(f"{o} (x{streak[sk]})")

                k = key(c)
                if undecoded(c):
                    # don't pollute the baseline with CID 0 rows; still log them
                    tag_cid = "CID ? (undecoded)"
                else:
                    seen_now.add(k)
                    lac_ops.setdefault(c["lac"], set()).add(c["mnc"])
                    if k not in baseline:
                        baseline[k] = {"first_seen": ts, "seen": 0, "band": band}
                        if passno > a.learn:
                            flags.insert(0, "NEW CELL")
                            strong = strong_reason(c, band, baseline, a.strong_pwr)
                            if strong:
                                flags.append(strong)
                            if site:
                                flags.append(f"btsearch: {site}")
                    baseline[k]["seen"] += 1
                    baseline[k]["last_seen"] = ts
                    baseline[k]["last_pwr"] = c["pwr"]
                    if site:
                        baseline[k]["site"] = site
                    tag_cid = f"CID {c['cid']}"

                c_row = dict(time=ts, band=band, op=MNC_NAMES.get(c["mnc"], "?"),
                             flags="; ".join(flags), **{f: c[f] for f in
                             ["arfcn", "freq", "mcc", "mnc", "lac", "cid", "pwr", "cell_arfcns", "neighbours"]})
                w.writerow(c_row)
                csv_f.flush()
                tag = f"ARFCN {c['arfcn']} ({c['freq']}M) {c['mcc']}-{c['mnc']} {c_row['op']} LAC {c['lac']} {tag_cid} {c['pwr']} dBm"
                if flags and (passno > a.learn or "NEW CELL" in flags):
                    alert(f"{tag}  [{'; '.join(flags)}]")
                    capture(c, ts, flags)
                else:
                    print("   " + tag, flush=True)
            # reset streaks for oddities that did not recur this pass
            for sk in [s for s in streak if s[0] == band and s not in odd_now]:
                del streak[sk]
            save_json(base_path, baseline)
            print(f"   {len(cells)} cells decoded", flush=True)
        for _ in range(a.interval):
            if stop:
                break
            time.sleep(1)

    csv_f.close()
    print("stopped cleanly.")


if __name__ == "__main__":
    main()
