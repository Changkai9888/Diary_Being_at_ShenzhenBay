"""通过真实回环 HTTP 验证子服务边界，不读取正式数据或调用模型。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask, Response, jsonify, request
from waitress import create_server

import app as web
from utils.workspace_gateway import WorkspaceGateway


class WorkspaceGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.calls = []
        self.identity = dict(service_id='workspace-test', identity_protocol=1, service_role='workspace',
                             ready=True, gateway_authenticated=True, capabilities=['gateway-user-v1'])
        child = Flask('workspace-fixture')

        @child.get('/__service__/identity')
        def identity():
            self.assertEqual(request.headers.get('X-SZD-Gateway-Token'), 's' * 48)
            return jsonify(self.identity)

        @child.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
        @child.route('/<path:path>', methods=['GET', 'POST'])
        def child_api(path):
            self.calls.append({'path': path, 'headers': dict(request.headers), 'body': request.get_data(),
                               'query': request.query_string})
            return jsonify(ok=True)

        self.server = create_server(child, host='127.0.0.1', port=0, map={}, asyncore_loop_timeout=0.05)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        config = root / 'services.json'
        config.write_text(json.dumps({'expected_service_id': 'workspace-test',
                                     'origin': f'http://127.0.0.1:{self.server.effective_port}',
                                     'entrypoint': 'missing.py', 'python': None}), encoding='utf-8')
        self.gateway = WorkspaceGateway(config, root / 'runtime')
        self.gateway.runtime.mkdir()
        (self.gateway.runtime / 'cognitive.token').write_text('s' * 48, encoding='utf-8')
        for target in (patch.dict(web.app.config, TESTING=True),
                       patch.object(web, 'workspace_gateway', self.gateway),
                       patch.object(web, '_setup_current_user_paths',
                                    side_effect=lambda: {'one': 'user0', 'two': 'user1'}[web.session['username']]),
                       patch.object(web, '_resolve_user_id_by_username', side_effect=lambda u: {'one': 'user0', 'two': 'user1'}[u])):
            target.start()
            self.addCleanup(target.stop)
        self.client = web.app.test_client()

    def stop_server(self):
        self.server.task_dispatcher.shutdown()
        for channel in list(self.server._map.values()):
            channel.close()
        self.thread.join(timeout=3)

    def login(self, username='one', expired=False):
        with self.client.session_transaction() as session:
            session.update(authenticated=True, username=username,
                           expires_at=time.time() + (-1 if expired else 3600))

    def test_login_and_expiry_gate_all_child_paths_before_forwarding(self):
        self.assertEqual(self.client.get('/cognitive/').status_code, 302)
        self.assertEqual(self.client.get('/cognitive/api/state').status_code, 401)
        self.login(expired=True)
        response = self.client.post('/cognitive/api/chat', json={'content': 'not-forwarded'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json['code'], 'session_expired')
        self.assertEqual(self.calls, [])

    def test_trusted_user_prefix_query_and_credentials_boundary(self):
        self.login()
        response = self.client.post('/cognitive/api/workspaces?x=1', json={'name': 'test'},
            headers={'X-Forwarded-Prefix': '/shenzhen-bay-diary', 'X-SZD-User': 'user99',
                     'X-SZD-Gateway-Token': 'forged', 'X-Workspace-Token': 'csrf',
                     'Authorization': 'must-not-forward', 'Origin': 'http://localhost'},
            environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        self.assertEqual(response.status_code, 200)
        call = self.calls[-1]
        self.assertEqual(call['path'], 'api/workspaces')
        self.assertEqual(call['query'], b'x=1')
        headers = {k.lower(): v for k, v in call['headers'].items()}
        self.assertEqual(headers['x-szd-user'], 'user0')
        self.assertEqual(headers['x-szd-prefix'], '/shenzhen-bay-diary/cognitive')
        self.assertEqual(headers['x-szd-gateway-token'], 's' * 48)
        self.assertEqual(headers['x-workspace-token'], 'csrf')
        for field in ('cookie', 'authorization', 'origin', 'x-forwarded-prefix'):
            self.assertNotIn(field, headers)
        self.login('two')
        self.client.get('/cognitive/api/state')
        self.assertEqual(self.calls[-1]['headers']['X-Szd-User'], 'user1')

    def test_cross_site_and_wrong_deployment_are_not_forwarded_or_started(self):
        self.login()
        self.assertEqual(self.client.post('/cognitive/api/chat', json={},
            headers={'Origin': 'https://other.example'}).status_code, 403)
        self.identity['gateway_authenticated'] = False
        with patch('utils.workspace_gateway.subprocess.Popen') as spawn:
            self.assertEqual(self.client.get('/cognitive/').status_code, 503)
        spawn.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_directory_entry_and_navigation_preserve_each_mount(self):
        self.login()
        for prefix in ('', '/diary', '/shenzhen-bay-diary'):
            headers = {'X-Forwarded-Prefix': prefix} if prefix else {}
            response = self.client.get('/cognitive', headers=headers)
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response.headers['Location'], prefix + '/cognitive/')
            page = self.client.get('/', headers=headers)
            self.assertIn('href="' + prefix + '/cognitive/"', page.text)
            self.assertEqual(page.headers['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
