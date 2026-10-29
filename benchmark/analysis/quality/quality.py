#!/usr/bin/env python3
"""Measure each quality tier's cost and error

Usage: quality.py [--slopes N] [--acts N] [--freeze] [--selftest] [--dry-run]
  --freeze     also write the frozen snapshot"""

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BIN = ROOT / "build" / "pipeline"
FROZEN = ROOT / "results" / "earlier_phase" / "frozen" / "quality-frozen.json"

TIERS = 4
TAG_V, TAG_U = 1, 2
M32 = 0xFFFFFFFF

RELERR_TOL = 2e-4


def reconstruct_hash(tag, i, j):
    """hash a tag and index pair to 32 bits as reconstruct.h does"""
    x = (tag * 0x9E3779B1 + i * 0x85EBCA77 + j * 0xC2B2AE3D + 0x27D4EB2F) & M32
    x ^= x >> 16
    x = (x * 0x7FEB352D) & M32
    x ^= x >> 15
    x = (x * 0x846CA68B) & M32
    x ^= x >> 16
    return x


def reconstruct_val(tag, i, j):
    """map the hash to a value in [-1, 1)"""
    return reconstruct_hash(tag, i, j) * (2.0 / 4294967296.0) - 1.0


def auto_modes(slopes, acts):
    """return the full mode count for a geometry"""
    m = (acts * slopes) // (slopes + acts)
    return max(m, TIERS)


def tier_modes(modes, tier):
    """return the modes a tier keeps"""
    return max(modes >> tier, 1)


def stimulus(n, seq):
    """return frame seq's slope vector of length n"""
    return [((seq + i) % 1024) * 0.001 for i in range(n)]


def build(slopes, acts, modes, alpha):
    """build the operator's V, U and singular values"""
    vt = [[reconstruct_val(TAG_V, k, i) for i in range(slopes)] for k in range(modes)]
    u = [[reconstruct_val(TAG_U, k, a) for a in range(acts)] for k in range(modes)]
    s = [math.pow(k + 1, -alpha) for k in range(modes)]
    return vt, u, s


def apply(vt, u, s, acts, m, x):
    """apply the first m modes of the operator to x"""
    cmd = [0.0] * acts
    for k in range(m):
        v = vt[k]
        c = s[k] * sum(v[i] * x[i] for i in range(len(x)))
        uk = u[k]
        for a in range(acts):
            cmd[a] += c * uk[a]
    return cmd


def relerr(vt, u, s, acts, modes, tier, nstim, slopes):
    """return a tier's relative error against the full rank over nstim stimuli"""
    num = den = 0.0
    full = tier_modes(modes, 0)
    kept = tier_modes(modes, tier)
    for f in range(nstim):
        x = stimulus(slopes, f)
        ref = apply(vt, u, s, acts, full, x)
        got = apply(vt, u, s, acts, kept, x)
        for a in range(acts):
            d = got[a] - ref[a]
            num += d * d
            den += ref[a] * ref[a]
    return math.sqrt(num / den) if den > 0 else 0.0


OBS = re.compile(r"(\w+)=([-\d.]+)")


def run_c(slopes, acts, modes, alpha, reps, stim, binary=BIN, curve=False):
    """run the binary's quality stage and parse its OBS and CURVE lines"""
    argv = [str(binary), "--stage", "quality", "--tag", "qual",
            "--slopes", str(slopes), "--acts", str(acts), "--modes", str(modes),
            "--alpha", str(alpha), "--frames", str(reps), "--stim", str(stim)]
    if curve:
        argv += ["--curve"]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise SystemExit(f"quality: {binary} exited {r.returncode}\n{r.stderr}")
    out, pts = {}, {}
    for line in r.stdout.splitlines():
        if line.startswith("OBS "):
            for k, v in OBS.findall(line[4:]):
                out[k] = float(v) if "." in v else int(v)
        elif line.startswith("CURVE "):
            _, m, e = line.split()
            pts[int(m)] = float(e)
    if pts:
        out["_curve"] = [pts[m] for m in sorted(pts)]
    missing = [f"tier{t}_relerr" for t in range(TIERS) if f"tier{t}_relerr" not in out]
    if missing:
        raise SystemExit(f"quality: the binary reported no {', '.join(missing)}")
    return out


