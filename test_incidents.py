"""Policy regressions without credentials or external notifications."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from page_support import audit, callers, history, incident_decision, scan


class IncidentTest(unittest.TestCase):
    def decision(self, state=None, at=1000, **kwargs):
        return incident_decision(state, condition=kwargs.pop("condition", "firing"),
                                 observed_at=at, now=kwargs.pop("now", at),
                                 human_action=kwargs.pop("human_action", "Restore the incident responder"), **kwargs)

    def test_transient_firing_never_wakes(self):
        state, sev, reason, key = self.decision()
        self.assertEqual((sev, reason), ("info", "triage_grace"))
        for at in (1060, 1120, 1180):
            state, sev, _, _ = self.decision(state, at=at)
            self.assertEqual(sev, "info")
        state, sev, _, _ = self.decision(state, at=1240, condition="resolved")
        self.assertIsNone(sev)
        self.assertTrue(state["active"])

    def test_persistent_unhandled_risk_escalates_but_handled_does_not(self):
        state = None
        for at in range(1000, 1660, 60):
            state, sev, _, key = self.decision(state, at=at)
        self.assertEqual(sev, "wake")
        self.assertEqual(key, 1)
        self.assertEqual(self.decision(state, at=1660, handling="automatic")[1], "info")
        self.assertEqual(self.decision(state, at=1660, human_action=None)[1], "info")

    def test_verified_urgent_harm_keeps_immediate_path(self):
        self.assertEqual(self.decision(wake_after=0)[1], "wake")

    def test_stale_unknown_and_future_cannot_wake_or_clear(self):
        state = self.decision()[0]
        for kwargs in (dict(now=1400), dict(now=999), dict(condition="unknown")):
            updated, severity, _, _ = self.decision(state, **kwargs)
            self.assertEqual(updated, state)
            self.assertIsNone(severity)

    def test_flapping_stays_in_one_episode_and_restarts_continuity(self):
        state = self.decision()[0]
        state, _, _, episode = self.decision(state, at=1060, condition="resolved")
        state, sev, _, later = self.decision(state, at=1120)
        self.assertEqual(episode, later)
        self.assertEqual(state["first_seen"], 1120)
        self.assertEqual(sev, "info")
        state, _, _, _ = self.decision(state, at=1180, condition="resolved")
        state, sev, reason, _ = self.decision(state, at=4780, condition="resolved")
        self.assertEqual((sev, reason), ("info", "resolved"))
        self.assertFalse(state["active"])
        self.assertEqual(self.decision(state, at=4840)[3], episode + 1)

    def test_resolution_can_retry_after_transport_failure(self):
        state = self.decision()[0]
        state = self.decision(state, at=1060, condition="resolved", resolve_after=0)[0]
        self.assertFalse(state["active"])
        self.assertEqual(self.decision(state, at=1120, condition="resolved")[1:3], ("info", "resolved"))

    def test_gap_resets_continuous_risk_clock(self):
        state = self.decision()[0]
        state, severity, _, _ = self.decision(state, at=2000)
        self.assertEqual((severity, state["first_seen"]), ("info", 2000))

    def test_audit_records_refusal_suppression_failure_and_caller_index(self):
        with tempfile.TemporaryDirectory() as root:
            for outcome in ("refused", "suppressed", "failed", "sent"):
                self.assertTrue(audit(root, dict(source="batch", outcome=outcome, parent_comm="python")))
            self.assertEqual(len(history(root)), 4)
            self.assertEqual(callers(root)[0]["outcomes"]["suppressed"], 1)
            log = next((Path(root) / "audit").glob("*.jsonl"))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            with log.open("a") as f:
                f.write("bad row\n")
            self.assertEqual(len(history(root)), 4)

    def test_audit_rotates_and_retains_private_bounded_segments(self):
        with tempfile.TemporaryDirectory() as root, patch("page_support.AUDIT_BYTES", 1):
            for _ in range(8):
                audit(root, dict(source="batch", outcome="sent"))
            self.assertEqual(len(list((Path(root) / "audit").glob("*.jsonl*"))), 4)

    def test_inventory_emits_only_locations_not_code_or_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "monitor.sh").write_text('page --sev wake --source disk "private context"\n')
            (path / "secrets").mkdir()
            (path / "secrets/no.sh").write_text('page --sev wake "secret"')
            (path / "linked.sh").symlink_to(path / "monitor.sh")
            rows = scan(root)
            self.assertEqual(rows, [dict(path="monitor.sh", line=1, source="disk")])


if __name__ == "__main__":
    unittest.main()
