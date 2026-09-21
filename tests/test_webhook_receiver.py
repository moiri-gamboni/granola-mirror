"""webhook_receiver.py — signature verification (unchanged, pinned) and the hardening.

_sig_ok is the security core and is asserted against the Standard Webhooks shapes: a whsec
secret and a raw one, a wrong signature, a stale and a malformed timestamp, missing headers,
and a multi-entry signature header. The hardening — threaded server with a body
cap, a required GRANOLA_MIRROR, note.regenerated in the handled set, and the runner receiving
the mirror as argv rather than an interpolated shell string — is exercised against a live
in-process server and the module's own refusal paths.
"""
import base64
import hashlib
import hmac
import http.client
import os
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webhook_receiver as wr  # noqa: E402  (importable — the server only starts under __main__)

from _harness import GranolaSandbox  # noqa: E402


def sign(secret, wid, ts, body, scheme="v1"):
    key = base64.b64decode(secret[6:]) if secret.startswith("whsec_") else secret.encode()
    mac = hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "%s,%s" % (scheme, base64.b64encode(mac).decode())


WHSEC = "whsec_" + base64.b64encode(b"a very secret key, 32 bytes long!").decode()


class Signature(GranolaSandbox):
    def setUp(self):
        super().setUp()
        self.secret_path = os.path.join(self.tmp, "webhook-secret")
        self._orig = wr.SECRET_FILE
        wr.SECRET_FILE = self.secret_path
        self.addCleanup(setattr, wr, "SECRET_FILE", self._orig)

    def use(self, secret):
        with open(self.secret_path, "w") as f:
            f.write(secret)

    def headers(self, wid="msg_1", ts=None, sig=None):
        h = {}
        if wid is not None:
            h["webhook-id"] = wid
        if ts is not None:
            h["webhook-timestamp"] = ts
        if sig is not None:
            h["webhook-signature"] = sig
        return h

    def test_a_valid_whsec_signature_passes(self):
        self.use(WHSEC)
        ts, body = str(int(time.time())), b'{"event_type":"note.generated"}'
        h = self.headers("msg_1", ts, sign(WHSEC, "msg_1", ts, body))
        self.assertTrue(wr._sig_ok(h, body))

    def test_a_raw_secret_is_also_accepted(self):
        self.use("plainsecret")
        ts, body = str(int(time.time())), b"{}"
        h = self.headers("msg_1", ts, sign("plainsecret", "msg_1", ts, body))
        self.assertTrue(wr._sig_ok(h, body))

    def test_a_wrong_signature_fails(self):
        self.use(WHSEC)
        ts, body = str(int(time.time())), b"{}"
        h = self.headers("msg_1", ts, "v1," + base64.b64encode(b"x" * 32).decode())
        self.assertFalse(wr._sig_ok(h, body))

    def test_a_stale_timestamp_fails(self):
        self.use(WHSEC)
        ts, body = str(int(time.time()) - wr.TOLERANCE_S - 60), b"{}"
        h = self.headers("msg_1", ts, sign(WHSEC, "msg_1", ts, body))
        self.assertFalse(wr._sig_ok(h, body))

    def test_a_malformed_timestamp_fails(self):
        self.use(WHSEC)
        body = b"{}"
        h = self.headers("msg_1", "not-a-number", sign(WHSEC, "msg_1", "not-a-number", body))
        self.assertFalse(wr._sig_ok(h, body))

    def test_missing_headers_fail(self):
        self.use(WHSEC)
        self.assertFalse(wr._sig_ok(self.headers(wid=None), b"{}"))

    def test_a_multi_entry_signature_header_passes_if_one_entry_matches(self):
        self.use(WHSEC)
        ts, body = str(int(time.time())), b"{}"
        good = sign(WHSEC, "msg_1", ts, body)
        h = self.headers("msg_1", ts, "v1,%s %s" % (base64.b64encode(b"z" * 32).decode(), good))
        self.assertTrue(wr._sig_ok(h, body))


