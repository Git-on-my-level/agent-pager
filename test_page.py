"""Runs the real CLI against a local fake server standing in for both backends."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("page")
BARK_KEY = "barkkeybarkkeybarkkeybark"
NTFY_TOPIC = "topic-topic-topic-topic"


class FakeServer(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests = []
        self.failing = set()
        self.truncating = set()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        backend = self.path.split("/")[1]
        secret = self.path.split("/")[2]
        self.server.requests.append({"backend": backend, "secret": secret, "headers": dict(self.headers), "body": body})
        self.send_response(500 if backend in self.server.failing else 200)
        if backend in self.server.truncating:
            self.send_header("Content-Length", "10")
        self.end_headers()

    def log_message(self, *args):
        pass


class PageTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "config/secrets").mkdir(parents=True)
        self.set_secret("bark-key", BARK_KEY)
        self.set_secret("ntfy-topic", NTFY_TOPIC)
        self.write_config({})

    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def write_config(self, config):
        """Points both backends at the fake server, then applies the test's own settings over that."""
        backends = {"bark": {"server": f"{self.base_url()}/bark"}, "ntfy": {"server": f"{self.base_url()}/ntfy"}}
        for name, settings in config.pop("backends", {}).items():
            backends[name].update(settings)
        (self.root / "config/config.json").write_text(json.dumps({"backends": backends, **config}))

    def set_secret(self, name, value):
        (self.root / "config/secrets" / name).write_text(value + "\n")

    def page(self, *args, config_dir=None):
        env = dict(
            os.environ,
            PAGE_CONFIG_DIR=str(config_dir or self.root / "config"),
            PAGE_STATE_DIR=str(self.root / "state"),
        )
        return subprocess.run([sys.executable, str(PAGE), *args], env=env, capture_output=True, text=True, timeout=180)

    def backends(self):
        return [request["backend"] for request in self.server.requests]

    def test_wake_rings_on_bark(self):
        result = self.page("--sev", "wake", "--url", "https://example.test/card", "lead is down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark"])
        body = json.loads(self.server.requests[0]["body"])
        self.assertEqual(self.server.requests[0]["secret"], BARK_KEY)
        self.assertEqual((body["level"], body["call"], body["body"]), ("critical", "1", "lead is down"))
        self.assertEqual(body["url"], "https://example.test/card")

    def test_info_goes_to_ntfy_at_normal_priority(self):
        self.assertEqual(self.page("build finished").returncode, 0)
        self.assertEqual(self.backends(), ["ntfy"])
        self.assertEqual(self.server.requests[0]["headers"]["Priority"], "3")
        self.assertEqual(self.server.requests[0]["body"], b"build finished")

    def test_wake_falls_back_to_max_priority_ntfy(self):
        self.server.failing.add("bark")
        result = self.page("--sev", "wake", "lead is down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark", "ntfy"])
        self.assertEqual(self.server.requests[1]["headers"]["Priority"], "5")
        self.assertIn("bark: http 500", result.stderr)
        self.assertIn("via ntfy, may not have rung", result.stdout)

    def test_a_wake_that_rang_does_not_hedge_and_a_quiet_test_says_so(self):
        self.assertEqual(self.page("--sev", "wake", "lead is down").stdout.strip(), "page: sent sev=wake via bark")
        self.assertIn("via bark, quiet", self.page("test", "--sev", "wake").stdout)

    def test_missing_backend_is_skipped(self):
        (self.root / "config/secrets/bark-key").unlink()
        self.assertEqual(self.page("--sev", "wake", "lead is down").returncode, 0)
        self.assertEqual(self.backends(), ["ntfy"])

    def test_no_backend_set_up_fails(self):
        (self.root / "config/secrets/bark-key").unlink()
        (self.root / "config/secrets/ntfy-topic").unlink()
        result = self.page("hello")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no backend set up", result.stderr)

    def test_repeat_is_suppressed_and_a_new_key_is_not(self):
        self.page("--sev", "wake", "--dedupe", "lead", "lead is down")
        repeat = self.page("--sev", "wake", "--dedupe", "lead", "lead is still down")
        self.assertEqual(repeat.returncode, 0)
        self.assertIn("suppressed", repeat.stdout)
        self.assertEqual(len(self.server.requests), 1)
        self.page("--sev", "wake", "--dedupe", "prod", "prod tripped")
        self.assertEqual(len(self.server.requests), 2)

    def test_failed_send_is_not_recorded_so_a_retry_goes_out(self):
        self.server.failing.update({"bark", "ntfy"})
        first = self.page("--sev", "wake", "--dedupe", "lead", "lead is down")
        self.assertEqual(first.returncode, 1)
        self.server.failing.clear()
        self.assertEqual(self.page("--sev", "wake", "--dedupe", "lead", "lead is down").returncode, 0)
        self.assertEqual(self.backends()[-1], "bark")

    def test_wake_over_the_hourly_cap_goes_out_as_info(self):
        for number in range(6):
            self.assertEqual(self.page("--sev", "wake", f"alert {number}").returncode, 0)
        result = self.page("--sev", "wake", "alert 7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sending as info", result.stderr)
        self.assertEqual(self.backends(), ["bark"] * 6 + ["ntfy"])
        self.assertEqual(self.server.requests[-1]["headers"]["Priority"], "3")

    def test_token_shaped_text_is_masked_and_message_is_capped(self):
        self.page("key sk-ant-abcdefghijklmnopqrstuvwxyz0123 leaked " + "x " * 300)
        body = self.server.requests[0]["body"].decode()
        self.assertNotIn("abcdefghijklmnop", body)
        self.assertIn("[masked]", body)
        self.assertLessEqual(len(body), 240)

    def test_secrets_never_reach_output_or_log(self):
        self.server.failing.update({"bark", "ntfy"})
        failed = self.page("--sev", "wake", "lead is down")
        self.server.failing.clear()
        sent = self.page("--sev", "wake", "lead is down")
        status = self.page("status")
        log = (self.root / "state/sent.jsonl").read_text()
        for text in (failed.stdout, failed.stderr, sent.stdout, sent.stderr, status.stdout, status.stderr, log):
            self.assertNotIn(BARK_KEY, text)
            self.assertNotIn(NTFY_TOPIC, text)

    def test_wake_test_is_quiet_unless_loud(self):
        self.page("test", "--sev", "wake")
        self.assertEqual(json.loads(self.server.requests[0]["body"])["level"], "active")
        self.page("test", "--sev", "wake", "--loud")
        self.assertEqual(json.loads(self.server.requests[1]["body"])["level"], "critical")

    def test_empty_message_is_rejected(self):
        self.assertEqual(self.page("   ").returncode, 2)
        self.assertEqual(self.server.requests, [])

    def test_config_changes_route_cap_window_and_volume(self):
        self.write_config({
            "routes": {"wake": ["ntfy"], "info": ["bark"]},
            "windows": {"info": 0},
            "wake_cap_per_hour": 1,
            "backends": {"bark": {"volume": 9}},
        })
        self.page("--sev", "wake", "first")
        self.assertEqual(self.backends(), ["ntfy"])
        self.page("--sev", "wake", "second")
        self.assertEqual(self.backends(), ["ntfy", "bark"])
        self.page("same")
        self.page("same")
        self.assertEqual(self.backends(), ["ntfy", "bark", "bark", "bark"])
        self.write_config({"backends": {"bark": {"volume": 9}}})
        self.page("test", "--sev", "wake", "--loud")
        self.assertEqual(json.loads(self.server.requests[-1]["body"])["volume"], 9)

    def test_config_can_point_at_a_secret_file_elsewhere(self):
        elsewhere = self.root / "managed-key"
        elsewhere.write_text("elsewhere-key\n")
        (self.root / "config/secrets/bark-key").unlink()
        self.write_config({"backends": {"bark": {"secret_file": str(elsewhere)}}})
        self.assertEqual(self.page("--sev", "wake", "lead is down").returncode, 0)
        self.assertEqual(self.server.requests[0]["secret"], "elsewhere-key")

    def test_unusable_config_is_reported_and_cannot_silence_a_page(self):
        cases = (
            "{not json",
            '["a list"]',
            '{"typo_key": 1}',
            '{"routes": {"wake": ["nope"]}}',
            '{"routes": {"wake": []}}',
            '{"wake_cap_per_hour": 0}',
            '{"backends": {"bark": {"volume": 11}}}',
            '{"backends": {"bark": {"server": "ftp://x"}}}',
            '{"backends": {"bark": {"server": "http://example.test"}}}',
            '{"backends": {"bark": {"server": "https://"}}}',
            '{"routes": null}',
            '{"windows": 5}',
            '{"backends": []}',
            '{"routes": {"wake": [[]]}}',
        )
        for number, text in enumerate(cases):
            with self.subTest(text=text):
                directory = self.root / f"bad{number}"
                directory.mkdir()
                (directory / "config.json").write_text(text)
                result = self.page("status", config_dir=directory)
                self.assertEqual(result.returncode, 0)
                self.assertIn("ignored", result.stdout)

    def test_a_bad_setting_keeps_the_rest_of_the_config(self):
        elsewhere = self.root / "managed-key"
        elsewhere.write_text("elsewhere-key\n")
        (self.root / "config/secrets/bark-key").unlink()
        for bad in ({"typo_key": 1}, {"wake_cap_per_hour": 0}, {"routes": {"wake": []}}, {"windows": 5}):
            with self.subTest(bad=bad):
                self.write_config({"backends": {"bark": {"secret_file": str(elsewhere)}}, **bad})
                self.server.requests.clear()
                result = self.page("--sev", "wake", "--window", "0", "lead is down")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ignored", result.stderr)
                self.assertEqual(self.server.requests[0]["secret"], "elsewhere-key")

    def test_a_bad_backend_setting_keeps_the_other_backend_settings(self):
        elsewhere = self.root / "managed-key"
        elsewhere.write_text("elsewhere-key\n")
        (self.root / "config/secrets/bark-key").unlink()
        self.write_config({"backends": {"bark": {"secret_file": str(elsewhere), "volume": 11}}})
        result = self.page("--sev", "wake", "lead is down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.requests[0]["secret"], "elsewhere-key")
        self.assertEqual(json.loads(self.server.requests[0]["body"])["volume"], 5)

    def test_an_unparseable_server_keeps_the_secret_path(self):
        # The default server is never contacted: the bad setting is only checked, and status sends nothing.
        elsewhere = self.root / "managed-key"
        elsewhere.write_text("elsewhere-key\n")
        (self.root / "config/secrets/bark-key").unlink()
        (self.root / "config/config.json").write_text(json.dumps(
            {"backends": {"bark": {"secret_file": str(elsewhere), "server": "https://[bad"}}}))
        output = self.page("status").stdout
        self.assertIn("partly ignored", output)
        self.assertIn("bark: set up", output)

    def test_bad_config_falls_back_to_defaults_when_paging(self):
        # No secrets, so the default servers are never contacted.
        directory = self.root / "bad"
        directory.mkdir()
        (directory / "config.json").write_text("{not json")
        result = self.page("--sev", "wake", "lead is down", config_dir=directory)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ignored", result.stderr)
        self.assertIn("no backend set up", result.stderr)

    def test_truncated_response_falls_back(self):
        self.server.truncating.add("bark")
        result = self.page("--sev", "wake", "lead is down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark", "ntfy"])

    def test_unusable_state_directory_still_sends(self):
        (self.root / "state").write_text("a file where the directory should be")
        result = self.page("--sev", "wake", "lead is down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state unavailable", result.stderr)
        self.assertEqual(self.backends(), ["bark"])

    def test_malformed_state_lines_are_ignored(self):
        (self.root / "state").mkdir()
        (self.root / "state/sent.jsonl").write_text('[]\n{"at": 1}\n{"at": "x", "key": 1}\nnot json\n')
        self.assertEqual(self.page("--sev", "wake", "lead is down").returncode, 0)
        self.assertEqual(self.page("status").returncode, 0)
        self.assertEqual(self.backends(), ["bark"])

    def test_a_held_lock_delays_a_page_but_cannot_stop_it(self):
        (self.root / "state").mkdir()
        with open(self.root / "state/lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.page("--sev", "wake", "lead is down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state unavailable", result.stderr)
        self.assertEqual(self.backends(), ["bark"])

    def test_url_keeps_its_path_and_loses_login_and_tokens(self):
        self.page(
            "--sev", "wake",
            "--url", "https://me:pw@example.test/owner/repo/pull/12345/long-readable-path?sig=abcdefghijklmnopqrstuvwxyz012345&a=1",
            "lead is down",
        )
        url = json.loads(self.server.requests[0]["body"])["url"]
        self.assertTrue(url.startswith("https://example.test/owner/repo/pull/12345/long-readable-path?"), url)
        self.assertNotIn("abcdefghijklmnop", url)
        self.assertNotIn("me:pw", url)

    def test_url_with_unusual_characters_still_sends_on_ntfy(self):
        for number, url in enumerate(("https://example.test/caf\u00e9\u2603", "http://[bad", "https://example.test/a\nb")):
            with self.subTest(url=url):
                result = self.page("--url", url, f"message {number}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.backends()[-1], "ntfy")

    def test_url_encodes_spaces_in_its_path(self):
        self.page("--sev", "wake", "--url", " https://example.test/incident room ", "lead is down")
        self.assertEqual(json.loads(self.server.requests[0]["body"])["url"], "https://example.test/incident%20room")

    def test_repeats_downgraded_by_the_cap_are_still_suppressed(self):
        self.write_config({"wake_cap_per_hour": 1})
        self.page("--sev", "wake", "first")
        self.page("--sev", "wake", "--dedupe", "looping", "second")
        repeat = self.page("--sev", "wake", "--dedupe", "looping", "second again")
        self.assertIn("suppressed", repeat.stdout)
        self.assertEqual(self.backends(), ["bark", "ntfy"])

    def test_quiet_tests_do_not_use_up_the_cap(self):
        self.write_config({"wake_cap_per_hour": 1})
        self.page("test", "--sev", "wake")
        self.page("test", "--sev", "wake")
        self.page("--sev", "wake", "lead is down")
        self.assertEqual(json.loads(self.server.requests[-1]["body"])["level"], "critical")

    def test_short_credentials_are_masked(self):
        self.page("aws AKIAIOSFODNN7EXAMPLE and password=hunter2 here")
        body = self.server.requests[0]["body"].decode()
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", body)
        self.assertNotIn("hunter2", body)
        self.assertIn("here", body)

    def test_readable_identifiers_stay_readable(self):
        for text in (
            "see card:agent-fleet-token-efficiency-david-10-30",
            "branch driver-failover-allowlist-alert failed",
            "/Volumes/Ephemeral/scratch/worktrees/hostctl-config-fo1 is full",
            "Codex token: expired on m4-mini",
            "Grok api_key: missing on m1",
        ):
            with self.subTest(text=text):
                self.page("--window", "0", text)
                self.assertEqual(self.server.requests[-1]["body"].decode(), text)

    def test_credential_shapes_are_still_masked(self):
        for secret in (
            "abcdefghijklmnopqrstuvwxyz0123",
            "aB3xY9kL2mN4-pQ7rS1tU5vW8zC6",
            "550e8400-e29b-41d4-a716-446655440000",
            "dGhpcyBpcyBhIHRlc3Qgc3RyaW5n+==",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "token: abc123",
            "Bearer my-long-lived-access-token-2026-11-01",
            "token=expired",
            "api_key: abcdefghijklmnop",
            "my_password=correct-horse-battery-staple-long",
            "secret: missing",
        ):
            with self.subTest(secret=secret):
                self.page("--window", "0", f"before {secret} after")
                body = self.server.requests[-1]["body"].decode()
                self.assertNotIn(secret, body)
                self.assertIn("[masked]", body)

    def test_url_query_keeps_a_readable_value(self):
        self.page("--sev", "wake", "--url", "https://example.test/c?card=agent-fleet-resilience-david-10-30", "x")
        self.assertEqual(
            json.loads(self.server.requests[0]["body"])["url"],
            "https://example.test/c?card=agent-fleet-resilience-david-10-30",
        )

    def test_status_rejects_extra_arguments(self):
        self.assertEqual(self.page("status", "unexpected").returncode, 2)

    def test_status_reports_a_missing_secret(self):
        (self.root / "config/secrets/ntfy-topic").unlink()
        output = self.page("status").stdout
        self.assertIn("bark: set up", output)
        self.assertIn("ntfy: no secret file", output)


if __name__ == "__main__":
    unittest.main()
