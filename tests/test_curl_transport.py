import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dag_builder.client import APIClient, CallFailure
from dag_builder.config import Config


class CurlTransportTests(unittest.TestCase):
    def test_secret_stdin_only_and_exact_body(self):
        key = "synthetic-test-secret"
        client = APIClient(Config(transport="curl"), key)
        body = {"messages": [{"content": 'A newline\nquote " and unicode 原文'}], "thinking": {"type": "enabled"}}
        with patch("dag_builder.client.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=b'{"ok":true}\n200', stderr=b"")
            self.assertEqual(client.complete(body), {"ok": True})
        command = run.call_args.args[0]
        self.assertNotIn(key, " ".join(command))
        self.assertEqual(command[1], "--disable")
        self.assertNotIn("--insecure", command)
        self.assertNotIn("--location", command)
        lines = run.call_args.kwargs["input"].decode().splitlines()
        raw = next(line.removeprefix("data-binary = ") for line in lines if line.startswith("data-binary = "))
        self.assertEqual(json.loads(json.loads(raw)), body)
        self.assertNotIn("transport", Config().to_dict())

    def test_http_errors_and_unknown_cost_fail_closed(self):
        for code, category in ((401,"authentication"), (429,"rate_limit"), (502,"uncertain_remote_state"), (302,"http_error")):
            client = APIClient(Config(transport="curl"), "synthetic-test-secret")
            with patch("dag_builder.client.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=f"{{}}\n{code}".encode(), stderr=b"")):
                with self.assertRaises(CallFailure) as caught:
                    client.complete({})
                self.assertEqual(caught.exception.category, category)
        with patch("dag_builder.client.subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 1)):
            with self.assertRaises(CallFailure) as caught:
                APIClient(Config(transport="curl"), "synthetic-test-secret").complete({})
            self.assertEqual(caught.exception.category, "uncertain_remote_state")


if __name__ == "__main__":
    unittest.main()