def check(slopes, acts, alpha, reps, stim_pub, stim_check, binary=BIN, quiet=False,
          block=16):
    """return the tier table, or exit naming what disagreed"""
    modes = auto_modes(slopes, acts)
    published = run_c(slopes, acts, modes, alpha, reps, stim_pub, binary, curve=True)
    cross = run_c(slopes, acts, modes, alpha, 1, stim_check, binary)

    if published["quality_modes"] != modes:
        raise SystemExit(
            f"quality: the binary chose {published['quality_modes']} modes where this "
            f"implementation of the same rule chose {modes}")

    vt, u, s = build(slopes, acts, modes, alpha)

    rows, worst = [], 0.0
    for t in range(TIERS):
        mine = relerr(vt, u, s, acts, modes, t, stim_check, slopes)
        theirs = cross[f"tier{t}_relerr"]
        d = abs(mine - theirs)
        worst = max(worst, d)
        if d > RELERR_TOL:
            raise SystemExit(
                f"quality: tier {t} relative error {theirs:.9f} from the binary, {mine:.9f} "
                f"rebuilt; apart by {d:.2e} (tolerance {RELERR_TOL:.0e})")
        rows.append({
            "tier": t,
            "modes": published[f"tier{t}_modes"],
            "macs": published[f"tier{t}_macs"],
            "min_ns": published[f"tier{t}_min_ns"],
            "p50_ns": published[f"tier{t}_p50_ns"],
            "p99_ns": published[f"tier{t}_p99_ns"],
            "cost_ratio": published[f"tier{t}_cost_ratio"],
            "mac_ratio": published[f"tier{t}_macs"] / published["tier0_macs"],
            "relerr": published[f"tier{t}_relerr"],
        })

    for a, b in zip(rows, rows[1:]):
        if b["macs"] >= a["macs"]:
            raise SystemExit(f"quality: tier {b['tier']} does not cost less than tier {a['tier']}")
        if b["relerr"] <= a["relerr"]:
            raise SystemExit(f"quality: tier {b['tier']} is not worse than tier {a['tier']}")

    sys.path.insert(0, str(ROOT))
    from benchmark import lib
    host = lib.check_exclusive()

    curve = published.get("_curve")
    worst_mode_step = 0.0
    if curve is not None:
        if len(curve) != modes + 1:
            raise SystemExit(f"quality: the curve has {len(curve)} points for {modes} modes")
        if abs(curve[modes]) > 1e-12:
            raise SystemExit(f"quality: stopping at every mode is not the reference: "
                             f"curve[{modes}]={curve[modes]}")
        for r in rows:
            c = curve[r["modes"]]
            if abs(c - r["relerr"]) > RELERR_TOL:
                raise SystemExit(
                    f"quality: tier {r['tier']} at {r['modes']} modes reports error "
                    f"{r['relerr']:.9f}, the curve says {c:.9f}")
        step = [b - a for a, b in zip(curve, curve[1:])]
        worst_mode_step = max(step) if step else 0.0
        stops = list(range(block, modes, block)) + [modes]
        for m0, m1 in zip(stops, stops[1:]):
            if curve[m1] > curve[m0]:
                raise SystemExit(
                    f"quality: stopping at {m1} modes is worse than at {m0} "
                    f"({curve[m1]:.9f} > {curve[m0]:.9f})")

    table = {
        "slopes": slopes, "acts": acts, "modes": modes, "alpha": alpha,
        "reps": reps, "stim_published": stim_pub, "stim_crosschecked": stim_check,
        "dense_macs": published["quality_dense_macs"],
        "relerr_max_disagreement": worst,
        "relerr_curve": curve,
        "block": block,
        "worst_single_mode_step": worst_mode_step if curve else None,
        "verdict": "GREEN" if host["exclusive"] else "RED",
        "verdict_reason": host["why"],
        "host_exclusive": host["exclusive"],
        "load1": host["load1"],
        "repo_sha": lib.sha(),
        "tiers": rows,
    }
    if not quiet:
        print(f"geometry slopes={slopes} acts={acts} modes={modes} alpha={alpha}  "
              f"dense would be {published['quality_dense_macs']} macs, tier 0 is "
              f"{rows[0]['macs']}")
        print(f"{'tier':>4} {'modes':>6} {'macs':>9} {'cost/t0':>8} {'macs/t0':>8} "
              f"{'p50_ns':>9} {'rel err':>9}")
        for r in rows:
            print(f"{r['tier']:>4} {r['modes']:>6} {r['macs']:>9} {r['cost_ratio']:>8.4f} "
                  f"{r['mac_ratio']:>8.4f} {r['p50_ns']:>9} {r['relerr']:>9.6f}")
        print(f"cross-check over {stim_check} stimuli in double precision: "
              f"worst disagreement {worst:.2e} (tolerance {RELERR_TOL:.0e})")
        print(f"host {table['verdict']}: {table['verdict_reason']} (load1 {table['load1']:.2f}) "
              f"-- the error column is arithmetic, the ns columns are not")
    return table


