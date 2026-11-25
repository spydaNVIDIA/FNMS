"""Offline checks prevent early probes and unsupported cold-start claims."""

import argparse
from datetime import UTC, datetime, timedelta

import pytest

from tests.render_cold_probe import AUTHORIZED_ORIGIN, classify_probe, ensure_authorized_window


def test_early_probe_and_missing_go_are_rejected():
    now = datetime.now(UTC)
    smoke = {
        "passed": True,
        "origin": AUTHORIZED_ORIGIN,
        "cold_start_eligible_at": (now + timedelta(minutes=1)).isoformat(),
    }
    with pytest.raises(RuntimeError, match="root GO"):
        ensure_authorized_window(argparse.Namespace(confirm_root_go_and_idle=False), smoke, now)
    with pytest.raises(RuntimeError, match="too early"):
        ensure_authorized_window(argparse.Namespace(confirm_root_go_and_idle=True), smoke, now)


@pytest.mark.parametrize(
    "cold_status,cold_time,warm_status,warm_time,classification",
    [
        (409, 60, 200, 0.2, "registry_retained_no_cold_restart_demonstrated"),
        (200, 0.5, 200, 0.2, "registry_lost_without_slow_wakeup"),
        (200, 60, 200, 0.2, "cache_loss_with_slow_wakeup_observed"),
        (503, 60, 200, 0.2, "inconclusive_transport_or_contract_failure"),
    ],
)
def test_classification_requires_both_cache_loss_and_wakeup_delay(
    cold_status, cold_time, warm_status, warm_time, classification
):
    assert classify_probe(cold_status, cold_time, warm_status, warm_time) == classification
