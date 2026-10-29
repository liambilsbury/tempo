"""Score the single-level pre-registration's R1 and R3
    python3 benchmark/score/single_level/r1_r3.py r1 results/experiments/pc-kern16.json
    python3 benchmark/score/single_level/r1_r3.py r3 results/experiments/pc-r3.json results/experiments/pcn-r3.json
    python3 benchmark/score/single_level/r1_r3.py --selftest"""
import json, sys
from math import comb, erf, sqrt
from statistics import median

def sign_p(diffs):
    """return the two-sided sign test p of nonzero differences"""
    n = sum(1 for d in diffs if d != 0)
    if n == 0:
        return 1.0
    k = min(sum(d > 0 for d in diffs), sum(d < 0 for d in diffs))
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)

def mann_whitney_p(a, b):
    """return the two-sided Mann-Whitney p by normal approximation with tie correction"""
    allv = sorted((v, i) for i, v in enumerate(a + b))
    ranks = [0.0] * len(allv); i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[allv[k][1]] = (i + j) / 2 + 1
        i = j + 1
    n1, n2 = len(a), len(b)
    n = n1 + n2
    u = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    ties = sum(t ** 3 - t for t in _run_lengths([v for v, _ in allv]))
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        return 1.0
    z = abs(u - n1 * n2 / 2) / sqrt(var)
    return 2 * (1 - 0.5 * (1 + erf(z / sqrt(2))))

def _run_lengths(sorted_vals):
    """return the length of each run of equal sorted values"""
    runs, i = [], 0
    while i < len(sorted_vals):
        j = i
        while j + 1 < len(sorted_vals) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        runs.append(j - i + 1)
        i = j + 1
    return runs

