import json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        print("mock", self.command, self.path, flush=True)

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "mock-model", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        text = json.dumps(req.get("messages", []))
        slow = "SLOW" in text
        print("mock request slow=%s" % slow, flush=True)
        if slow:
            time.sleep(20)
        if "FAILME" in text:
            body = json.dumps({"error": {"message": "mock provider rejected request", "type": "invalid_request_error", "code": "mock_fail"}}).encode()
            self.send_response(400); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        def chunk(delta, finish=None):
            c = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-model",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode()); self.wfile.flush()
        chunk({"role": "assistant", "content": "CUSTOMER_TASK_OK_42"})
        chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
