"""An isolated Git repository and ephemeral loopback server exercise the adapter."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

spec = importlib.util.spec_from_file_location('git_readonly', Path(__file__).parents[1] / 'scripts/webops-git-readonly.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.mark.parametrize('method,path', [
    ('POST', '/webops.git/git-receive-pack'),
    ('GET', '/webops.git/info/refs?service=git-receive-pack'),
    ('GET', '/other.git/info/refs?service=git-upload-pack'),
    ('GET', '/webops.git/config'), ('GET', '/webops.git/../config'),
    ('GET', '/webops.git/info/refs?service=git-upload-pack&other=x'),
])
def test_rejects_push_files_and_other_repositories(method, path):
    assert not adapter.allowed(method, path)


@pytest.mark.skipif(sys.platform == 'win32', reason='installed Linux git http-backend')
def test_real_fetch_works_and_push_is_denied(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], text=True, stderr=subprocess.STDOUT).strip()
    source = tmp_path / 'source'
    git('init', '-b', 'main', str(source))
    (source / 'readme').write_text('trusted fixture')
    git('-C', str(source), 'add', 'readme')
    git('-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
    expected = git('-C', str(source), 'rev-parse', 'HEAD')
    git('clone', '--bare', str(source), str(tmp_path / 'webops.git'))
    adapter.PROJECT_ROOT = str(tmp_path)
    server = adapter.HTTPServer(('127.0.0.1', 0), adapter.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:' + str(server.server_port) + '/webops.git'
    try:
        cloned = tmp_path / 'cloned'
        git('clone', url, str(cloned))
        assert git('-C', str(cloned), 'rev-parse', 'HEAD') == expected
        assert git('ls-remote', url, 'refs/heads/main').split()[0] == expected
        for path in ('/config', '/info/refs?service=git-receive-pack', '/git-receive-pack'):
            request = urllib.request.Request(url + path, method='POST' if path == '/git-receive-pack' else 'GET')
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            assert error.value.code == 403
        assert git('-C', str(tmp_path / 'webops.git'), 'rev-parse', 'HEAD') == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
