import json
import os
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

from helpers import REPO_DIR, TempConfig

from sandboxlib import broker

CONFIG = """
name: test-box
broker:
  timeout_seconds: 10
  max_request_bytes: 2048
actions:
  greet:
    description: Say hello.
    command: [echo, "hello {who}"]
    args:
      who: {choices: [world, there]}
  count:
    command: [echo, "{n}"]
    args:
      n: {integer: {min: 1, max: 5}, default: 2}
  cat:
    command: [cat]
    args:
      content: {text: {max_bytes: 50}}
    stdin: content
  gated:
    command: [echo, approved]
    approval: true
  fail:
    command: [sh, -c, "exit 3"]
  slow:
    command: [sleep, "5"]
    timeout: 1
"""


class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = TempConfig(CONFIG)
        self.addCleanup(tmp.cleanup)
        self.cfg = tmp.load()
        broker.ensure_dirs(self.cfg)

    def submit(self, req_id, data):
        path = self.cfg.requests_dir / f"{req_id}.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def result(self, req_id):
        path = self.cfg.results_dir / f"{req_id}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def run_one(self, req_id, data):
        self.submit(req_id, data)
        broker.run_queue(self.cfg)
        return self.result(req_id)

    def assertQueueEmpty(self):
        self.assertEqual(os.listdir(self.cfg.requests_dir), [])


