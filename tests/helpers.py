import sys
import tempfile
import textwrap
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

from sandboxlib.config import load_config  # noqa: E402


class TempConfig:
    def __init__(self, body: str):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.path = self.root / "config.yaml"
        self.path.write_text(textwrap.dedent(body))

    def load(self):
        return load_config(self.path)

    def cleanup(self):
        self._dir.cleanup()
