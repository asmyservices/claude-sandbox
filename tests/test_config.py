import unittest

from helpers import REPO_DIR, TempConfig

from sandboxlib.config import ConfigError, load_config
from sandboxlib.render import compose, systemd_units


def config_with(actions: str = "", extra: str = "") -> str:
    return "name: test-box\n" + extra + "actions:\n" + actions


class ExampleConfigTest(unittest.TestCase):
    def test_example_config_is_valid(self):
        cfg = load_config(REPO_DIR / "config.example.yaml")
        self.assertEqual(cfg.name, "my-server")
        self.assertIn("deploy", cfg.actions)
        self.assertTrue(cfg.actions["deploy"].approval)
        self.assertEqual(cfg.actions["status"].args["service"].choices, ["my-bot"])
        self.assertEqual(cfg.actions["crontab-install"].stdin, "content")


class ValidationTest(unittest.TestCase):
    def assertConfigError(self, body: str, fragment: str):
        tmp = TempConfig(body)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(ConfigError) as ctx:
            tmp.load()
        self.assertIn(fragment, str(ctx.exception))

    def test_unknown_top_level_key(self):
        self.assertConfigError("name: test-box\nmodle: x\n", "modle")

    def test_x_keys_are_ignored(self):
        tmp = TempConfig("name: test-box\nx-anything: [1, 2]\n")
        self.addCleanup(tmp.cleanup)
        self.assertEqual(tmp.load().name, "test-box")

    def test_bad_name(self):
        self.assertConfigError("name: Test Box\n", "name")

    def test_undefined_placeholder(self):
        self.assertConfigError(config_with(
            "  a:\n    command: [echo, '{missing}']\n"), "{missing}")

    def test_text_arg_not_allowed_in_argv(self):
        self.assertConfigError(config_with(
            "  a:\n    command: [echo, '{t}']\n    args:\n      t: {text: {max_bytes: 10}}\n"),
            "only be used as stdin")

    def test_unused_argument(self):
        self.assertConfigError(config_with(
            "  a:\n    command: [echo]\n    args:\n      s: {choices: [x]}\n"), "never used")

    def test_invalid_default(self):
        self.assertConfigError(config_with(
            "  a:\n    command: [echo, '{n}']\n    args:\n      n: {integer: {max: 5}, default: 9}\n"),
            "default")

    def test_argument_needs_one_type(self):
        self.assertConfigError(config_with(
            "  a:\n    command: [echo, '{s}']\n    args:\n      s: {choices: [x], integer: {}}\n"),
            "exactly one")

    def test_reserved_mount_path(self):
        self.assertConfigError(
            "name: test-box\nmounts:\n  - {host: /tmp, container: /queue/x}\n", "reserved")

    def test_home_is_a_builtin(self):
        tmp = TempConfig(config_with("  a:\n    command: [ls, '{home}/x']\n"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("a", tmp.load().actions)


class RenderTest(unittest.TestCase):
    def setUp(self):
        tmp = TempConfig(
            "name: test-box\nmodel: claude-opus-5-5\n"
            "mounts:\n  - {host: /tmp, container: /mnt/tmp}\n"
            "git: {ssh_key: /tmp/key}\n")
        self.addCleanup(tmp.cleanup)
        self.cfg = tmp.load()

    def test_compose_isolation_settings(self):
        service = compose(self.cfg, "0:0")["services"]["claude"]
        self.assertIn(f"{self.cfg.results_dir}:/queue/results:ro", service["volumes"])
        self.assertIn("/tmp:/mnt/tmp:ro", service["volumes"])
        self.assertIn("/tmp/key:/run/secrets/git_key:ro", service["volumes"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["environment"]["IS_SANDBOX"], "1")
        self.assertNotIn("docker.sock", " ".join(service["volumes"]))

    def test_non_root_user_is_not_marked_sandbox_root(self):
        service = compose(self.cfg, "1000:1000")["services"]["claude"]
        self.assertNotIn("IS_SANDBOX", service["environment"])

    def test_path_unit_watches_requests(self):
        units = systemd_units(self.cfg)
        path_unit = units["claude-sandbox-test-box-broker.path"]
        self.assertIn(f"DirectoryNotEmpty={self.cfg.requests_dir}", path_unit)
        self.assertIn("Unit=claude-sandbox-test-box-broker.service", path_unit)


if __name__ == "__main__":
    unittest.main()
