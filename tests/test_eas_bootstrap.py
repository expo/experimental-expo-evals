import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class EASBootstrapTest(unittest.TestCase):
    def test_bootstrap_rejects_machines_outside_eas_before_mutation(self):
        bootstrap = Path(__file__).resolve().parents[1] / "environments/eas/bootstrap.sh"
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            marker = tools / "mutated"
            scripts = {
                "uname": "printf 'Linux\\n'",
                "mkdir": ': > "$MUTATION_MARKER"',
                "sudo": ': > "$MUTATION_MARKER"',
            }
            for name, command in scripts.items():
                path = tools / name
                path.write_text(f"#!/bin/sh\n{command}\n")
                path.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(bootstrap)],
                env={**os.environ, "PATH": str(tools), "MUTATION_MARKER": str(marker)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("requires a macOS worker", result.stderr)
            self.assertFalse(marker.exists())
            (tools / "uname").write_text("#!/bin/sh\nprintf Darwin\n")
            result = subprocess.run(
                ["/bin/bash", str(bootstrap)],
                env={"PATH": str(tools), "MUTATION_MARKER": str(marker)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("only inside a disposable EAS workflow", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
