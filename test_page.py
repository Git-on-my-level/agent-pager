"""Runs the real CLI against a local fake server standing in for both backends."""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
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

    def test_incident_refuses_stale_wake_and_audits_deferral(self):
        result = self.page("--sev", "wake", "--source", "batch", "--incident", "custody",
                           "--observed-at", "2020-01-01T00:00:00Z", "--wake-after", "0",
                           "--human-action", "Restore storage access", "Audio custody needs investigation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deferred stale_observation", result.stdout)
        self.assertEqual(self.server.requests, [])
        rows = json.loads(self.page("history", "--json").stdout)
        self.assertEqual(rows[-1]["outcome"], "deferred")

    def test_incident_info_then_urgent_wake_and_duplicate_suppression(self):
        import datetime
        observed = datetime.datetime.now(datetime.timezone.utc).isoformat()
        args = ("--sev", "wake", "--source", "batch", "--incident", "custody",
                "--observed-at", observed, "--human-action", "Restore storage access")
        result = self.page(*args, "Audio custody needs investigation")
        self.assertIn("deferred triage_grace", result.stdout)
        result = self.page(*args, "--wake-after", "0", "Confirmed missing audio needs investigation")
        self.assertIn("sev=wake", result.stdout)
        result = self.page(*args, "--wake-after", "0", "Confirmed missing audio needs investigation")
        self.assertIn("suppressed", result.stdout)
        self.assertEqual(self.backends(), ["bark"])
        self.assertIn("Restore storage access", json.loads(self.server.requests[-1]["body"])["body"])
        rows = json.loads(self.page("callers", "--json").stdout)
        self.assertEqual(rows[0]["outcomes"], {"deferred": 1, "sent": 1, "suppressed": 1})

    def test_backend_failure_is_audited_and_retry_is_not_suppressed(self):
        self.server.failing = {"bark", "ntfy"}
        self.assertEqual(self.page("--source", "worker", "Build failed; investigation needed").returncode, 1)
        self.server.failing = set()
        self.assertEqual(self.page("--source", "worker", "Build failed; investigation needed").returncode, 0)
        rows = json.loads(self.page("history", "--json").stdout)
        self.assertEqual([r["outcome"] for r in rows], ["failed", "sent"])

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

    def test_wake_refuses_short_or_placeholder_messages_without_sending(self):
        for text in ("", "   ", "x" * 19, "  " + "x" * 19 + "  ",
                     "summary", "message", "body", "text", "test", "todo", "tbd", "placeholder"):
            for message in (text, "  " + text.upper() + "  "):
                with self.subTest(message=message):
                    result = self.page("--sev", "wake", "--", message)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("page: refusing wake:", result.stderr)
                    self.assertEqual(self.server.requests, [])
                    self.assertFalse((self.root / "state/sent.jsonl").exists())

    def test_wake_refuses_placeholder_dedupe_without_sending(self):
        for key in ("key", "dedupe", "name", "id", "todo"):
            for dedupe in (key, "  " + key.upper() + "  "):
                with self.subTest(dedupe=dedupe):
                    result = self.page("--sev", "wake", "--dedupe", dedupe,
                                       "Production is down; restart needed")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("--dedupe is a placeholder", result.stderr)
                    self.assertEqual(self.server.requests, [])
                    self.assertFalse((self.root / "state/sent.jsonl").exists())

    def test_wake_refuses_unfilled_templates_without_sending(self):
        for template in ("{summary}", "<summary>", "$SUMMARY", "%s", "${SUMMARY}",
                         "{}", "<>", "{multi\nline}"):
            with self.subTest(template=template):
                # Even a template past the transmitted text's truncation limit must be refused.
                result = self.page("--sev", "wake", "Production needs attention: " + "x " * 130 + template)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("message contains an unfilled template", result.stderr)
                self.assertEqual(self.server.requests, [])
                self.assertFalse((self.root / "state/sent.jsonl").exists())

    def test_wake_accepts_twenty_characters_after_stripping(self):
        result = self.page("--sev", "wake", "--dedupe", "incident-42", "  " + "x " * 9 + "xx  ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark"])
        self.assertEqual(json.loads(self.server.requests[0]["body"])["level"], "critical")

    def test_info_allows_wake_placeholders_and_templates(self):
        for text in ("summary", "{summary}", "<summary>", "$SUMMARY", "%s"):
            result = self.page("--dedupe", "key", "--window", "0", text)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.server.requests[-1]["body"].decode(), text)
        self.assertEqual(self.backends(), ["ntfy"] * 5)

    def test_sent_rows_record_caller_basenames(self):
        for args in (("Build finished",), ("--sev", "wake", "Production is down; restart needed"),
                     ("test", "--sev", "wake")):
            result = self.page(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        entries = [json.loads(line) for line in (self.root / "state/sent.jsonl").read_text().splitlines()]
        self.assertEqual(len(entries), 3)
        for entry in entries:
            self.assertEqual(entry["ppid"], os.getpid())
            self.assertTrue(entry["parent_comm"])
            self.assertEqual(entry["parent_comm"], Path(entry["parent_comm"]).name)
            self.assertEqual(entry["cwd"], Path.cwd().name)

    def test_caller_lookup_is_bounded_private_and_fails_open(self):
        loader = importlib.machinery.SourceFileLoader("page_module", str(PAGE))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader("page_module", loader))
        loader.exec_module(module)
        with unittest.mock.patch.object(module.os, "getppid", return_value=123), \
                unittest.mock.patch.object(module.subprocess, "run") as run:
            run.return_value.stdout = "/some/private/path/zsh\n"
            self.assertEqual(module.caller_fields(), {"ppid": 123, "parent_comm": "zsh", "cwd": Path.cwd().name})
            run.assert_called_once_with(["ps", "-o", "comm=", "-p", "123"],
                                        capture_output=True, text=True, timeout=0.5, check=True)
            for error in (FileNotFoundError(), subprocess.TimeoutExpired("ps", 0.5),
                          subprocess.CalledProcessError(1, "ps"), UnicodeError()):
                with self.subTest(error=type(error).__name__):
                    run.side_effect = error
                    self.assertEqual(module.caller_fields()["parent_comm"], "")
            with unittest.mock.patch.object(module.Path, "cwd", side_effect=FileNotFoundError()):
                self.assertEqual(module.caller_fields()["cwd"], "")

    def test_this_machines_own_name_stays_readable(self):
        name = "DavidMacSudioM1s-Mac-Studio"
        loader = importlib.machinery.SourceFileLoader("page_module", str(PAGE))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader("page_module", loader))
        loader.exec_module(module)
        with unittest.mock.patch("socket.gethostname", return_value=f"{name}.local"):
            self.assertEqual(module.mask(f"page test from {name}"), f"page test from {name}")
            self.assertEqual(module.mask("x DavidMacSudioM1s-Mac-Studio7 y"), "x [masked] y")

    def test_wake_rings_on_bark(self):
        result = self.page("--sev", "wake", "--url", "https://example.test/card", "Lead is down; takeover needs attention")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark"])
        body = json.loads(self.server.requests[0]["body"])
        self.assertEqual(self.server.requests[0]["secret"], BARK_KEY)
        self.assertEqual((body["level"], body["call"], body["body"]), ("critical", "1", "Lead is down; takeover needs attention"))
        self.assertEqual(body["url"], "https://example.test/card")

    def test_info_goes_to_ntfy_at_normal_priority(self):
        self.assertEqual(self.page("build finished").returncode, 0)
        self.assertEqual(self.backends(), ["ntfy"])
        self.assertEqual(self.server.requests[0]["headers"]["Priority"], "3")
        self.assertEqual(self.server.requests[0]["body"], b"build finished")

    def test_wake_falls_back_to_max_priority_ntfy(self):
        self.server.failing.add("bark")
        result = self.page("--sev", "wake", "Lead is down; takeover needs attention")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark", "ntfy"])
        self.assertEqual(self.server.requests[1]["headers"]["Priority"], "5")
        self.assertIn("bark: http 500", result.stderr)
        self.assertIn("via ntfy, may not have rung", result.stdout)

    def test_a_wake_that_rang_does_not_hedge_and_a_quiet_test_says_so(self):
        self.assertEqual(self.page("--sev", "wake", "Lead is down; takeover needs attention").stdout.strip(), "page: sent sev=wake via bark")
        self.assertIn("via bark, quiet", self.page("test", "--sev", "wake").stdout)

    def test_missing_backend_is_skipped(self):
        (self.root / "config/secrets/bark-key").unlink()
        self.assertEqual(self.page("--sev", "wake", "Lead is down; takeover needs attention").returncode, 0)
        self.assertEqual(self.backends(), ["ntfy"])

    def test_no_backend_set_up_fails(self):
        (self.root / "config/secrets/bark-key").unlink()
        (self.root / "config/secrets/ntfy-topic").unlink()
        result = self.page("hello")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no backend set up", result.stderr)

    def test_repeat_is_suppressed_and_a_new_key_is_not(self):
        self.page("--sev", "wake", "--dedupe", "lead", "Lead is down; takeover needs attention")
        repeat = self.page("--sev", "wake", "--dedupe", "lead", "lead is still down: incident needs attention")
        self.assertEqual(repeat.returncode, 0)
        self.assertIn("suppressed", repeat.stdout)
        self.assertEqual(len(self.server.requests), 1)
        self.page("--sev", "wake", "--dedupe", "prod", "prod tripped: incident needs attention")
        self.assertEqual(len(self.server.requests), 2)

    def test_failed_send_is_not_recorded_so_a_retry_goes_out(self):
        self.server.failing.update({"bark", "ntfy"})
        first = self.page("--sev", "wake", "--dedupe", "lead", "Lead is down; takeover needs attention")
        self.assertEqual(first.returncode, 1)
        self.server.failing.clear()
        self.assertEqual(self.page("--sev", "wake", "--dedupe", "lead", "Lead is down; takeover needs attention").returncode, 0)
        self.assertEqual(self.backends()[-1], "bark")

    def test_wake_over_the_hourly_cap_goes_out_as_info(self):
        for number in range(6):
            self.assertEqual(self.page("--sev", "wake", f"alert {number}: incident needs attention").returncode, 0)
        result = self.page("--sev", "wake", "alert 7: incident needs attention")
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
        failed = self.page("--sev", "wake", "Lead is down; takeover needs attention")
        self.server.failing.clear()
        sent = self.page("--sev", "wake", "Lead is down; takeover needs attention")
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
        self.page("--sev", "wake", "first: incident needs attention")
        self.assertEqual(self.backends(), ["ntfy"])
        self.page("--sev", "wake", "second: incident needs attention")
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
        self.assertEqual(self.page("--sev", "wake", "Lead is down; takeover needs attention").returncode, 0)
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
                self.assertIn("ignored", result.stdout)

    def test_a_bad_setting_keeps_the_rest_of_the_config(self):
        elsewhere = self.root / "managed-key"
        elsewhere.write_text("elsewhere-key\n")
        (self.root / "config/secrets/bark-key").unlink()
        for bad in ({"typo_key": 1}, {"wake_cap_per_hour": 0}, {"routes": {"wake": []}}, {"windows": 5}):
            with self.subTest(bad=bad):
                self.write_config({"backends": {"bark": {"secret_file": str(elsewhere)}}, **bad})
                self.server.requests.clear()
                result = self.page("--sev", "wake", "--window", "0", "Lead is down; takeover needs attention")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ignored", result.stderr)
                self.assertEqual(self.server.requests[0]["secret"], "elsewhere-key")

    def test_a_bad_backend_setting_keeps_the_other_backend_settings(self):
        elsewhere = self.root / "managed-key"
        elsewhere.write_text("elsewhere-key\n")
        (self.root / "config/secrets/bark-key").unlink()
        self.write_config({"backends": {"bark": {"secret_file": str(elsewhere), "volume": 11}}})
        result = self.page("--sev", "wake", "Lead is down; takeover needs attention")
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
        result = self.page("--sev", "wake", "Lead is down; takeover needs attention", config_dir=directory)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ignored", result.stderr)
        self.assertIn("no backend set up", result.stderr)

    def test_truncated_response_falls_back(self):
        self.server.truncating.add("bark")
        result = self.page("--sev", "wake", "Lead is down; takeover needs attention")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backends(), ["bark", "ntfy"])

    def test_unusable_state_directory_still_sends(self):
        (self.root / "state").write_text("a file where the directory should be")
        result = self.page("--sev", "wake", "Lead is down; takeover needs attention")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state unavailable", result.stderr)
        self.assertEqual(self.backends(), ["bark"])

    def test_malformed_state_lines_are_ignored(self):
        (self.root / "state").mkdir()
        (self.root / "state/sent.jsonl").write_text('[]\n{"at": 1}\n{"at": "x", "key": 1}\nnot json\n')
        self.assertEqual(self.page("--sev", "wake", "Lead is down; takeover needs attention").returncode, 0)
        self.assertEqual(self.page("status").returncode, 0)
        self.assertEqual(self.backends(), ["bark"])

    def test_a_held_lock_delays_a_page_but_cannot_stop_it(self):
        (self.root / "state").mkdir()
        with open(self.root / "state/lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.page("--sev", "wake", "Lead is down; takeover needs attention")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state unavailable", result.stderr)
        self.assertEqual(self.backends(), ["bark"])

    def test_url_keeps_its_path_and_loses_login_and_tokens(self):
        self.page(
            "--sev", "wake",
            "--url", "https://me:pw@example.test/owner/repo/pull/12345/long-readable-path?sig=abcdefghijklmnopqrstuvwxyz012345&a=1",
            "Lead is down; takeover needs attention",
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
        self.page("--sev", "wake", "--url", " https://example.test/incident room ", "Lead is down; takeover needs attention")
        self.assertEqual(json.loads(self.server.requests[0]["body"])["url"], "https://example.test/incident%20room")

    def test_repeats_downgraded_by_the_cap_are_still_suppressed(self):
        self.write_config({"wake_cap_per_hour": 1})
        self.page("--sev", "wake", "first: incident needs attention")
        self.page("--sev", "wake", "--dedupe", "looping", "second: incident needs attention")
        repeat = self.page("--sev", "wake", "--dedupe", "looping", "second again: incident needs attention")
        self.assertIn("suppressed", repeat.stdout)
        self.assertEqual(self.backends(), ["bark", "ntfy"])

    def test_quiet_tests_do_not_use_up_the_cap(self):
        self.write_config({"wake_cap_per_hour": 1})
        self.page("test", "--sev", "wake")
        self.page("test", "--sev", "wake")
        self.page("--sev", "wake", "Lead is down; takeover needs attention")
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
            "Sol token: unavailable on m1",
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
            "token: abcdefghijklmno",
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
        self.page("--sev", "wake", "--url", "https://example.test/c?card=agent-fleet-resilience-david-10-30", "x: incident needs attention")
        self.assertEqual(
            json.loads(self.server.requests[0]["body"])["url"],
            "https://example.test/c?card=agent-fleet-resilience-david-10-30",
        )

    def test_invalid_json_still_finds_secrets_at_the_default_paths(self):
        # Status only: a page here would go to the default public servers.
        (self.root / "config/config.json").write_text("{not json")
        result = self.page("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ignored", result.stdout)
        self.assertIn("bark: set up", result.stdout)

    def test_status_exit_says_whether_anything_can_send(self):
        self.assertEqual(self.page("status").returncode, 0)
        (self.root / "config/secrets/bark-key").unlink()
        self.assertEqual(self.page("status").returncode, 0)
        (self.root / "config/secrets/ntfy-topic").unlink()
        self.assertEqual(self.page("status").returncode, 1)

    def test_status_fails_when_the_only_secret_is_on_no_route(self):
        (self.root / "config/secrets/bark-key").unlink()
        self.write_config({"routes": {"wake": ["bark"], "info": ["bark"]}})
        self.assertEqual(self.page("status").returncode, 1)

    def test_status_shows_the_key_as_given_and_omits_a_missing_source(self):
        self.page("--dedupe", "lead-down", "--source", "failover", "Lead is down; takeover needs attention")
        self.page("no key here")
        lines = self.page("status").stdout.splitlines()
        self.assertIn("key=lead-down source=failover", lines[-2])
        self.assertNotIn("source=", lines[-1])

    def test_a_window_past_a_day_still_suppresses(self):
        self.page("--dedupe", "weekly", "--window", "172800", "first")
        log = self.root / "state/sent.jsonl"
        entry = json.loads(log.read_text())
        entry["at"] -= 90000
        log.write_text(json.dumps(entry) + "\n")
        self.assertIn("suppressed", self.page("--dedupe", "weekly", "--window", "172800", "first").stdout)
        entry["at"] -= 90000
        log.write_text(json.dumps(entry) + "\n")
        self.assertIn("sent", self.page("--dedupe", "weekly", "--window", "172800", "first").stdout)

    def test_a_capped_wake_says_on_stdout_that_it_did_not_ring(self):
        self.write_config({"wake_cap_per_hour": 1})
        self.page("--sev", "wake", "first: incident needs attention")
        self.assertIn("via ntfy, capped, did not ring", self.page("--sev", "wake", "second: incident needs attention").stdout)

    def test_help_lists_the_subcommands(self):
        output = self.page("--help").stdout
        self.assertIn("page status", output)
        self.assertIn("page test", output)

    def test_a_separator_sends_the_word_status_as_a_message(self):
        self.assertEqual(self.page("--", "status").returncode, 0)
        self.assertEqual(self.server.requests[0]["body"], b"status")

    def test_url_path_loses_known_credentials_and_keeps_hashes(self):
        self.page("--sev", "wake", "--url", "https://example.test/ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890/x", "a: incident needs attention")
        self.assertEqual(json.loads(self.server.requests[-1]["body"])["url"], "https://example.test/[masked]/x")
        self.page("--sev", "wake", "--url", "https://example.test/download/token=abc123", "c: incident needs attention")
        self.assertNotIn("abc123", json.loads(self.server.requests[-1]["body"])["url"])
        commit = "https://example.test/o/r/commit/0ee5a15c0ee5a15c0ee5a15c0ee5a15c0ee5a15c"
        self.page("--sev", "wake", "--url", commit, "b: incident needs attention")
        self.assertEqual(json.loads(self.server.requests[-1]["body"])["url"], commit)

    def test_status_rejects_extra_arguments(self):
        self.assertEqual(self.page("status", "unexpected").returncode, 2)

    def test_status_reports_a_missing_secret(self):
        (self.root / "config/secrets/ntfy-topic").unlink()
        output = self.page("status").stdout
        self.assertIn("bark: set up", output)
        self.assertIn("ntfy: no secret file", output)


if __name__ == "__main__":
    unittest.main()
