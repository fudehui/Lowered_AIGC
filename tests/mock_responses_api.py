from http.server import BaseHTTPRequestHandler, HTTPServer
import json

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(n).decode("utf-8"))
        raw_input = payload.get("input", "{}")
        try:
            data = json.loads(raw_input)
            text = data.get("text", raw_input)
        except Exception:
            text = raw_input
        rewritten = text.replace("本文", "本研究").replace("通过", "借助").replace("提出", "构建")
        if rewritten == text:
            rewritten = "围绕研究目标，" + text
        body = json.dumps({"output_text": rewritten}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
