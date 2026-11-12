"""示例后端(http 类型的处理器接它):和 handler_docx_text 做同一件事,只是换成 HTTP 服务。

运行:  python3 examples/http_handler_server.py          (监听 127.0.0.1:8080)
配置:  "handlers": {"default": {"type": "http", "url": "http://127.0.0.1:8080/convert"}}

真实场景里,这里换成你已有的服务(任何语言都行),只要遵守 docs/handlers.md 里的约定。
"""
import json
import os
import sys
import urllib.parse
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitable_worker.handlers import HandlerError, Job      # noqa: E402
from examples.handler_docx_text import handle              # noqa: E402


def parse_multipart(ctype, body):
    """返回 ({表单字段: 值}, (文件名, 文件内容))。"""
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    form, upload = {}, None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fname = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if fname is not None:
            try:      # 文件名是直接按 UTF-8 写在头里的
                fname = fname.encode("utf-8", "surrogateescape").decode("utf-8")
            except UnicodeError:
                pass
            upload = (fname, data)
        else:
            form[name] = data.decode("utf-8", "ignore")
    return form, upload


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        form, upload = parse_multipart(self.headers.get("Content-Type") or "", body)
        if not upload:
            return self._send(400, "application/json", json.dumps({"error": "没有收到文件"}).encode())
        try:
            res = handle(Job(upload[0], upload[1], option=form.get("option") or None))
        except HandlerError as e:      # 4xx + {"error": ...}:文案会原样显示在表格的「说明」列
            return self._send(400, "application/json", json.dumps({"error": str(e)}).encode())
        name, data = res.files[0]
        self._send(200, "text/plain; charset=utf-8", data,
                   {"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(name)})

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8080), H).serve_forever()
