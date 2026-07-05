#!/usr/bin/env python3
"""Re-score the week-2 outputs on a system-agnostic basis.

The shipped week-2 `quality_score` mixed three things into one headline:
(1) objective surface stats (length, structure, citations, table, redundancy)
    that are computable identically for every system;
(2) the system's OWN verifier score (weight 0.25) — which only FlowerNet
    populates, so baselines score 0 on it by construction; and
(3) two bonuses gated on `startswith("flowernet")` / `== flowernet_full`.

(2) and (3) make cross-system comparison circular / unfair. This script
re-derives, from the already-stored per-document metrics, a fair table:
  * objective surface stats per system (true, comparable);
  * the old headline vs a surface-only score with (2)+(3) removed;
  * the internal verifier pass-rate reported SEPARATELY and labelled
    (FlowerNet-only, not comparable, and not a quality claim).

No LLM calls, no regeneration — pure recomputation from the outputs JSON.
"""
import json, os, statistics as st

DATA = os.path.join(os.path.dirname(__file__), "..", "results", "week2",
                    "week2_benchmark_outputs_full.json")

def surface_only(m):
    chars = float(m.get("chars", 0) or 0)
    headings = float(m.get("heading_count", 0) or 0)
    paras = float(m.get("paragraphs", 0) or 0)
    cites = float(m.get("citation_marker_count", 0) or 0)
    tables = float(m.get("table_marker_count", 0) or 0)
    rep = float(m.get("repeat_3gram_ratio", 0) or 0)
    length = min(1.0, chars / 6500.0)
    structure = min(1.0, (headings + paras / 5.0) / 10.0)
    evidence = min(1.0, cites / 12.0)
    table = min(1.0, tables / 40.0)
    penalty = min(0.25, rep * 0.35)
    # same weights as shipped, but WITHOUT the self-verifier term and the
    # flowernet-only bonuses; not renormalised, so the number is directly
    # comparable to the old headline minus exactly the removed pieces.
    return round(max(0.0, 0.24*length + 0.18*structure + 0.20*evidence
                     + 0.08*table + 0.05 - penalty), 4)

def main():
    rows = json.load(open(DATA))
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    agg = {}
    for r in rows:
        s = r.get("system"); m = r.get("metrics") or {}
        a = agg.setdefault(s, dict(chars=[], cites=[], head=[], rep=[],
                                   old=[], surf=[], strict=0, forced=0, exp=0, n=0))
        a["chars"].append(float(m.get("chars", 0) or 0))
        a["cites"].append(float(m.get("citation_marker_count", 0) or 0))
        a["head"].append(float(m.get("heading_count", 0) or 0))
        a["rep"].append(float(m.get("repeat_3gram_ratio", 0) or 0))
        a["old"].append(float(r.get("week2_score", 0) or 0))
        a["surf"].append(surface_only(m))
        a["strict"] += int(r.get("verified_subsections", 0) or 0)
        a["forced"] += int(r.get("forced_pass_subsections", 0) or 0)
        a["exp"] += int(r.get("expected_subsections", 0) or 0)
        a["n"] += 1

    order = ["flowernet_full", "flowernet_wo_bandit", "flowernet_wo_nli",
             "flowernet_wo_multidim", "flowernet_no_vc_budget20",
             "self_refine", "cogwriter_adapter", "arise_adapter", "vanilla_llm"]
    order = [s for s in order if s in agg] + [s for s in agg if s not in order]
    mean = lambda v: round(st.mean(v), 4) if v else float("nan")

    print("Fair re-score of week-2 outputs (mean over topics)\n")
    print(f"{'system':26} {'OLD q̄':>7} {'surface':>8} {'Δ':>7} | "
          f"{'chars':>6} {'cites':>6} {'redund%':>7} | verifier(internal)")
    print("-" * 96)
    for s in order:
        a = agg[s]
        old, surf = mean(a["old"]), mean(a["surf"])
        vpass = f"{a['strict']}/{a['exp']} strict, {a['forced']}/{a['exp']} forced" if a["exp"] else "n/a (baseline)"
        print(f"{s:26} {old:7.3f} {surf:8.3f} {old-surf:7.3f} | "
              f"{mean(a['chars']):6.0f} {mean(a['cites']):6.1f} "
              f"{mean(a['rep'])*100:6.1f}% | {vpass}")
    print("\nNotes:")
    print("  * 'surface' removes the self-verifier term (0.25) + flowernet-only")
    print("    bonuses. Δ = how much the OLD headline was inflated by those.")
    print("  * chars/cites/redund% are objective and comparable; they are NOT a")
    print("    quality claim. Real quality needs the independent-judge win-rate.")
    print("  * verifier(internal) shows the quality gate passed 0 strictly on")
    print("    every FlowerNet variant; all sub-sections were force-accepted.")

if __name__ == "__main__":
    main()
