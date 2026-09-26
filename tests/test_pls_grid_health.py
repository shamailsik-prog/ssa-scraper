from scraper.pls_grid_health import grid_rows_remaining, pls_zero_query_grid_failure


def test_cursor_resume_never_treated_as_complete_at_offset():
    cfg = {
        "citation_grid_cursor": {"row_offset": 8344, "last_total_rows": 20568},
        "citation_grid_cursor_shard_0": {"row_offset": 4818, "last_total_rows": 20568},
        "citation_grid_cursor_shard_1": {"row_offset": 4271, "last_total_rows": 20568},
    }
    assert grid_rows_remaining(cfg) > 0


def test_zero_query_grid_failure_when_rows_remain():
    stats = {"surface_mode": "citation_grid", "queries": 0, "pages": 0, "citation_grid_windows": 0}
    cfg = {"citation_grid_cursor": {"row_offset": 100, "last_total_rows": 500}}
    msg = pls_zero_query_grid_failure(stats, cfg)
    assert msg and "zero citation-grid progress" in msg


def test_zero_query_not_failure_when_progress():
    stats = {"surface_mode": "citation_grid", "queries": 0, "citation_grid_windows": 1, "pages_charged": 2}
    cfg = {"citation_grid_cursor": {"row_offset": 100, "last_total_rows": 500}}
    assert pls_zero_query_grid_failure(stats, cfg) is None
