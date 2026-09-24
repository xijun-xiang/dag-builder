"""The B1 transfer archive contains no macOS sidecar source files."""

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from dag_builder.storage import write_bytes_once, write_once
from package_t2ance_canary7 import archive_package


class T2anceArchiveTests(unittest.TestCase):
    def test_only_manifest_files_are_archived(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            package = root / "package"
            payload = b"print(1)\n"
            write_bytes_once(package / "code/dag_builder/example.py", payload)
            write_bytes_once(package / "code/dag_builder/._example.py", b"macOS sidecar")
            write_once(package / "package-manifest.json", {
                "protocol": "t2ance-canary7-package-v1",
                "files": {"code/dag_builder/example.py": hashlib.sha256(payload).hexdigest()}})
            result = archive_package(package, root / "payload.tar")
            self.assertEqual(result["archive_files"], 2)
            with tarfile.open(root / "payload.tar") as archive:
                self.assertNotIn("code/dag_builder/._example.py", archive.getnames())
                self.assertEqual(archive.extractfile("code/dag_builder/example.py").read(),
                                 payload)
                self.assertEqual(archive.getmember("code/dag_builder/example.py").pax_headers,
                                 {})
            with self.assertRaises(FileExistsError):
                write_bytes_once(root / "payload.tar", b"different")


if __name__ == "__main__":
    unittest.main()
