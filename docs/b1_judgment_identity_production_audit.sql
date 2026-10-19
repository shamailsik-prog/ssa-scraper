-- B1 production audit (read-only). Run against the live corpus DB; do not execute from automation.

-- Staging rows marked duplicate whose reconciled docket disagrees with the promoted judgment.
SELECT s.id, s.source_name, j.id, j.canonical_citation
FROM scraper_staging s
JOIN judgment j ON j.id = s.promoted_to_id
WHERE s.status = 'duplicate'
  AND (s.reconciled_json->>'docket_number') IS NOT NULL
  AND regexp_replace(lower(s.reconciled_json->>'docket_number'), '\s+', '', 'g')
      <> regexp_replace(lower(coalesce(j.docket_number, '')), '\s+', '', 'g');

-- Unreported rows later relabelled to reported (alias trail).
SELECT j.id, j.canonical_citation, c.citation_string
FROM citation c
JOIN judgment j ON j.id = c.judgment_id
WHERE c.source_evidence = 'unreported identity before a citation was known';

-- Courtless PLD canonical citations still stored (year/page only).
SELECT id, canonical_citation
FROM judgment
WHERE canonical_citation ~ '^(PLD \d{4} \d+|\d{4} PLD \d+)$';

-- PLD reporter rows whose embedded court disagrees with the judgment header snippet.
SELECT j.id, j.canonical_citation, c.name
FROM judgment j
JOIN court c ON c.id = j.court_id
WHERE j.reporter = 'PLD'
  AND (
    (j.canonical_citation ILIKE '% SC %' AND left(j.full_text, 800) NOT ILIKE '%supreme court%')
    OR (j.canonical_citation ILIKE '% Lahore %' AND left(j.full_text, 800) NOT ILIKE '%lahore%')
  );

-- Pre-migration: rows that would block uq_judgment_case_text_hash (same court, docket, date, hash).
SELECT court_id, lower(regexp_replace(coalesce(docket_number, ''), '\s+', '', 'g')) AS docket_key,
       decision_date, full_text_hash, count(*) AS n
FROM judgment
WHERE full_text_hash IS NOT NULL
  AND court_id IS NOT NULL
  AND docket_number IS NOT NULL
  AND decision_date IS NOT NULL
  AND lower(regexp_replace(coalesce(docket_number, ''), '\s+', '', 'g')) <> ''
GROUP BY 1, 2, 3, 4
HAVING count(*) > 1;
