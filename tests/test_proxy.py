import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/v1/models' or self.headers.get('Authorization') != 'Bearer fixture-secret':
            self.send_response(401)
            self.end_headers()
            return
        body = b'{"data":[{"id":"fixture-model"}]}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class ProxyTests(unittest.TestCase):
    def test_localhost_is_rejected_even_if_resolver_is_overridden(self):
        from unittest.mock import patch
        from agy_pool.config import ConfigError
        from agy_pool.proxy import load_proxy_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'proxy.json'
            path.write_text(json.dumps({'schema_version': 1, 'base_url': 'http://localhost:8317/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}), encoding='utf-8')
            with patch('socket.getaddrinfo', side_effect=AssertionError('resolver must not be used')):
                with self.assertRaises(ConfigError):
                    load_proxy_config(path)

    def test_numeric_loopback_validation_and_timeout_edges(self):
        from agy_pool.config import ConfigError
        from agy_pool.proxy import load_proxy_config
        config = {'schema_version': 1, 'base_url': '', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'proxy.json'
            for host, allowed in [('127.0.0.1', True), ('127.1.2.3', True), ('[::1]', True), ('[::ffff:127.0.0.1]', True), ('[::ffff:192.0.2.1]', False), ('127.1', False), ('localhost', False), ('[::2]', False)]:
                config['base_url'] = f'http://{host}:8317/v1'
                path.write_text(json.dumps(config), encoding='utf-8')
                with self.subTest(host=host):
                    if allowed:
                        self.assertEqual(load_proxy_config(path)['base_url'], config['base_url'])
                    else:
                        with self.assertRaises(ConfigError):
                            load_proxy_config(path)
            config['base_url'] = 'http://127.0.0.1:8317/v1'
            for timeout in (True, False, 10**400, -(10**400), 1e309, -1e309):
                config['timeout_s'] = timeout
                path.write_text(json.dumps(config), encoding='utf-8')
                with self.subTest(timeout=str(timeout)), self.assertRaises(ConfigError):
                    load_proxy_config(path)

    def test_probe_disables_environment_proxy_and_redirects(self):
        class RedirectHandler(BaseHTTPRequestHandler):
            requests = []
            def do_GET(self):
                self.requests.append((self.path, self.headers.get('Authorization')))
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/stolen')
                self.end_headers()
            def log_message(self, format, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'proxy.json'
                path.write_text(json.dumps({'schema_version': 1, 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}), encoding='utf-8')
                env = dict(os.environ, FIXTURE_KEY='fixture-secret', http_proxy='http://127.0.0.1:1', HTTP_PROXY='http://127.0.0.1:1', no_proxy='', NO_PROXY='')
                result = subprocess.run([sys.executable, '-m', 'agy_pool', 'proxy-probe', str(path)], capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('HTTP 302', result.stderr)
                self.assertNotIn('fixture-secret', result.stdout + result.stderr)
                self.assertEqual(RedirectHandler.requests, [('/v1/models', 'Bearer fixture-secret')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_huge_timeout_cli_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'proxy.json'
            path.write_text(json.dumps({'schema_version': 1, 'base_url': 'http://127.0.0.1:8317/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 10**400}), encoding='utf-8')
            for command in ('proxy-probe', 'proxy-hermes'):
                args = [sys.executable, '-m', 'agy_pool', command, str(path)]
                if command == 'proxy-hermes':
                    args.append('fixture-model')
                result = subprocess.run(args, capture_output=True, text=True, timeout=10)
                with self.subTest(command=command):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('Timeout must be', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)

    def test_hermes_configuration_output_contains_only_env_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'proxy.json'
            path.write_text(json.dumps({'schema_version': 1, 'base_url': 'http://127.0.0.1:8317/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}), encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'agy_pool', 'proxy-hermes', str(path), 'fixture-model'], capture_output=True, text=True, env=dict(os.environ, FIXTURE_KEY='fixture-secret'), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'model': {'default': 'fixture-model', 'provider': 'custom:agy-local'}, 'providers': {'agy-local': {'api': 'http://127.0.0.1:8317/v1', 'key_env': 'FIXTURE_KEY'}}})
            self.assertNotIn('fixture-secret', result.stdout + result.stderr)

    def test_rejects_remote_and_invalid_config_without_leaking(self):
        from agy_pool.config import ConfigError
        from agy_pool.proxy import load_proxy_config
        good = {'schema_version': 1, 'base_url': 'http://127.0.0.1:8317/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'proxy.json'
            for changes in ({'base_url': 'http://example.com:8317/v1'}, {'base_url': 'http://user:secret@127.0.0.1:8317/v1'}, {'timeout_s': 31}, {'timeout_s': True}, {'unexpected': 'secret'}, {'base_url': 'http://127.0.0.1:8317/v1?secret'}):
                path.write_text(json.dumps(dict(good, **changes)), encoding='utf-8')
                with self.subTest(changes=changes), self.assertRaises(ConfigError) as error:
                    load_proxy_config(path)
                self.assertNotIn('secret', str(error.exception))

    def test_http_error_does_not_echo_response_or_key(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'proxy.json'
                path.write_text(json.dumps({'schema_version': 1, 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}), encoding='utf-8')
                result = subprocess.run([sys.executable, '-m', 'agy_pool', 'proxy-probe', str(path)], capture_output=True, text=True, env=dict(os.environ, FIXTURE_KEY='wrong-secret'), timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn('HTTP 401', result.stderr)
                self.assertNotIn('wrong-secret', result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_probe_real_local_endpoint_in_subprocess(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'proxy.json'
                path.write_text(json.dumps({'schema_version': 1, 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key_env': 'FIXTURE_KEY', 'timeout_s': 2}), encoding='utf-8')
                env = dict(os.environ, FIXTURE_KEY='fixture-secret')
                result = subprocess.run([sys.executable, '-m', 'agy_pool', 'proxy-probe', str(path)], capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['models'], ['fixture-model'])
                self.assertNotIn('fixture-secret', result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