class Server(GranolaSandbox):
    """A live threaded server, module globals pointed at the sandbox, _kick stubbed."""

    def setUp(self):
        super().setUp()
        self.secret_path = os.path.join(self.tmp, "webhook-secret")
        with open(self.secret_path, "w") as f:
            f.write(WHSEC)
        self.saved = {k: getattr(wr, k) for k in
                      ("SECRET_FILE", "MIRROR", "PENDING", "EVENTS", "LOCKS", "_kick")}
        wr.SECRET_FILE = self.secret_path
        wr.MIRROR = self.mirror
        wr.PENDING = os.path.join(self.state, "granola-webhook-pending")
        wr.EVENTS = os.path.join(self.state, "granola-webhook-events.jsonl")
        wr.LOCKS = os.path.join(self.home, ".locks")
        self.kicks = []
        wr._kick = lambda: self.kicks.append(1)
        self.addCleanup(lambda: [setattr(wr, k, v) for k, v in self.saved.items()])

        self.srv = wr.http.server.ThreadingHTTPServer(("127.0.0.1", 0), wr.Handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def post(self, body=b"{}", headers=None, content_length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/granola", skip_host=False, skip_accept_encoding=True)
        cl = content_length if content_length is not None else str(len(body))
        conn.putheader("Content-Length", cl)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            try:
                conn.send(body)
            except Exception:
                pass
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def signed(self, event_type="note.generated"):
        ts = str(int(time.time()))
        body = ('{"event_type":"%s","note_id":"n1"}' % event_type).encode()
        return body, {"webhook-id": "msg_1", "webhook-timestamp": ts,
                      "webhook-signature": sign(WHSEC, "msg_1", ts, body)}

    def test_an_oversized_content_length_is_refused_before_reading(self):
        self.assertEqual(self.post(body=b"x" * (wr.MAX_BODY + 1)), 413)
        self.assertEqual(self.kicks, [])

    def test_a_non_numeric_content_length_is_a_bad_request(self):
        self.assertEqual(self.post(body=b"", content_length="not-a-number"), 400)

    def test_a_bad_signature_is_unauthorized(self):
        body, h = self.signed()
        h["webhook-signature"] = "v1,wrong"
        self.assertEqual(self.post(body, h), 401)
        self.assertEqual(self.kicks, [])

    def test_a_valid_event_kicks_the_pipeline(self):
        body, h = self.signed("note.generated")
        self.assertEqual(self.post(body, h), 204)
        self.assertEqual(self.kicks, [1])

    def test_note_regenerated_is_handled(self):
        body, h = self.signed("note.regenerated")
        self.assertEqual(self.post(body, h), 204)
        self.assertEqual(self.kicks, [1], "note.regenerated must be in the handled set")

    def test_an_unhandled_event_is_logged_but_does_not_kick(self):
        body, h = self.signed("note.deleted")
        self.assertEqual(self.post(body, h), 204)
        self.assertEqual(self.kicks, [])
        with open(wr.EVENTS) as f:
            self.assertIn("note.deleted", f.read())


class RunnerArgv(unittest.TestCase):
    def test_the_runner_takes_refresh_and_mirror_as_argv_not_interpolation(self):
        self.assertIn('"$1" --commit "$2"', wr.RUNNER)
        self.assertNotIn("granola-refresh\"", wr.RUNNER)  # the dropped caller-side flock target

    def test_kick_passes_refresh_and_mirror_positionally(self):
        wr.MIRROR = wr.MIRROR or "/tmp/mirror"
        captured = {}

        def fake_popen(argv, **kw):
            captured["argv"] = argv
            class P:  # noqa: E306
                pass
            return P()
        orig = wr.subprocess.Popen
        wr.subprocess.Popen = fake_popen
        try:
            wr._kick()
        finally:
            wr.subprocess.Popen = orig
        argv = captured["argv"]
        self.assertEqual(argv[:3], ["bash", "-c", wr.RUNNER])
        self.assertEqual(argv[3:], ["_", wr.REFRESH, wr.MIRROR])


class MainRefusals(unittest.TestCase):
    """The __main__ preflights: no secret, and no GRANOLA_MIRROR — both must exit, not serve."""

    MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "webhook_receiver.py")

    def run_main(self, env):
        return subprocess.run(("python3", self.MODULE), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)

    def test_no_signing_secret_refuses(self):
        import tempfile
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home)
        env.pop("GRANOLA_MIRROR", None)
        r = self.run_main(env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("signing secret", r.stdout)

    def test_missing_mirror_refuses_with_three_facts(self):
        import tempfile
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".config", "granola"))
        with open(os.path.join(home, ".config", "granola", "webhook-secret"), "w") as f:
            f.write(WHSEC)
        env = dict(os.environ, HOME=home)
        env.pop("GRANOLA_MIRROR", None)
        r = self.run_main(env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GRANOLA_MIRROR", r.stdout)

    def test_the_env_file_supplies_the_mirror_path(self):
        """~/.config/granola/env provides GRANOLA_MIRROR when the environment doesn't:
        the refusal names the env-file path, proving the file was consumed."""
        import tempfile
        home = tempfile.mkdtemp()
        cfg = os.path.join(home, ".config", "granola")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "webhook-secret"), "w") as f:
            f.write("whsec_c2VjcmV0\n")
        with open(os.path.join(cfg, "env"), "w") as f:
            f.write("# per-deployment constants\nexport GRANOLA_MIRROR='/nonexistent/mirror-from-env-file'\n")
        env = dict(os.environ, HOME=home)
        env.pop("GRANOLA_MIRROR", None)
        r = self.run_main(env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/nonexistent/mirror-from-env-file", r.stdout)


class EnvFileParsing(unittest.TestCase):
    def test_values_comments_export_and_quotes(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "env")
        with open(path, "w") as f:
            f.write("# constants\nexport A='/a b'\nB=\"/b\"\nC=\nD=/d\n")
        self.assertEqual(wr._env_file_value("A", path), "/a b")
        self.assertEqual(wr._env_file_value("B", path), "/b")
        self.assertIsNone(wr._env_file_value("C", path))
        self.assertEqual(wr._env_file_value("D", path), "/d")
        self.assertIsNone(wr._env_file_value("MISSING", path))
        self.assertIsNone(wr._env_file_value("A", path + ".absent"))


if __name__ == "__main__":
    unittest.main()
