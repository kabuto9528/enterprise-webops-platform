#!/usr/bin/python3
"""Single-repository, loopback-only smart Git fetch adapter; no push or files."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import subprocess
from urllib.parse import urlsplit

ADDRESS = ('127.0.0.1', 19418)
PROJECT_ROOT = '/srv/git'
MAX_BODY = 4 * 1024 * 1024


def allowed(method, target):
    url = urlsplit(target)
    if url.scheme or url.netloc:
        return False
    return ((method == 'GET' and url.path == '/webops.git/info/refs'
             and url.query == 'service=git-upload-pack')
            or (method == 'POST' and url.path == '/webops.git/git-upload-pack'
                and not url.query))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_git(self):
        if self.client_address[0] != '127.0.0.1' or not allowed(self.command, self.path):
            self.send_error(403)
            return
        self.connection.settimeout(15)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
            self.send_error(413)
            return
        if self.command == 'POST' and self.headers.get('Content-Type') != 'application/x-git-upload-pack-request':
            self.send_error(415)
            return
        url = urlsplit(self.path)
        environment = {
            'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8',
            'GIT_PROJECT_ROOT': PROJECT_ROOT, 'GIT_HTTP_EXPORT_ALL': '1',
            'REQUEST_METHOD': self.command, 'PATH_INFO': url.path,
            'QUERY_STRING': url.query, 'REMOTE_ADDR': '127.0.0.1',
            'CONTENT_TYPE': self.headers.get('Content-Type', ''),
            'CONTENT_LENGTH': str(length),
        }
        protocol = self.headers.get('Git-Protocol', '')
        if protocol in {'version=1', 'version=2'}:
            environment['GIT_PROTOCOL'] = protocol
        args = ['/usr/bin/git', '-c', 'safe.directory=' + PROJECT_ROOT + '/webops.git',
                '-c', 'http.receivepack=false', '-c', 'http.getanyfile=false',
                '-c', 'http.uploadarchive=false', 'http-backend']
        try:
            result = subprocess.run(args, input=self.rfile.read(length), env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
            headers, body = result.stdout.split(b'\r\n\r\n', 1)
            fields = [line.decode('ascii').split(':', 1) for line in headers.split(b'\r\n')]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.send_error(502)
            return
        status = next((int(value.strip().split()[0]) for key, value in fields if key.lower() == 'status'), 200)
        self.send_response(status)
        for key, value in fields:
            if key.lower() in {'content-type', 'cache-control', 'expires', 'pragma'}:
                self.send_header(key, value.strip())
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = handle_git
    do_POST = handle_git


if __name__ == '__main__':
    HTTPServer(ADDRESS, Handler).serve_forever()
