"""The alerts run's own logic (measure_alerts.py): its scenario is wired up as described, and the timeline of
pending, firing and resolved is read correctly from Prometheus polls. No Prometheus, no database."""

import measure_alerts as ma


def test_the_timeline_records_first_pending_first_firing_and_resolution_after_the_faults():
    polls = [
        (5, {}),
        (10, {"A": "pending"}),
        (15, {"A": "firing", "B": "pending"}),
        (20, {"A": "firing"}),  # B's condition stopped holding before the faults did: not "resolved"
        (25, {"A": "firing"}),
        (30, {}),
    ]
    assert ma.timeline(polls, faults_stopped=22) == {
        "A": {"pending": 10, "firing": 15, "resolved": 30},
        "B": {"pending": 15, "firing": None, "resolved": None},
    }


def test_an_alert_seen_again_is_not_resolved():
    polls = [(10, {"A": "firing"}), (20, {}), (30, {"A": "firing"})]
    assert ma.timeline(polls, faults_stopped=15)["A"]["resolved"] is None


def test_every_expected_alert_has_a_route_with_traffic_and_a_fault():
    routes, providers = ma.scenario_routes(), ma.scenario_providers()
    assert set(ma.EXPECTED.values()) <= set(routes) == set(ma.TRAFFIC)
    assert all(c.provider in providers for r in routes.values() for c in r.candidates)
    ma.stop_faults(providers)  # and stopping them leaves every provider healthy
    assert all(p.fail is None and p.first_token_delay == 0 for p in providers.values())
