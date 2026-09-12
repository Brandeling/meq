"""Packaging metadata and the public installation promise stay aligned."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_documented_python_floor_matches_project_metadata(self):
        metadata = re.search(
            r'^requires-python = "([^"]+)"$',
            (ROOT / "pyproject.toml").read_text(),
            re.MULTILINE,
        )
        documented = re.search(
            r"Python (\d+\.\d+) or newer", (ROOT / "README.md").read_text()
        )

        self.assertIsNotNone(metadata)
        self.assertIsNotNone(documented)
        self.assertEqual(metadata.group(1), ">=3.10")
        self.assertEqual(documented.group(1), "3.10")


if __name__ == "__main__":
    unittest.main()
