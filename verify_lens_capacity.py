#!/usr/bin/env python3
"""
verify_lens_capacity.py
Audits a LENS repo against the CAPACITY feature build prompt.
Run from repo root: python verify_lens_capacity.py
Exits non-zero if any REQUIRED check fails.
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(".").resolve()
BACKEND = ROOT / "backend"
RESULTS = []  # (task, check_name, passed: bool, required: bool, detail: str)

def log(task, check, passed, required=True, detail=""):
    RESULTS.append((task, check, passed, required, detail))

def read(path):
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None

def find_file(*candidates):
    for c in candidates:
        p = ROOT / c
        if p.exists():
            return p
    return None

def ast_has_def(source, name, kind="function"):
    """kind: 'function' or 'class'"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    node_type = ast.FunctionDef if kind == "function" else ast.ClassDef
    return any(isinstance(n, node_type) and n.name == name for n in ast.walk(tree))

def pydantic_class_fields(source, class_name):
    """Extract annotated field names from a pydantic BaseModel class."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields = set()
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
            return fields
    return set()


# ── TASK 1: CAPACITY module ────────────────────────────────────────────
print("=== Task 1: CAPACITY (Repayment Capacity Estimator) ===")

cap_file = find_file("backend/capacity.py", "capacity.py")
log(1, "backend/capacity.py exists", cap_file is not None)

cap_src = read(cap_file) if cap_file else ""
if cap_file:
    log(1, "compute_capacity() defined", ast_has_def(cap_src, "compute_capacity"))
    log(1, "FOIR_BANDS dict present (per loan type)",
        bool(re.search(r"FOIR_BANDS\s*=\s*\{", cap_src)))
    for lt in ["Personal Loan", "Auto Loan", "Home Loan", "Mortgage Loan"]:
        log(1, f"FOIR band covers '{lt}'", lt in cap_src, required=False)
    log(1, "ASSUMED_RATES dict present", bool(re.search(r"ASSUMED_RATES\s*=\s*\{", cap_src)))
    log(1, "ASSUMED_TENURE_MONTHS dict present",
        bool(re.search(r"ASSUMED_TENURE_MONTHS\s*=\s*\{", cap_src)))
    log(1, "eligible_principal() (EMI->principal formula) defined",
        ast_has_def(cap_src, "eligible_principal"))
    log(1, "over_leveraged flag / clamp logic present",
        "over_leveraged" in cap_src)
    log(1, "EMI-pattern recurring debit detection present",
        bool(re.search(r"existing_emi_monthly|recurring.*debit|coefficient.*variation", cap_src, re.I)))
else:
    print("  -> capacity.py missing, skipping deeper checks for Task 1")

# CapacityResult schema
models_file = find_file("backend/models.py", "models.py")
models_src = read(models_file) if models_file else ""
required_fields = {
    "customer_id", "reconstructed_income", "declared_income", "existing_emi_monthly",
    "disposable_income", "foir_ratio_applied", "safe_emi_ceiling", "dti_ratio",
    "eligible_amount_by_type", "recommended_loan_type", "recommended_eligible_amount",
    "recommended_tenure_months", "assumptions",
}
fields_found = pydantic_class_fields(models_src, "CapacityResult") if models_src else set()
# also check capacity.py itself in case model lives there
if not fields_found and cap_src:
    fields_found = pydantic_class_fields(cap_src, "CapacityResult")

missing_fields = required_fields - fields_found
log(1, "CapacityResult model found", bool(fields_found))
log(1, f"CapacityResult has all {len(required_fields)} required fields",
    not missing_fields, detail=f"missing: {sorted(missing_fields)}" if missing_fields else "")

# Pipeline integration: engine.py calls compute_capacity / uses CAPACITY
engine_file = find_file("backend/engine.py", "engine.py")
engine_src = read(engine_file) if engine_file else ""
log(1, "engine.py calls compute_capacity / CAPACITY stage wired in",
    bool(engine_src and re.search(r"compute_capacity|capacity\s*\(", engine_src)))

# API response includes capacity block
router_files = list(BACKEND.glob("**/*.py")) if BACKEND.exists() else list(ROOT.glob("**/*.py"))
capacity_in_response = any(
    "capacity" in read(f) and re.search(r"capacity\s*[:=]", read(f))
    for f in router_files if f.name not in ("capacity.py",)
)
log(1, "Lead-detail response wires in `capacity` field somewhere in routers",
    capacity_in_response, required=False)

# Tests
test_file = find_file("tests/test_capacity.py", "test_capacity.py")
log(1, "tests/test_capacity.py exists", test_file is not None)
if test_file:
    test_src = read(test_file)
    for expected in ["emi", "foir", "eligible", "dti", "integration"]:
        log(1, f"test_capacity.py has a test touching '{expected}'",
            expected in test_src.lower(), required=False)


# ── TASK 2: Frontend eligibility panel ─────────────────────────────────
print("\n=== Task 2: Frontend eligibility panel ===")
frontend_hits = []
for ext in ("*.jsx", "*.tsx", "*.js", "*.ts", "*.html"):
    frontend_hits += list(ROOT.glob(f"frontend/**/{ext}")) + list(ROOT.glob(f"src/**/{ext}"))

capacity_component_found = False
for f in frontend_hits:
    src = read(f) or ""
    if re.search(r"capacity|eligib", src, re.I) and re.search(r"foir|dti|emi", src, re.I):
        capacity_component_found = True
        break
log(2, "A frontend component references capacity/eligibility + FOIR/DTI/EMI",
    capacity_component_found)


# ── TASK 3: Per-loan-type accuracy breakdown ───────────────────────────
print("\n=== Task 3: Per-loan-type accuracy breakdown ===")
gov_hits = [f for f in (router_files if router_files else []) if "govern" in f.name.lower()]
gov_src = "\n".join(read(f) or "" for f in gov_hits)
if not gov_src:
    gov_src = engine_src or ""
log(3, "Per-loan-type precision/recall/F1 breakdown present",
    bool(re.search(r"per_loan_type|loan_type.*(precision|recall|f1)", gov_src, re.I)))


# ── TASK 4: TRUST tier action labels ───────────────────────────────────
print("\n=== Task 4: TRUST tier relabeling ===")
log(4, "TIER_ACTION_LABELS dict present", bool(engine_src and "TIER_ACTION_LABELS" in engine_src))
log(4, "tier_action_label field present", bool(engine_src and "tier_action_label" in engine_src))
log(4, "Original 'tier' field (Tier 1/2/3) still present, not renamed",
    bool(engine_src and re.search(r"[\"']?tier[\"']?\s*[:=]", engine_src, re.I)))


# ── TASK 5: README methodology section ─────────────────────────────────
print("\n=== Task 5: README methodology section ===")
readme = read("README.md") or ""
log(5, "README has a 'Conversion Rate & Underwriting Methodology' section",
    bool(re.search(r"conversion rate.*(methodology|underwriting)", readme, re.I)))
log(5, "README defines conversion rate precisely (mentions ground-truth / noise_level)",
    bool(re.search(r"ground.?truth|noise_level", readme, re.I)), required=False)
log(5, "README discloses FOIR bands / rate & tenure assumptions",
    bool(re.search(r"FOIR|assumption", readme, re.I)), required=False)


# ── ACCEPTANCE: run existing test suite, confirm untouched ─────────────
print("\n=== Acceptance: existing test suite still passes ===")
def run_pytest(path):
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q"],
            capture_output=True, text=True, timeout=180
        )
        return r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    except FileNotFoundError:
        return False, "pytest not installed"
    except subprocess.TimeoutExpired:
        return False, "pytest timed out"

gov_test = find_file("tests/test_governance.py")
if gov_test:
    ok, out = run_pytest(gov_test)
    log("accept", "tests/test_governance.py (31 tests) passes unmodified", ok, detail=out if not ok else "")
else:
    log("accept", "tests/test_governance.py found", False)

if test_file:
    ok, out = run_pytest(test_file)
    log("accept", "tests/test_capacity.py passes", ok, detail=out if not ok else "")


# ── OPTIONAL: live API smoke test ──────────────────────────────────────
print("\n=== Optional: live API smoke test (skipped if server not running) ===")
try:
    import urllib.request
    BASE_URL = "http://localhost:8000"  # adjust if different
    test_customer_id = "CUST10000"      # adjust to a real seeded id
    url = f"{BASE_URL}/api/leads/{test_customer_id}"
    with urllib.request.urlopen(url, timeout=3) as resp:
        data = json.loads(resp.read())
    has_capacity = "capacity" in data
    log(1, "Live API: lead detail response includes 'capacity' object", has_capacity, required=False)
    if has_capacity:
        missing = required_fields - set(data["capacity"].keys())
        log(1, "Live API: capacity object has all required fields", not missing,
            required=False, detail=f"missing: {sorted(missing)}" if missing else "")
except Exception as e:
    print(f"  -> Skipped (server not reachable at localhost:8000): {e}")


# ── REPORT ──────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

required_fail = False
by_task = {}
for task, check, passed, required, detail in RESULTS:
    by_task.setdefault(task, []).append((check, passed, required, detail))

for task in sorted(by_task.keys(), key=lambda x: str(x)):
    print(f"\nTask {task}:")
    for check, passed, required, detail in by_task[task]:
        tag = "REQUIRED" if required else "optional"
        mark = "[OK]" if passed else "[FAIL]"
        print(f"  {mark} [{tag}] {check}")
        if detail and not passed:
            print(f"       detail: {detail}")
        if not passed and required:
            required_fail = True

print("\n" + "=" * 70)
if required_fail:
    print("RESULT: [FAIL] One or more REQUIRED checks failed. Not done yet.")
    sys.exit(1)
else:
    print("RESULT: [PASS] All required checks passed.")
    sys.exit(0)