def _num(v):
    """check that v is an int or float, excluding bool"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def _complete(r, arms, keys):
    """check that a round has every arm with numeric values for keys"""
    return isinstance(r, dict) and all(
        isinstance(r.get(a), dict) and all(_num(r[a].get(k)) for k in keys) for a in arms)

def _confirmed(rounds, arm):
    """return how many rounds have hit > 0 for an arm, as k/n"""
    k = sum(1 for r in rounds if isinstance(r, dict) and isinstance(r.get(arm), dict)
            and _num(r[arm].get("hit")) and r[arm]["hit"] > 0)
    return f"{k}/{len(rounds)}"

MIN_ROUNDS = 12
SINGLE, NESTED = "single-level", "nested"

def score_r1(d):
    """score R1a and R1b from the kern16 batch"""
    if d.get("topology") != SINGLE:
        return {"R1a": "VOID", "R1b": "VOID", "topology": d.get("topology", "-")}
    rounds = d.get("rounds")
    if not isinstance(rounds, list):
        return {"R1a": "VOID", "R1b": "VOID", "why": "no rounds list"}
    arms = ("pc-kern16-nonrt", "pc-kern16-rt")
    rs = [r for r in rounds if _complete(r, arms, ("max_ns", "p999_ns"))]
    seen = {"complete": f"{len(rs)}/{len(rounds)}",
            "injection_confirmed": ", ".join(f"{a.rsplit('-', 1)[1]} {_confirmed(rounds, a)}"
                                             for a in arms)}
    if len(rs) < MIN_ROUNDS:
        return {"R1a": "VOID", "R1b": "VOID", **seen}
    dmax = [r["pc-kern16-rt"]["max_ns"] - r["pc-kern16-nonrt"]["max_ns"] for r in rs]
    dp = [r["pc-kern16-rt"]["p999_ns"] - r["pc-kern16-nonrt"]["p999_ns"] for r in rs]
    up = sum(x > 0 for x in dmax)
    r1a = "HIT" if up >= 12 and sign_p(dmax) < 0.05 else "MISS"
    r1b = "HIT" if sign_p(dp) >= 0.05 else "MISS"
    return {"R1a": r1a, "R1b": r1b, "rt_max_higher": f"{up}/{len(rs)}",
            "p_max": round(sign_p(dmax), 4), "p_p999": round(sign_p(dp), 4), **seen}

def score_r3(single, nested):
    """score R3a from the single-level and nested hog batches"""
    if single.get("topology") != SINGLE or nested.get("topology") != NESTED:
        return {"R3a": "VOID", "topology": f"{single.get('topology', '-')}/{nested.get('topology', '-')}"}
    sr, nr = single.get("rounds"), nested.get("rounds")
    if not isinstance(sr, list) or not isinstance(nr, list):
        return {"R3a": "VOID", "why": "no rounds list"}
    s = [r["pc-r3-hog"]["max_ns"] for r in sr
         if _complete(r, ("pc-r3-control", "pc-r3-hog"), ("max_ns",))]
    n = [r["pcn-r3-hog"]["max_ns"] for r in nr
         if _complete(r, ("pcn-r3-control", "pcn-r3-hog"), ("max_ns",))]
    seen = {"complete": f"single {len(s)}/{len(sr)}, nested {len(n)}/{len(nr)}",
            "injection_confirmed": f"single {_confirmed(sr, 'pc-r3-hog')}, "
                                   f"nested {_confirmed(nr, 'pcn-r3-hog')}"}
    if len(s) < MIN_ROUNDS or len(n) < MIN_ROUNDS:
        return {"R3a": "VOID", **seen}
    s_med, n_med = median(s), median(n)
    if s_med == 0:
        return {"R3a": "VOID", "why": "zero single-level median", **seen}
    p = mann_whitney_p(s, n)
    r3a = "HIT" if n_med >= 3 * s_med and p < 0.05 else "MISS"
    return {"R3a": r3a, "nested_over_single_max": round(n_med / s_med, 2), "p": round(p, 4), **seen}

def _rounds(pairs, a, b):
    """build a two-arm single-level batch from value pairs"""
    return {"metric": "p999_ns", "topology": "single-level",
            "rounds": [{a: {"max_ns": x, "p999_ns": x, "late": 0},
                        b: {"max_ns": y, "p999_ns": y, "late": 0}} for x, y in pairs]}

def _hog(prefix, maxes, topology):
    """build a control and hog batch from hog maxima"""
    return {"topology": topology,
            "rounds": [{f"{prefix}-control": {"max_ns": 1}, f"{prefix}-hog": {"max_ns": m}}
                       for m in maxes]}

def selftest():
    """run the scoring cases and return 1 on any failure"""
    cases = []
    up = _rounds([(100, 130)] * 16, "pc-kern16-nonrt", "pc-kern16-rt")
    cases.append(("R1 HIT when rt max is higher in 16/16", score_r1(up)["R1a"] == "HIT"))
    down = _rounds([(130, 100)] * 16, "pc-kern16-nonrt", "pc-kern16-rt")
    cases.append(("R1 MISS when rt max is lower in 16/16", score_r1(down)["R1a"] == "MISS"))
    short = _rounds([(100, 130)] * 5, "pc-kern16-nonrt", "pc-kern16-rt")
    cases.append(("R1 VOID under 12 complete rounds", score_r1(short)["R1a"] == "VOID"))
    s = _hog("pc-r3", [3] * 16, "single-level")
    n = _hog("pcn-r3", [40] * 16, "nested")
    cases.append(("R3 HIT when nested hog max dwarfs single-level", score_r3(s, n)["R3a"] == "HIT"))
    same = _hog("pcn-r3", [3] * 16, "nested")
    cases.append(("R3 MISS when they match", score_r3(s, same)["R3a"] == "MISS"))
    s2 = _hog("pc-r3", [2] * 16, "single-level")
    n2 = _hog("pcn-r3", [4] * 8 + [6] * 8, "nested")
    cases.append(("R3 MISS at a 2.5x median whose upper-middle value is 3x",
                  score_r3(s2, n2)["R3a"] == "MISS"))
    wrong = _hog("pcn-r3", [40] * 16, "single-level")
    cases.append(("R3 VOID when the nested batch is stamped single-level",
                  score_r3(s, wrong)["R3a"] == "VOID"))
    unstamped = {"rounds": n["rounds"]}
    cases.append(("R3 VOID when the nested batch carries no topology",
                  score_r3(s, unstamped)["R3a"] == "VOID"))
    z = {"topology": "single-level", "rounds": [{"pc-r3-control": {"max_ns": 1}, "pc-r3-hog": {"max_ns": 0}}] * 16}
    nn = {"topology": "nested", "rounds": [{"pcn-r3-control": {"max_ns": 1}, "pcn-r3-hog": {"max_ns": 5}}] * 16}
    cases.append(("R3 VOID on a zero single-level median", score_r3(z, nn)["R3a"] == "VOID"))
    half = {"topology": "single-level", "rounds": [{"pc-r3-hog": {"max_ns": 3}}] * 16}
    cases.append(("R3 VOID when control never ran", score_r3(half, nn)["R3a"] == "VOID"))
    up_nested = dict(up, topology="nested")
    cases.append(("R1 VOID when the batch is stamped nested", score_r1(up_nested)["R1a"] == "VOID"))
    pt = mann_whitney_p([1] * 8 + [2] * 8, [2] * 8 + [3] * 8)
    cases.append(("Mann-Whitney applies the tie correction", 7.5e-5 < pt < 9.0e-5))
    cases.append(("Mann-Whitney is 1 when every value ties", mann_whitney_p([3] * 16, [3] * 16) == 1.0))

    def safe(f):
        """return f's result, or the name of the exception it raised"""
        try:
            return f()
        except Exception as e:
            return f"raised {type(e).__name__}"
    dash = _rounds([(100, 130)] * 16, "pc-kern16-nonrt", "pc-kern16-rt")
    for r in dash["rounds"][:5]:
        r["pc-kern16-rt"]["max_ns"] = "-"
    cases.append(("R1 VOID when '-' leaves 11 complete rounds",
                  safe(lambda: score_r1(dash)["R1a"]) == "VOID"))
    dash2 = _rounds([(100, 130)] * 16, "pc-kern16-nonrt", "pc-kern16-rt")
    for r in dash2["rounds"][:2]:
        r["pc-kern16-nonrt"]["p999_ns"] = "-"
    cases.append(("R1 scores the 14 numeric rounds when '-' voids two",
                  safe(lambda: (score_r1(dash2)["R1a"], score_r1(dash2)["rt_max_higher"],
                                score_r1(dash2)["complete"])) == ("HIT", "14/14", "14/16")))
    sdash = _hog("pc-r3", [3] * 16, "single-level")
    for r in sdash["rounds"][:5]:
        r["pc-r3-hog"]["max_ns"] = "-"
    cases.append(("R3 VOID when '-' leaves 11 complete hog rounds",
                  safe(lambda: score_r3(sdash, n)["R3a"]) == "VOID"))
    cases.append(("R3 VOID when a batch has no rounds list", safe(lambda: score_r3({"topology": SINGLE}, n)["R3a"]) == "VOID"))
    cases.append(("r3 with one path is a usage error", safe(lambda: main(["r3", "only-one.json"])) == 2))
    cases.append(("r1 with two paths is a usage error", safe(lambda: main(["r1", "a.json", "b.json"])) == 2))
    cases.append(("an unknown batch is a usage error", safe(lambda: main(["r9", "a.json"])) == 2))
    cases.append(("an unreadable file is VOID, exit 1", safe(lambda: main(["r1", "/nonexistent/pc-kern16.json"])) == 1))

    hit_up = _rounds([(100, 130)] * 16, "pc-kern16-nonrt", "pc-kern16-rt")
    for i, r in enumerate(hit_up["rounds"]):
        r["pc-kern16-nonrt"]["hit"] = 500
        r["pc-kern16-rt"]["hit"] = 0 if i < 3 else 700
    rep = safe(lambda: score_r1(hit_up))
    cases.append(("R1 reports injection confirmed per arm, and scores the same",
                  isinstance(rep, dict) and rep.get("injection_confirmed") == "nonrt 16/16, rt 13/16"
                  and rep.get("R1a") == "HIT"))
    rep0 = safe(lambda: score_r1(_rounds([(100, 130)] * 16, "pc-kern16-nonrt", "pc-kern16-rt")))
    cases.append(("R1 with no hit field reports 0/16 confirmed",
                  isinstance(rep0, dict) and rep0.get("injection_confirmed") == "nonrt 0/16, rt 0/16"))
    sh = _hog("pc-r3", [3] * 16, "single-level")
    nh = _hog("pcn-r3", [40] * 16, "nested")
    for r in sh["rounds"]:
        r["pc-r3-hog"]["hit"] = 9
    for r in nh["rounds"][:15]:
        r["pcn-r3-hog"]["hit"] = "-"
    rep3 = safe(lambda: score_r3(sh, nh))
    cases.append(("R3 reports hog-arm injection confirmed per batch, and scores the same",
                  isinstance(rep3, dict) and rep3.get("injection_confirmed") == "single 16/16, nested 0/16"
                  and rep3.get("R3a") == "HIT"))
    bad = [c for c, ok in cases if not ok]
    for c, ok in cases:
        print(("ok   " if ok else "FAIL ") + c)
    print(f"pc_score selftest: {len(cases) - len(bad)}/{len(cases)}")
    return 1 if bad else 0

def main(argv=None):
    """parse the arguments and print a batch's score"""
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    want = {"r1": 1, "r3": 2}
    if not argv or argv[0] not in want or len(argv) - 1 != want[argv[0]]:
        print("usage:\n" + __doc__.split("\n", 1)[1].rstrip(), file=sys.stderr)
        return 2
    which, paths = argv[0], argv[1:]
    try:
        data = [json.load(open(p)) for p in paths]
    except (OSError, ValueError) as e:
        print(f"{which.upper()}: VOID -- cannot read {e}")
        return 1
    if not all(isinstance(x, dict) for x in data):
        print(f"{which.upper()}: VOID -- not a paired batch")
        return 1
    res = score_r1(*data) if which == "r1" else score_r3(*data)
    for k, v in res.items():
        print(f"{k}: {v}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
