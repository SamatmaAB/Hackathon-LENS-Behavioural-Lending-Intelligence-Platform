#!/usr/bin/env python3
"""
verify_lens_build.py
=====================
One-shot verification script for the LENS hackathon build.

Checks TWO layers:
  1. STATIC  — the right files/functions/endpoints actually exist in the repo
               (catches "the agent said it did X but didn't").
  2. RUNTIME — the live server returns correct, internally-consistent data
               (catches "it exists but is broken or returns garbage").

Usage:
    pip install requests --break-system-packages   # if not already installed
    python verify_lens_build.py --repo /path/to/repo --base-url http://localhost:8000

If --base-url is omitted, only the static checks run (no server needed).
If the server isn't already running, start it first:
    cd <repo> && python -m uvicorn backend.app:app --port 8000
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: this script needs the `requests` package.")
    print("Run: pip install requests --break-system-packages")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
class Results:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.skipped = []

    def ok(self, name, detail=""):
        self.passed.append(name)
        print(f"  [PASS]  {name}" + (f"  - {detail}" if detail else ""))

    def fail(self, name, detail=""):
        self.failed.append(name)
        print(f"  [FAIL]  {name}" + (f"  - {detail}" if detail else ""))

    def skip(self, name, detail=""):
        self.skipped.append(name)
        print(f"  [SKIP]  {name}" + (f"  - {detail}" if detail else ""))

    def summary(self):
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print("\n" + "=" * 70)
        print(f"SUMMARY: {len(self.passed)} passed, {len(self.failed)} failed, "
              f"{len(self.skipped)} skipped, {total} total")
        print("=" * 70)
        if self.failed:
            print("\nFailed checks:")
            for f in self.failed:
                print(f"  - {f}")
        return len(self.failed) == 0


R = Results()


# ---------------------------------------------------------------------------
# STATIC CHECKS — files, functions, endpoints actually present in source
# ---------------------------------------------------------------------------
def check_frontend_public_identical(repo: Path):
    print("\n--- Static: frontend/index.html vs public/index.html ---")
    fe = repo / "frontend" / "index.html"
    pub = repo / "public" / "index.html"
    if not fe.exists() or not pub.exists():
        R.fail("frontend/public files exist", f"missing {fe if not fe.exists() else pub}")
        return
    if fe.read_text(encoding="utf-8") == pub.read_text(encoding="utf-8"):
        R.ok("frontend/index.html and public/index.html are byte-identical")
    else:
        R.fail("frontend/index.html and public/index.html are byte-identical",
               "run: cp frontend/index.html public/index.html")


def check_frontend_components(repo: Path):
    print("\n--- Static: required React components present in frontend/index.html ---")
    fe = repo / "frontend" / "index.html"
    if not fe.exists():
        R.fail("frontend/index.html exists")
        return
    content = fe.read_text(encoding="utf-8")

    required_components = [
        "TriggerContributionChart",
        "LeadFunnelChart",
        "TrustScoreWaterfall",
        "TrustGauge",
        "SegmentationBubbleChart",
        "CashflowSankey",
        "TransactionCalendarHeatmap",
        "BehaviorRadarChart",
        "GeoDistributionMap",
        "RoiWaterfallChart",
        "FraudRiskScatter",
        "OutreachTimeline",
        "AuditTrailTimeline",
        "ComparisonRadarChart",
    ]
    for comp in required_components:
        # must be both defined (function ComponentName) AND used (<ComponentName)
        defined = re.search(rf"function\s+{comp}\s*\(", content)
        used = re.search(rf"<{comp}[\s/>]", content)
        if defined and used:
            R.ok(f"{comp} defined and rendered")
        elif defined and not used:
            R.fail(f"{comp} defined but never rendered", "component exists but isn't wired into any view")
        else:
            R.fail(f"{comp} defined", "function not found in frontend/index.html")


def check_backend_files(repo: Path):
    print("\n--- Static: backend files and known fixes ---")
    backend = repo / "backend"

    # geo.py exists with CITY_COORDS and build_geo_distribution
    geo_py = backend / "geo.py"
    if geo_py.exists():
        content = geo_py.read_text(encoding="utf-8")
        if "CITY_COORDS" in content and "def build_geo_distribution" in content:
            R.ok("backend/geo.py exists with CITY_COORDS + build_geo_distribution")
        else:
            R.fail("backend/geo.py has expected contents", "missing CITY_COORDS or build_geo_distribution")
    else:
        R.fail("backend/geo.py exists")

    # semantic_classifier.py retry-storm fix
    sem_py = backend / "semantic_classifier.py"
    if sem_py.exists():
        content = sem_py.read_text(encoding="utf-8")
        if "_model_load_failed" in content:
            R.ok("semantic_classifier.py retry-storm fix applied (_model_load_failed guard present)")
        else:
            R.fail("semantic_classifier.py retry-storm fix applied",
                   "_model_load_failed guard not found — model reload spam may still occur")
    else:
        R.fail("backend/semantic_classifier.py exists")

    # app.py: dead on_event handler removed
    app_py = backend / "app.py"
    if app_py.exists():
        content = app_py.read_text(encoding="utf-8")
        if '@app.on_event("startup")' in content:
            R.fail("dead @app.on_event('startup') handler removed from app.py",
                   "deprecated decorator still present — should have been deleted")
        else:
            R.ok("dead @app.on_event('startup') handler removed from app.py")

        # required new/modified endpoints
        required_routes = [
            ('/api/governance/geo-distribution', "geo-distribution endpoint"),
            ('/api/leads/segmentation', "leads/segmentation endpoint"),
            ('/api/governance/audit-trail', "audit-trail endpoint"),
        ]
        for needle, label in required_routes:
            if needle in content:
                R.ok(f"{label} route present in app.py")
            else:
                R.fail(f"{label} route present in app.py", f"could not find `{needle}`")

        # lead_detail exposes trust sub-scores + cashflow breakdown
        if 'lead["income_confidence"]' in content and 'lead["repay_score"]' in content:
            R.ok("lead_detail exposes income_confidence + repay_score")
        else:
            # Let's also check single quote just in case
            if "lead['income_confidence']" in content and "lead['repay_score']" in content:
                R.ok("lead_detail exposes income_confidence + repay_score")
            else:
                R.fail("lead_detail exposes income_confidence + repay_score")

        if "cashflow_breakdown" in content:
            R.ok("lead_detail exposes cashflow_breakdown")
        else:
            R.fail("lead_detail exposes cashflow_breakdown")

        if '"all_leads"' in content or "'all_leads'" in content:
            R.ok("anomalies endpoint returns all_leads (not just flagged)")
        else:
            R.fail("anomalies endpoint returns all_leads (not just flagged)")
    else:
        R.fail("backend/app.py exists")

    # engine.py has build_cashflow_breakdown
    engine_py = backend / "engine.py"
    if engine_py.exists():
        content = engine_py.read_text(encoding="utf-8")
        if "def build_cashflow_breakdown" in content:
            R.ok("engine.py has build_cashflow_breakdown")
        else:
            R.fail("engine.py has build_cashflow_breakdown")
    else:
        R.fail("backend/engine.py exists")


def check_test_files(repo: Path):
    print("\n--- Static: test files present ---")
    tests = repo / "tests"
    for fname in ["test_engine.py", "test_capacity.py", "test_governance.py"]:
        f = tests / fname
        if f.exists():
            R.ok(f"tests/{fname} exists")
        else:
            R.fail(f"tests/{fname} exists")


def run_pytest(repo: Path):
    print("\n--- Static: running pytest suite ---")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            cwd=str(repo), capture_output=True, text=True, timeout=180,
        )
        tail = "\n".join(result.stdout.strip().splitlines()[-5:])
        if result.returncode == 0:
            R.ok("pytest tests/ -q", tail.replace("\n", " | "))
        else:
            R.fail("pytest tests/ -q", tail.replace("\n", " | "))
    except Exception as e:
        R.fail("pytest tests/ -q", f"error running pytest: {e}")


# ---------------------------------------------------------------------------
# RUNTIME CHECKS — hit the live server, validate real responses
# ---------------------------------------------------------------------------
def runtime_checks(base_url: str):
    print(f"\n--- Runtime: checking live server at {base_url} ---")
    s = requests.Session()

    # 1. Health check
    try:
        r = s.get(f"{base_url}/api/health", timeout=10)
        if r.status_code == 200:
            R.ok("GET /api/health", r.json())
        else:
            R.fail("GET /api/health", f"status {r.status_code}")
            return  # no point continuing if server isn't even up
    except Exception as e:
        R.fail("GET /api/health", f"connection error: {e}")
        return

    # 2. Login
    try:
        r = s.post(f"{base_url}/api/auth/login", json={
            "email": "admin@idbibank.com", "password": "idbi@12345"
        }, timeout=10)
        if r.status_code == 200 and "token" in r.json():
            token = r.json()["token"]
            R.ok("POST /api/auth/login (seeded admin)")
        else:
            R.fail("POST /api/auth/login (seeded admin)", f"status {r.status_code}: {r.text[:200]}")
            return
    except Exception as e:
        R.fail("POST /api/auth/login (seeded admin)", str(e))
        return

    headers = {"Authorization": f"Bearer {token}"}

    # 3. Generate a fresh dataset (deterministic seed)
    try:
        r = s.post(f"{base_url}/api/generate", params={"seed": 42, "n_customers": 60}, headers=headers, timeout=60)
        if r.status_code == 200:
            gen = r.json()
            R.ok("POST /api/generate", f"{gen.get('total_leads')} leads from {gen.get('total_customers')} customers")
        else:
            R.fail("POST /api/generate", f"status {r.status_code}: {r.text[:200]}")
            return
    except Exception as e:
        R.fail("POST /api/generate", str(e))
        return

    # 4. Stats + funnel data sanity
    try:
        r = s.get(f"{base_url}/api/stats", headers=headers, timeout=10)
        stats = r.json()
        required_keys = ["total_customers", "total_leads", "tier_distribution", "lead_conversion_rate_pct"]
        missing = [k for k in required_keys if k not in stats]
        if r.status_code == 200 and not missing:
            tiers = stats["tier_distribution"]
            tier_sum = tiers.get("Tier 1", 0) + tiers.get("Tier 2", 0) + tiers.get("Tier 3", 0)
            if tier_sum == stats["total_leads"]:
                R.ok("GET /api/stats — funnel data consistent",
                     f"customers={stats['total_customers']} leads={stats['total_leads']} tiers={tiers}")
            else:
                R.fail("GET /api/stats — funnel data consistent",
                       f"tier counts sum to {tier_sum} but total_leads={stats['total_leads']}")
        else:
            R.fail("GET /api/stats", f"status {r.status_code}, missing keys: {missing}")
    except Exception as e:
        R.fail("GET /api/stats", str(e))

    # 5. Lead list + lead detail (trigger contributions, waterfall, sankey, capacity)
    lead_id = None
    try:
        r = s.get(f"{base_url}/api/leads", params={"limit": 5}, headers=headers, timeout=10)
        leads = r.json()
        if r.status_code == 200 and leads:
            lead_id = leads[0]["customer_id"]
            R.ok("GET /api/leads", f"{len(leads)} leads returned")
        else:
            R.fail("GET /api/leads", f"status {r.status_code}, empty={not leads}")
    except Exception as e:
        R.fail("GET /api/leads", str(e))

    if lead_id:
        try:
            r = s.get(f"{base_url}/api/leads/{lead_id}", headers=headers, timeout=10)
            detail = r.json()
            lead = detail.get("lead", {})

            # -- trigger contributions (item 1) --
            triggers = lead.get("triggers_fired", [])
            if triggers and all("contribution" in t for t in triggers):
                total_contrib = sum(t["contribution"] for t in triggers)
                R.ok("Lead detail: trigger contributions present", f"{len(triggers)} triggers, sum={total_contrib:.1f}%")
            else:
                R.fail("Lead detail: trigger contributions present")

            # -- waterfall sub-scores (item 3) --
            income_conf = lead.get("income_confidence")
            repay_score = lead.get("repay_score")
            intent = lead.get("intent_score")
            trust = lead.get("trust_score")
            if income_conf is not None and repay_score is not None and intent is not None and trust is not None:
                recomputed = round(intent * 0.4 + income_conf * 0.3 + repay_score * 0.3, 1)
                # allow slack for SENTRY's -15 anomaly dock
                if abs(recomputed - trust) <= 15.1:
                    R.ok("Lead detail: waterfall sub-scores present and consistent",
                         f"recomputed={recomputed} vs stored trust_score={trust}")
                else:
                    R.fail("Lead detail: waterfall sub-scores consistent with trust_score",
                           f"recomputed={recomputed} vs stored trust_score={trust} (diff too large)")
            else:
                R.fail("Lead detail: waterfall sub-scores present",
                       "income_confidence/repay_score/intent_score/trust_score missing")

            # -- capacity / gauge / radar dependency (dti_ratio) --
            capacity = detail.get("capacity")
            if capacity and "dti_ratio" in capacity and "recommended_eligible_amount" in capacity:
                R.ok("Lead detail: capacity object present (feeds gauge + radar + bubble chart)")
            else:
                R.fail("Lead detail: capacity object present")

            # -- cashflow sankey (item 6) --
            cashflow = detail.get("cashflow_breakdown")
            if cashflow and cashflow.get("total_income", 0) > 0 and cashflow.get("buckets"):
                bucket_sum = sum(cashflow["buckets"].values())
                R.ok("Lead detail: cashflow_breakdown present", f"total_income={cashflow['total_income']}, buckets_sum={bucket_sum:.0f}")
            else:
                R.fail("Lead detail: cashflow_breakdown present")

            # -- transactions (feeds calendar heatmap) --
            txns = detail.get("transactions", [])
            if txns and all("timestamp" in t for t in txns):
                R.ok("Lead detail: transactions with timestamps present (feeds calendar heatmap)", f"{len(txns)} txns")
            else:
                R.fail("Lead detail: transactions with timestamps present")

        except Exception as e:
            R.fail("GET /api/leads/{customer_id}", str(e))
    else:
        R.skip("GET /api/leads/{customer_id} checks", "no lead_id available from previous step")

    # 6. Segmentation endpoint (item 5)
    try:
        r = s.get(f"{base_url}/api/leads/segmentation", headers=headers, timeout=30)
        seg = r.json()
        if r.status_code == 200 and seg and all("eligible_amount" in s_ for s_ in seg):
            R.ok("GET /api/leads/segmentation", f"{len(seg)} segments returned")
        else:
            R.fail("GET /api/leads/segmentation", f"status {r.status_code}, empty or malformed")
    except Exception as e:
        R.fail("GET /api/leads/segmentation", str(e))

    # 7. Geo distribution (map)
    try:
        r = s.get(f"{base_url}/api/governance/geo-distribution", headers=headers, timeout=10)
        geo = r.json()
        if r.status_code == 200 and geo and all("lat" in c and "lng" in c for c in geo):
            R.ok("GET /api/governance/geo-distribution", f"{len(geo)} cities returned")
        else:
            R.fail("GET /api/governance/geo-distribution", f"status {r.status_code}, empty or malformed")
    except Exception as e:
        R.fail("GET /api/governance/geo-distribution", str(e))

    # 8. ROI waterfall data
    try:
        r = s.get(f"{base_url}/api/governance/roi", headers=headers, timeout=10)
        roi = r.json()
        required = ["estimated_revenue", "estimated_cost", "net_profit", "roi_multiplier"]
        if r.status_code == 200 and all(k in roi for k in required):
            recomputed_profit = round(roi["estimated_revenue"] - roi["estimated_cost"], 2)
            consistent = abs(recomputed_profit - roi["net_profit"]) < 1.0
            if consistent:
                R.ok("GET /api/governance/roi — internally consistent",
                     f"revenue={roi['estimated_revenue']} cost={roi['estimated_cost']} profit={roi['net_profit']}")
            else:
                R.fail("GET /api/governance/roi — internally consistent",
                       f"revenue-cost={recomputed_profit} != net_profit={roi['net_profit']}")
        else:
            R.fail("GET /api/governance/roi", f"status {r.status_code}, missing keys")
    except Exception as e:
        R.fail("GET /api/governance/roi", str(e))

    # 9. Anomalies (fraud scatter needs all_leads)
    try:
        r = s.get(f"{base_url}/api/governance/anomalies", headers=headers, timeout=10)
        anomaly = r.json()
        if r.status_code == 200 and "all_leads" in anomaly and isinstance(anomaly["all_leads"], list):
            R.ok("GET /api/governance/anomalies — includes all_leads for scatter plot",
                 f"{anomaly.get('flagged_count')} of {anomaly.get('total_leads')} flagged")
        else:
            R.fail("GET /api/governance/anomalies — includes all_leads",
                   "endpoint still only returns flagged_leads (scatter plot will have no background population)")
    except Exception as e:
        R.fail("GET /api/governance/anomalies", str(e))

    # 10. Audit trail (admin only)
    try:
        r = s.get(f"{base_url}/api/governance/audit-trail", headers=headers, timeout=10)
        if r.status_code == 200:
            events = r.json()
            R.ok("GET /api/governance/audit-trail", f"{len(events)} merged events")
        else:
            R.fail("GET /api/governance/audit-trail", f"status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        R.fail("GET /api/governance/audit-trail", str(e))

    # 11. Outreach windows present on lead list (Gantt timeline)
    try:
        r = s.get(f"{base_url}/api/leads", params={"limit": 10}, headers=headers, timeout=10)
        leads = r.json()
        if leads and all("outreach_window_start" in l and "outreach_window_end" in l for l in leads):
            R.ok("GET /api/leads — outreach windows present (feeds Gantt timeline)")
        else:
            R.fail("GET /api/leads — outreach windows present")
    except Exception as e:
        R.fail("GET /api/leads (outreach check)", str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Verify the LENS hackathon build (static + runtime).")
    parser.add_argument("--repo", type=str, default=".", help="Path to the repo root")
    parser.add_argument("--base-url", type=str, default=None,
                         help="Running server URL, e.g. http://localhost:8000. Omit to skip runtime checks.")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip running the pytest suite")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.exists():
        print(f"ERROR: repo path does not exist: {repo}")
        sys.exit(1)

    print("=" * 70)
    print(f"LENS BUILD VERIFICATION — repo: {repo}")
    print("=" * 70)

    # Static checks (always run — no server needed)
    check_frontend_public_identical(repo)
    check_frontend_components(repo)
    check_backend_files(repo)
    check_test_files(repo)
    if not args.skip_pytest:
        run_pytest(repo)

    # Runtime checks (only if a server URL was given)
    if args.base_url:
        runtime_checks(args.base_url.rstrip("/"))
    else:
        R.skip("All runtime API checks", "no --base-url provided — start the server and re-run with --base-url")

    success = R.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
