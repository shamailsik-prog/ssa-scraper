"""Production billiard on_process_exit must ignore SIGTERM before lock release."""

import signal

from scraper.pls_billiard_process_exit import billiard_worker_process_exit


def test_billiard_process_exit_sets_sig_ign_before_release(monkeypatch):
    order: list[str] = []

    def fake_signal(sig, handler):
        order.append("sig_ign")
        return None

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(
        "scraper.pls_lock_shutdown.release_on_worker_process_shutdown",
        lambda: order.append("release"),
    )
    billiard_worker_process_exit(1, 0)
    assert order == ["sig_ign", "release"]