def selftest():
    """make each refusal in check fire on a constructed input"""
    import tempfile
    failures = []

    def expect_refusal(what, fn):
        """record a failure if fn does not exit"""
        try:
            fn()
        except SystemExit as e:
            print(f"  refused {what}: {str(e)[:100]}")
            return
        failures.append(what)
        print(f"  ACCEPTED {what} -- the check does not fire")

    orig = globals()["reconstruct_hash"]

    def drifted(tag, i, j):
        """hash as reconstruct_hash does with a constant changed"""
        x = (tag * 0x9E3779B1 + i * 0x85EBCA77 + j * 0xC2B2AE3D + 0x27D4EB2F) & M32
        x ^= x >> 16
        x = (x * 0x7FEB352F) & M32
        x ^= x >> 15
        x = (x * 0x846CA68B) & M32
        x ^= x >> 16
        return x

    globals()["reconstruct_hash"] = drifted
    expect_refusal("a rebuilt operator that does not match the binary",
                   lambda: check(200, 50, 1.0, 200, 4, 4, quiet=True))
    globals()["reconstruct_hash"] = orig

    fake = Path(tempfile.mkdtemp()) / "flat"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('OBS quality_modes=40'); print('OBS quality_dense_macs=10000')\n"
        "for t in range(4):\n"
        "    print(f'OBS tier{t}_modes=40 tier{t}_macs=10000 tier{t}_min_ns=1 "
        "tier{t}_p50_ns=1 tier{t}_p99_ns=1 tier{t}_cost_ratio=1.0 tier{t}_relerr=0.0')\n")
    fake.chmod(0o755)
    expect_refusal("a table whose tiers all cost the same",
                   lambda: check(200, 50, 1.0, 200, 4, 4, binary=fake, quiet=True))

    empty = fake.parent / "empty"
    empty.write_text("#!/usr/bin/env python3\nprint('OBS quality_modes=40')\n")
    empty.chmod(0o755)
    expect_refusal("a binary that reports no tiers",
                   lambda: check(200, 50, 1.0, 200, 4, 4, binary=empty, quiet=True))

    wrong = fake.parent / "wrongrank"
    wrong.write_text(
        "#!/usr/bin/env python3\n"
        "print('OBS quality_modes=41'); print('OBS quality_dense_macs=10000')\n"
        "for t in range(4):\n"
        "    print(f'OBS tier{t}_modes=41 tier{t}_macs={10000>>t} tier{t}_min_ns=1 "
        "tier{t}_p50_ns=1 tier{t}_p99_ns=1 tier{t}_cost_ratio=1.0 tier{t}_relerr={t*0.01}')\n")
    wrong.chmod(0o755)
    expect_refusal("a binary that chose a different full rank",
                   lambda: check(200, 50, 1.0, 200, 4, 4, binary=wrong, quiet=True))

    real = subprocess.run(
        [str(BIN), "--stage", "quality", "--tag", "qual", "--slopes", "200", "--acts", "50",
         "--modes", "40", "--alpha", "1.0", "--frames", "200", "--stim", "4", "--curve"],
        capture_output=True, text=True, timeout=600).stdout
    lines = []
    for line in real.splitlines():
        if line.startswith("CURVE "):
            _, m, e = line.split()
            lines.append(f"CURVE {m} {0.9 if m == '24' else e}")
        else:
            lines.append(line)
    nonmono = fake.parent / "nonmono"
    nonmono.write_text("#!/usr/bin/env python3\nprint(r\"\"\"" + "\n".join(lines) + "\"\"\")\n")
    nonmono.chmod(0o755)
    expect_refusal("a curve where more modes deliver a worse command",
                   lambda: check(200, 50, 1.0, 200, 4, 4, binary=nonmono, quiet=True, block=8))

    if failures:
        print(f"selftest: {len(failures)} check(s) did not fire: {failures}")
        return 1
    print("selftest: every refusal fired")
    return 0


def main():
    """parse arguments and run the selftest, the dry run or the check"""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--slopes", type=int, default=2400)
    p.add_argument("--acts", type=int, default=600)
    p.add_argument("--alpha", type=float, default=1.0)
    p.add_argument("--reps", type=int, default=2000,
                   help="timed applies per tier")
    p.add_argument("--stim", type=int, default=1024,
                   help="stimuli the error is averaged over")
    p.add_argument("--check-stim", type=int, default=2,
                   help="stimuli the double-precision rebuild checks")
    p.add_argument("--block", type=int, default=16,
                   help="modes between the loop's clock reads")
    p.add_argument("--freeze", action="store_true",
                   help=f"write {FROZEN.name}")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="check the arguments and binary only")
    a = p.parse_args()

    if a.selftest:
        return selftest()
    if a.dry_run:
        bad = [f"--{n} must be positive" for n, v in (("slopes", a.slopes), ("acts", a.acts), ("reps", a.reps), ("stim", a.stim),
                                                      ("check-stim", a.check_stim), ("block", a.block)) if v < 1]
        for b in bad:
            print(f"quality: REFUSED -- {b}", file=sys.stderr)
        print(f"quality: dry run -- {a.reps} reps per tier at {a.slopes} x {a.acts}, stim {a.stim}, "
              f"check-stim {a.check_stim}, block {a.block}; nothing was timed")
        if not BIN.exists():
            print(f"quality: host prerequisite you supply -- {BIN} is not built (make)")
        return 1 if bad else 0
    if not BIN.exists():
        raise SystemExit(f"quality: {BIN} is not built")

    table = check(a.slopes, a.acts, a.alpha, a.reps, a.stim, a.check_stim, block=a.block)
    if a.freeze:
        from benchmark import lib
        topo = lib.host_topology()
        if topo["topology"] not in lib.PUBLISHED_TOPOLOGIES:
            raise SystemExit(f"quality: host topology is {topo['topology']} ({topo['why']}); "
                             f"refusing to freeze the published {FROZEN.name}")
        FROZEN.parent.mkdir(exist_ok=True)
        FROZEN.write_text(json.dumps(table, indent=2) + "\n")
        print(f"froze {FROZEN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
