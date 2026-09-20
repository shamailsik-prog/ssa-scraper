from __future__ import annotations

import asyncio

from scraper.tasks import residual_smoke_cli


def test_residual_smoke_cli_defaults_to_dry_run(monkeypatch, capsys):
    captured: dict[str, object] = {}

    async def _fake_smoke(**kwargs):
        captured.update(kwargs)
        return {"mode": "dry_run", "before": {"total_unresolved": 1}}

    monkeypatch.setattr(
        residual_smoke_cli,
        "reconcile_citation_statute_residual_smoke",
        _fake_smoke,
    )
    monkeypatch.setattr(
        residual_smoke_cli,
        "run_async",
        lambda coro: asyncio.run(coro),
    )

    code = residual_smoke_cli.main([])
    output = capsys.readouterr()

    assert code == 0
    assert '"mode": "dry_run"' in output.out
    assert captured == {
        "run_reconcile": False,
        "lookback_hours": None,
        "instrument_limit": None,
        "judgment_batch_size": None,
        "fail_on_increase": True,
        "include_unresolved_breakdown": False,
        "unresolved_breakdown_top_n": None,
    }


def test_residual_smoke_cli_apply_breakdown_flags(monkeypatch, capsys):
    captured: dict[str, object] = {}

    async def _fake_smoke(**kwargs):
        captured.update(kwargs)
        return {
            "mode": "apply",
            "unresolved_breakdown": {
                "top_n": kwargs["unresolved_breakdown_top_n"],
            },
        }

    monkeypatch.setattr(
        residual_smoke_cli,
        "reconcile_citation_statute_residual_smoke",
        _fake_smoke,
    )
    monkeypatch.setattr(
        residual_smoke_cli,
        "run_async",
        lambda coro: asyncio.run(coro),
    )

    code = residual_smoke_cli.main(
        [
            "--apply",
            "--lookback-hours",
            "168",
            "--instrument-limit",
            "200",
            "--judgment-batch-size",
            "150",
            "--include-unresolved-breakdown",
            "--unresolved-breakdown-top-n",
            "7",
            "--no-fail-on-increase",
        ]
    )
    output = capsys.readouterr()

    assert code == 0
    assert '"top_n": 7' in output.out
    assert captured == {
        "run_reconcile": True,
        "lookback_hours": 168,
        "instrument_limit": 200,
        "judgment_batch_size": 150,
        "fail_on_increase": False,
        "include_unresolved_breakdown": True,
        "unresolved_breakdown_top_n": 7,
    }


def test_residual_smoke_cli_surfaces_fail_closed_error(monkeypatch, capsys):
    def _raise(_coro):
        raise RuntimeError("unresolved counts increased")

    monkeypatch.setattr(residual_smoke_cli, "run_async", _raise)

    code = residual_smoke_cli.main(["--apply"])
    output = capsys.readouterr()

    assert code == 1
    assert "residual smoke failed: unresolved counts increased" in output.err
