#!/usr/bin/env python3
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import time
import urllib.parse

topics = {}

class NtfyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.endswith('/json'):
            topic = parsed.path[:-5].lstrip('/')
            if topic in topics and topics[topic]:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "event": "message",
                    "topic": topic,
                    "message": topics[topic],
                    "time": int(time.time())
                }).encode())
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        topic = parsed.path.lstrip('/')
        if not topic:
            self.send_response(400)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        topics[topic] = body
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, format, *args):
        pass

def run(port=8080):
    server = HTTPServer(('', port), NtfyHandler)
    print(f'✅ Fake ntfy running on http://localhost:{port}')
    print('Press Ctrl+C to stop')
    server.serve_forever()

if __name__ == '__main__':
    run()