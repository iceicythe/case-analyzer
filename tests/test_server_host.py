"""The listen address must stay overridable for container runs."""
import tempfile
import threading
import unittest
from pathlib import Path

from app import server
from app.paths import PROJECT_ROOT
from app.workspace import initialize


class ServerHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-host-')
        self.root = initialize(Path(self.temp.name))
        self.context = server.WorkbenchContext(batch_dir=self.root / 'index', data_root=self.root / 'data',
            annotations_root=self.root / 'annotations', evaluation_output=self.root / 'evaluations',
            model_reviews_root=self.root / 'model_reviews', automatic_review=False)

    def tearDown(self):
        self.temp.cleanup()

    def serve_on(self, host):
        """Boot one server on an ephemeral port and stop it cleanly."""
        http = server.create_server(self.context, 0, host)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop, http, thread)
        return http

    @staticmethod
    def stop(http, thread):
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)

    def test_default_host_stays_loopback(self):
        http = self.serve_on('127.0.0.1')
        self.assertEqual(http.server_address[0], '127.0.0.1')

    def test_host_override_binds_given_address(self):
        http = self.serve_on('0.0.0.0')
        self.assertEqual(http.server_address[0], '0.0.0.0')

    def test_public_sources_embed_no_local_paths(self):
        # Guardrail: public sources must not embed personal absolute paths.
        text = (PROJECT_ROOT / 'app' / '__main__.py').read_text(encoding='utf-8')
        self.assertNotIn('C:\\', text)


if __name__ == '__main__':
    unittest.main()
