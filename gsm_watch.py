#!/usr/bin/env python3
"""
gsm_watch.py - loop grgsm_scanner over GSM900 / DCS1800, log every result,
keep a baseline of known cells, and alert on anything new or odd.

Usage:
    ./gsm_watch.py                       # default: bladerf=0, gain 35, both bands
    ./gsm_watch.py -g 40 -b GSM900       # one band only
    ./gsm_watch.py --baseline-only       # just (re)build baseline from existing log, no scanning

Files (in --dir, default ./gsm_watch_data):
    scans.csv      every cell seen, one row per scan pass
    baseline.json  cells considered "known" (edit by hand to curate)
    alerts.log     human-readable alerts with timestamps
    raw/           raw grgsm_scanner output per pass, for the report

Stop with Ctrl-C (not kill -9, or GNU Radio leaks shm segments).
"""

import argparse, csv, json, os, re, signal, subprocess, sys, time
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

stop = False
def _sig(*_):
    global stop
    stop = True
signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def run_scanner(band, args, gain, timeout):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = ["grgsm_scanner", "--args", args, "-b", band, "-g", str(gain)]
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.stdout + "\n" + p.stderr
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + "\n[gsm_watch] scanner timed out\n"
    except FileNotFoundError:
        sys.exit("grgsm_scanner not found in PATH (are you inside a venv without system packages?)")


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


def undecoded(c):
    """True when the scanner clearly failed to read the SI messages (not a property of the cell)."""
    return c["cid"] == "0" or c["mcc"] == "0"


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
    ap.add_argument("-b", "--bands", nargs="+", default=["GSM900", "DCS1800"])
    ap.add_argument("--dir", default="gsm_watch_data")
    ap.add_argument("--interval", type=int, default=60, help="seconds to sleep between passes")
    ap.add_argument("--timeout", type=int, default=900, help="max seconds per scanner run")
    ap.add_argument("--learn", type=int, default=3,
                    help="passes during which new cells are added to baseline silently")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--capture-secs", type=int, default=0,
                    help="on alert, record this many seconds of raw I/Q at the cell's BCCH (0 = off)")
    ap.add_argument("--capture-rate", default="2M", help="sample rate for I/Q capture")
    ap.add_argument("--capture-max", type=int, default=20, help="max captures per run (disk guard)")
    a = ap.parse_args()

    os.makedirs(os.path.join(a.dir, "raw"), exist_ok=True)
    csv_path = os.path.join(a.dir, "scans.csv")
    base_path = os.path.join(a.dir, "baseline.json")
    alert_path = os.path.join(a.dir, "alerts.log")

    baseline = {}
    if os.path.exists(base_path):
        with open(base_path) as f:
            baseline = json.load(f)

    if a.baseline_only:
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                baseline.setdefault(key(row), {"first_seen": row["time"], "seen": 0})
                baseline[key(row)]["seen"] += 1
        json.dump(baseline, open(base_path, "w"), indent=1)
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
               "-g", str(a.gain), "-c", cfile, "-T", str(a.capture_secs)]
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
                       "core:hw": f"bladeRF 2.0 xA4, osmosdr args {a.args}, gain {a.gain}"},
            "captures": [{"core:sample_start": 0, "core:frequency": c["freq"] and float(c["freq"]) * 1e6,
                          "core:datetime": ts}],
            "annotations": [{"core:sample_start": 0, "gsm:arfcn": c["arfcn"], "gsm:mcc": c["mcc"],
                             "gsm:mnc": c["mnc"], "gsm:lac": c["lac"], "gsm:cid": c["cid"],
                             "gsm:pwr_dbm": c["pwr"], "gsm:flags": flags}],
        }
        with open(os.path.join(cap_dir, stem + ".sigmf-meta"), "w") as f:
            json.dump(meta, f, indent=1)
        captures_done += 1

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
            if stop:
                break
            ts = datetime.now().isoformat(timespec="seconds")
            print(f"[{ts}] pass {passno} scanning {band} ...", flush=True)
            out = run_scanner(band, a.args, a.gain, a.timeout)
            with open(os.path.join(a.dir, "raw", f"{ts.replace(':', '-')}_{band}.txt"), "w") as f:
                f.write(out)
            cells = parse(out)
            seen_now = set()
            odd_now = set()
            for c in cells:
                soft, hard = oddities(c, lac_ops)
                flags = list(hard)
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
                            if int(c["pwr"]) > -40:
                                flags.append(f"very strong for a new cell ({c['pwr']} dBm)")
                    baseline[k]["seen"] += 1
                    baseline[k]["last_seen"] = ts
                    baseline[k]["last_pwr"] = c["pwr"]
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
            # cells in baseline for this band that vanished this pass
            for k, v in baseline.items():
                if v.get("band") == band and k not in seen_now and v.get("last_seen") == v.get("last_seen_prev"):
                    pass  # could alert on disappearance; off by default, too noisy indoors
            for k in seen_now:
                baseline[k]["last_seen_prev"] = baseline[k]["last_seen"]
            json.dump(baseline, open(base_path, "w"), indent=1)
            print(f"   {len(cells)} cells decoded", flush=True)
        for _ in range(a.interval):
            if stop:
                break
            time.sleep(1)

    csv_f.close()
    print("stopped cleanly.")


if __name__ == "__main__":
    main()
