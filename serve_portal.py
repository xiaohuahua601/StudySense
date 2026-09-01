"""门户静态服务器: 访问 / 时自动定位到登录页(portal.html), 无需输入 .html"""
import functools
import http.server
import os
import socketserver

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class PortalHandler(http.server.SimpleHTTPRequestHandler):
    # /、/login、/portal 均路由到登录页, 其余按静态文件原样服务(如 /logo.svg)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def send_head(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/login", "/portal"):
            self.path = "/portal.html"
        return super().send_head()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("0.0.0.0", 8501), PortalHandler) as httpd:
        print("StudySense portal serving on http://0.0.0.0:8501/")
        httpd.serve_forever()
