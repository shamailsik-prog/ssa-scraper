#!/usr/bin/env bash
# Run from repo root. Each mutant must fail its target test (exit non-zero).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3.12
fail() { echo "MUTANT $1: expected FAIL got PASS"; exit 1; }
pass_mutant() { echo "MUTANT $1: FAIL (ok)"; }

run_test() {
  $PY -m pytest "$1" -q --tb=no >/dev/null 2>&1
}

# M1
cp scraper/tasks/judgment_identity_reconcile.py /tmp/jir.bak
$PY <<'PY'
from pathlib import Path
p = Path("scraper/tasks/judgment_identity_reconcile.py")
t = p.read_text()
t2 = t.replace(
    '    if not unrep.full_text_hash or unrep.full_text_hash != rep.full_text_hash:\n        raise ValueError("full text hash must match for reported/unreported link")',
    '    if False:\n        raise ValueError("full text hash must match for reported/unreported link")',
)
p.write_text(t2)
PY
if run_test tests/test_b2_identity_reconcile.py::test_mutant_b2_m1_link_requires_matching_hash; then fail M1; else pass_mutant M1; fi
cp /tmp/jir.bak scraper/tasks/judgment_identity_reconcile.py

# M2 — link on identity_peers without hash
$PY <<'PY'
from pathlib import Path
p = Path("scraper/tasks/judgment_identity_reconcile.py")
t = p.read_text()
old = """        if len(hash_matches) == 1:
            try:
                await link_exact_identity(db, u.id, hash_matches[0].id, source="reconcile")
                counts["linked"] += 1
                linked_reported_id = hash_matches[0].id
            except ValueError:"""
new = """        if len(identity_peers) == 1:
            try:
                await link_exact_identity(db, u.id, identity_peers[0].id, source="reconcile")
                counts["linked"] += 1
                linked_reported_id = identity_peers[0].id
            except ValueError:"""
if old not in t:
    raise SystemExit("M2 pattern missing")
p.write_text(t.replace(old, new, 1))
PY
if run_test tests/test_b2_identity_reconcile.py::test_mutant_b2_m2_reconcile_skips_link_without_hash_match; then fail M2; else pass_mutant M2; fi
cp /tmp/jir.bak scraper/tasks/judgment_identity_reconcile.py

# Continue-after-link mutant
$PY <<'PY'
from pathlib import Path
p = Path("scraper/tasks/judgment_identity_reconcile.py")
t = p.read_text()
needle = "                linked_reported_id = hash_matches[0].id\n            except ValueError:"
if needle not in t:
    raise SystemExit("continue mutant pattern missing")
p.write_text(t.replace(
    "                linked_reported_id = hash_matches[0].id\n            except ValueError:",
    "                linked_reported_id = hash_matches[0].id\n                continue\n            except ValueError:",
    1,
))
PY
if run_test tests/test_b2_identity_reconcile.py::test_reconcile_links_exact_match_and_flags_conflicting_hash_peer; then fail CONTINUE; else pass_mutant CONTINUE; fi
cp /tmp/jir.bak scraper/tasks/judgment_identity_reconcile.py

# Flagged count mutant
$PY <<'PY'
from pathlib import Path
p = Path("scraper/tasks/judgment_identity_reconcile.py")
t = p.read_text()
t = t.replace("if created:\n                counts[\"flagged\"] += 1", "counts[\"flagged\"] += 1")
p.write_text(t)
PY
if run_test tests/test_b2_identity_reconcile.py::test_reconcile_flagged_count_only_new_review_rows; then fail FLAGCOUNT; else pass_mutant FLAGCOUNT; fi
cp /tmp/jir.bak scraper/tasks/judgment_identity_reconcile.py

# Ntry — propagate link raise
$PY <<'PY'
from pathlib import Path
p = Path("scraper/tasks/judgment_identity_reconcile.py")
t = p.read_text()
t = t.replace("            except ValueError:\n                _, created = await flag_identity_review(\n                    db,\n                    unreported_id=u.id,\n                    reported_id=hash_matches[0].id,\n                    reason=IDENTITY_REVIEW_HASH_MISMATCH,", "            except ValueError:\n                raise\n                _, created = await flag_identity_review(\n                    db,\n                    unreported_id=u.id,\n                    reported_id=hash_matches[0].id,\n                    reason=IDENTITY_REVIEW_HASH_MISMATCH,", 1)
p.write_text(t)
PY
if run_test tests/test_b2_identity_reconcile.py::test_scan_never_raises_when_link_guard_rejects; then fail NTRY; else pass_mutant NTRY; fi
cp /tmp/jir.bak scraper/tasks/judgment_identity_reconcile.py

# G19b
cp migrations/019_judgment_identity_reconcile.py /tmp/m19.bak
$PY <<'PY'
from pathlib import Path
p = Path("migrations/019_judgment_identity_reconcile.py")
t = p.read_text()
t = t.replace(
    """    if (await conn.execute(text("SELECT 1 FROM judgment_identity_review LIMIT 1"))).first():
        raise RuntimeError(
            "cannot downgrade 019_judgment_identity_reconcile: judgment_identity_review has rows"
        )
""",
    "",
    1,
)
p.write_text(t)
PY
if run_test tests/test_b2_migrations.py::test_migration_019_downgrade_refuses_when_review_rows_only; then fail G19b; else pass_mutant G19b; fi
cp /tmp/m19.bak migrations/019_judgment_identity_reconcile.py

# Backfill docket_key
cp scraper/tasks/pls_backfill.py /tmp/pls.bak
$PY <<'PY'
from pathlib import Path
p = Path("scraper/tasks/pls_backfill.py")
t = p.read_text()
t = t.replace(
    """                        if col == "docket_number":
                            j.docket_key = normalize_docket_key(value) or None
""",
    "",
    1,
)
p.write_text(t)
PY
if run_test tests/test_pls_backfill_case_metadata.py::test_backfill_sets_docket_key_when_filling_docket_number; then fail BACKFILL; else pass_mutant BACKFILL; fi
cp /tmp/pls.bak scraper/tasks/pls_backfill.py

# M20
cp migrations/018_judgment_case_text_hash.py /tmp/m18.bak
$PY <<'PY'
from pathlib import Path
p = Path("migrations/018_judgment_case_text_hash.py")
t = p.read_text()
t = t.replace("NULLIF({_DOCKET_KEY_SQL}, '')", f"({_DOCKET_KEY_SQL})")
p.write_text(t)
PY
if run_test tests/test_b1_migration_018.py::test_migration_018_m20_whitespace_docket_yields_null_docket_key; then fail M20; else pass_mutant M20; fi
cp /tmp/m18.bak migrations/018_judgment_case_text_hash.py

echo "All mutants verified."
