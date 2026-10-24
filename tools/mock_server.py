"""本地假模型服务：用于不联网验证 webagent 的模型调用/响应解析链路。

用法::

    python tools/mock_server.py --port 8799 --plan-file plan.json --log requests.jsonl

收到 /v1/chat/completions 后返回 plan-file 里的 JSON 内容作为 assistant 回复，
并把请求摘要追加写入 log 文件（供断言）。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

LOCK = threading.Lock()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--plan-file", default="plan.json")
    ap.add_argument("--log", default="requests.jsonl")
    args = ap.parse_args()

    try:
        plan_text = open(args.plan_file, encoding="utf-8").read().strip()
    except OSError:
        plan_text = json.dumps({
            "done": True, "reasoning": "默认计划", "actions": [],
            "result": {"question": "?", "answer": "A"},
        }, ensure_ascii=False)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            summary = {
                "path": self.path,
                "model": body.get("model"),
                "auth": self.headers.get("Authorization", ""),
                "messages": body.get("messages"),
            }
            with LOCK:
                with open(args.log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary, ensure_ascii=False) + "\n")

            resp = {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": plan_text},
                    "finish_reason": "stop",
                }],
            }
            data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: A003
            pass

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock server on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