class ActionTest(BrokerTestCase):
    def test_runs_allowed_action(self):
        result = self.run_one("r1", {"action": "greet", "args": {"who": "world"}})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["output"], "hello world\n")
        self.assertQueueEmpty()

    def test_value_outside_choices_is_rejected(self):
        result = self.run_one("r1", {"action": "greet", "args": {"who": "world; touch /tmp/pwned"}})
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("output", result)

    def test_unknown_action_is_rejected(self):
        result = self.run_one("r1", {"action": "rm", "args": {}})
        self.assertEqual(result["status"], "rejected")

    def test_unknown_argument_is_rejected(self):
        result = self.run_one("r1", {"action": "greet", "args": {"who": "world", "extra": "x"}})
        self.assertEqual(result["status"], "rejected")

    def test_missing_argument_is_rejected(self):
        result = self.run_one("r1", {"action": "greet"})
        self.assertEqual(result["status"], "rejected")

    def test_integer_default_and_bounds(self):
        self.assertEqual(self.run_one("r1", {"action": "count"})["output"], "2\n")
        self.assertEqual(self.run_one("r2", {"action": "count", "args": {"n": "4"}})["output"], "4\n")
        self.assertEqual(self.run_one("r3", {"action": "count", "args": {"n": 9}})["status"], "rejected")
        self.assertEqual(self.run_one("r4", {"action": "count", "args": {"n": True}})["status"], "rejected")
        self.assertEqual(self.run_one("r5", {"action": "count", "args": {"n": "1 2"}})["status"], "rejected")

    def test_text_goes_to_stdin_and_is_not_echoed_in_args(self):
        result = self.run_one("r1", {"action": "cat", "args": {"content": "secret-ish"}})
        self.assertEqual(result["output"], "secret-ish")
        self.assertEqual(result["args"]["content"]["bytes"], 10)
        self.assertNotIn("secret-ish", json.dumps(result["args"]))

    def test_text_over_limit_is_rejected(self):
        result = self.run_one("r1", {"action": "cat", "args": {"content": "x" * 51}})
        self.assertEqual(result["status"], "rejected")

    def test_failing_command_reports_exit_code(self):
        result = self.run_one("r1", {"action": "fail"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["exit_code"], 3)

    def test_timeout(self):
        result = self.run_one("r1", {"action": "slow"})
        self.assertEqual(result["status"], "timeout")

    def test_audit_log_records_requests(self):
        self.run_one("r1", {"action": "greet", "args": {"who": "there"}})
        lines = self.cfg.audit_log.read_text().splitlines()
        entry = json.loads(lines[-1])
        self.assertEqual((entry["id"], entry["action"], entry["status"]), ("r1", "greet", "ok"))

    def test_catalog_is_published(self):
        broker.run_queue(self.cfg)
        catalog = json.loads((self.cfg.results_dir / broker.CATALOG_NAME).read_text())
        self.assertEqual(catalog["actions"]["greet"]["args"]["who"]["choices"], ["world", "there"])
        self.assertTrue(catalog["actions"]["gated"]["approval"])


class HostileInputTest(BrokerTestCase):
    def test_symlink_is_not_followed(self):
        os.symlink("/etc/passwd", self.cfg.requests_dir / "r1.json")
        broker.run_queue(self.cfg)
        result = self.result("r1")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("root:", json.dumps(result))
        self.assertTrue(os.path.exists("/etc/passwd"))
        self.assertQueueEmpty()

    def test_directory_entry_is_removed(self):
        (self.cfg.requests_dir / "r1.json").mkdir()
        (self.cfg.requests_dir / "r1.json" / "inner").write_text("x")
        broker.run_queue(self.cfg)
        self.assertEqual(self.result("r1")["status"], "rejected")
        self.assertQueueEmpty()

    def test_fifo_does_not_block(self):
        os.mkfifo(self.cfg.requests_dir / "r1.json")
        broker.run_queue(self.cfg)
        self.assertEqual(self.result("r1")["status"], "rejected")
        self.assertQueueEmpty()

    def test_invalid_names_are_removed_without_results(self):
        self.submit("bad name", {"action": "greet", "args": {"who": "world"}})
        (self.cfg.requests_dir / "notes.txt").write_text("hi")
        broker.run_queue(self.cfg)
        self.assertQueueEmpty()
        self.assertEqual(sorted(os.listdir(self.cfg.results_dir)), [broker.CATALOG_NAME])

    def test_oversized_request_is_rejected(self):
        result = self.run_one("r1", json.dumps({"action": "greet", "pad": "x" * 4000}))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("larger", result["error"])

    def test_invalid_json_is_rejected(self):
        self.assertEqual(self.run_one("r1", "{not json")["status"], "rejected")

    def test_duplicate_id_does_not_overwrite(self):
        self.run_one("r1", {"action": "greet", "args": {"who": "world"}})
        self.run_one("r1", {"action": "greet", "args": {"who": "there"}})
        self.assertEqual(self.result("r1")["output"], "hello world\n")


class ApprovalTest(BrokerTestCase):
    def test_approval_flow(self):
        result = self.run_one("r1", {"action": "gated"})
        self.assertEqual(result["status"], "pending")
        self.assertEqual([p["id"] for p in broker.list_pending(self.cfg)], ["r1"])

        outcome = broker.approve(self.cfg, "r1")
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(self.result("r1")["output"], "approved\n")
        self.assertEqual(broker.list_pending(self.cfg), [])

    def test_deny(self):
        self.run_one("r1", {"action": "gated"})
        broker.deny(self.cfg, "r1", "not now")
        result = self.result("r1")
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["error"], "not now")
        self.assertNotIn("output", result)

    def test_approve_rejects_bad_ids(self):
        with self.assertRaises(broker.RequestError):
            broker.approve(self.cfg, "../../etc/passwd")


class ClientRoundTripTest(BrokerTestCase):
    def setUp(self):
        super().setUp()
        loader = SourceFileLoader("hostctl", str(REPO_DIR / "image" / "hostctl"))
        self.hostctl = module_from_spec(spec_from_loader("hostctl", loader))
        loader.exec_module(self.hostctl)
        self.hostctl.QUEUE = str(self.cfg.queue_dir)
        self.hostctl.CATALOG = str(self.cfg.results_dir / broker.CATALOG_NAME)

    def test_client_request_reaches_broker(self):
        req_id = self.hostctl.submit("greet", {"who": "there"})
        self.assertEqual(os.listdir(self.cfg.tmp_dir), [])
        broker.run_queue(self.cfg)
        self.assertEqual(self.hostctl.read_result(req_id)["output"], "hello there\n")

    def test_file_arguments(self):
        path = self.cfg.root / "input.txt"
        path.write_text("from a file")
        self.assertEqual(self.hostctl.parse_args([f"content=@{path}"]), {"content": "from a file"})


if __name__ == "__main__":
    unittest.main()
