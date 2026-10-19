-- Read-only production audit queries (Scraper Auditor baselines 2026-10-18).
-- Run against the corpus database with a read-only role; no writes.

-- Total statutes
SELECT COUNT(*) AS statutes_total FROM statute;

-- Statutes with no child sections (legacy stubs / listing-only rows)
SELECT COUNT(*) AS statutes_without_sections
FROM statute s
WHERE NOT EXISTS (SELECT 1 FROM statute_section ss WHERE ss.statute_id = s.id);

-- Statutes with at least one section row
SELECT COUNT(*) AS statutes_with_any_sections
FROM statute s
WHERE EXISTS (SELECT 1 FROM statute_section ss WHERE ss.statute_id = s.id);

-- Statutes with at least one section that has non-empty current version text
SELECT COUNT(*) AS statutes_complete
FROM statute s
WHERE EXISTS (
    SELECT 1
    FROM statute_section ss
    JOIN statute_section_version sv ON sv.id = ss.current_version_id
    WHERE ss.statute_id = s.id
      AND length(btrim(sv.section_text)) > 0
);

-- Missing enactment year (auditor: 588)
SELECT COUNT(*) AS statutes_no_year FROM statute WHERE year_enacted IS NULL;

-- Distinct names that appear more than once (auditor: 79 duplicate name groups)
SELECT COUNT(*) AS statutes_duplicate_name_groups
FROM (
    SELECT lower(name) AS lname
    FROM statute
    GROUP BY lower(name)
    HAVING COUNT(*) > 1
) dup;

-- Rows flagged by migration 022 (after deploy)
SELECT COUNT(*) AS needs_section_refetch FROM statute WHERE needs_section_refetch;
SELECT COUNT(*) AS quality_flag_no_year FROM statute WHERE quality_flag_no_year;
SELECT COUNT(*) AS quality_flag_duplicate_name FROM statute WHERE quality_flag_duplicate_name;
