import os
import json
import time
import requests
import configparser
from flask import Flask, request, Response, stream_with_context

app = Flask(__name__)

# ============ 配置 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")

config = configparser.ConfigParser()
config.read(CONFIG_FILE, encoding="utf-8")

UPSTREAM_BASE_URL = os.environ.get(
    "UPSTREAM_BASE_URL",
    config.get("upstream", "base_url", fallback="https://api.deepseek.com/beta")
)
UPSTREAM_TIMEOUT = int(os.environ.get(
    "UPSTREAM_TIMEOUT",
    config.get("server", "timeout", fallback="60")
))
PORT = int(os.environ.get(
    "PORT",
    config.get("server", "port", fallback="4396")
))
PREFIX_STRING = os.environ.get("PREFIX_STRING", "")

#下面这行是转发开关，设为false则不转发
ENABLE_FORWARD = os.environ.get("ENABLE_FORWARD", "true").lower() == "true"

MESSAGE_FILE = os.path.join(BASE_DIR, "message.json")
RESPONSE_FILE = os.path.join(BASE_DIR, "response.json")

EXCLUDED_REQ_HEADERS = {
    "host", "connection", "content-length", "transfer-encoding",
}
EXCLUDED_RES_HEADERS = {
    "host", "connection", "content-length", "transfer-encoding",
    "content-encoding",
}


# ============ 消息捕获 ============
def parse_request_body():
    """尝试解析请求体为 JSON，失败则返回原始文本"""
    raw = request.get_data(as_text=True)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def save_message(body_data):
    """将接收到的消息写入 message.json"""
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "path": request.path,
        "method": request.method,
        "headers": dict(request.headers),
        "query_params": dict(request.args),
        "body": body_data,
    }
    with open(MESSAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    # 控制台输出摘要
    msg_count = 0
    last_role = ""
    if isinstance(body_data, dict) and "messages" in body_data:
        msgs = body_data["messages"]
        msg_count = len(msgs)
        if msgs:
            last_role = msgs[-1].get("role", "")

    model = ""
    if isinstance(body_data, dict):
        model = body_data.get("model", "")
    stream = ""
    if isinstance(body_data, dict):
        stream = " (stream)" if body_data.get("stream") else ""

    print(f"[ST-Relay] {record['timestamp']} | {request.method} {request.path}{stream}")
    if model:
        print(f"           model={model} | messages={msg_count} | last_role={last_role}")


def save_response(status_code, headers, body_data):
    """将上游响应写入 response.json"""
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status_code": status_code,
        "headers": headers,
        "body": body_data,
    }
    with open(RESPONSE_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"[ST-Relay] Response saved to {RESPONSE_FILE}")


# ============ 路由 ============
@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], defaults={"path": ""})
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy(path):
    url = f"{UPSTREAM_BASE_URL}/{path}" if path else UPSTREAM_BASE_URL

    # 提取请求头（去除 hop-by-hop 头）
    req_headers = {}
    for key, value in request.headers:
        if key.lower() not in EXCLUDED_REQ_HEADERS:
            req_headers[key] = value

    params = request.args

    # 解析请求体
    body_data = parse_request_body()

    # 写入 message.json
    save_message(body_data)

    # 处理 JSON 请求体 —— 注入/修改
    body = request.get_data()
    is_stream = False
    if isinstance(body_data, dict):
        data = body_data
        modified = False

        if not data.get("model"):
            data["model"] = "deepseek-v4-flash"
            modified = True

        data["thinking"] = {"type": "disabled"}
        modified = True

        custom_prefix = data.pop("my_prefix", None)
        prefix_content = PREFIX_STRING
        if custom_prefix is not None:
            prefix_content = str(custom_prefix)

        if prefix_content and "messages" in data and isinstance(data["messages"], list):
            data["messages"].append({
                "role": "assistant",
                "content": prefix_content,
                "prefix": True
            })
            modified = True

        is_stream = data.get("stream", False)

        if modified:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    # ===== 转发到上游 =====
    if not ENABLE_FORWARD:
        print(f"[ST-Relay] Forward disabled — message saved, skipping upstream")
        return Response(
            json.dumps({"status": "received", "message_count": len(body_data.get("messages", [])) if isinstance(body_data, dict) else 0}, ensure_ascii=False),
            status=200,
            content_type="application/json",
        )

    try:
        upstream_resp = requests.request(
            method=request.method,
            url=url,
            headers=req_headers,
            params=params,
            data=body,
            stream=True,
            timeout=UPSTREAM_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        print(f"[ST-Relay] Upstream error: {e}")
        return Response(f"Upstream request failed: {str(e)}", status=502)

    # 过滤响应头
    res_headers = {}
    for key, value in upstream_resp.headers.items():
        if key.lower() not in EXCLUDED_RES_HEADERS:
            res_headers[key] = value

    # 流式响应
    if is_stream or "text/event-stream" in upstream_resp.headers.get("Content-Type", ""):
        def generate():
            chunks = []
            for chunk in upstream_resp.iter_content(chunk_size=None):
                if chunk:
                    chunks.append(chunk)
                    yield chunk
            full_body = b"".join(chunks)
            try:
                resp_data = json.loads(full_body)
            except (json.JSONDecodeError, ValueError):
                resp_data = full_body.decode("utf-8", errors="replace")
            save_response(upstream_resp.status_code, dict(res_headers), resp_data)

        return Response(
            stream_with_context(generate()),
            status=upstream_resp.status_code,
            headers=res_headers,
            content_type=upstream_resp.headers.get("Content-Type", "text/event-stream"),
        )

    # 普通响应 —— 先写入 response.json 再返回
    resp_body = upstream_resp.content
    try:
        resp_data = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        resp_data = resp_body.decode("utf-8", errors="replace")

    save_response(upstream_resp.status_code, dict(res_headers), resp_data)

    return Response(
        resp_body,
        status=upstream_resp.status_code,
        headers=res_headers,
        content_type=upstream_resp.headers.get("Content-Type"),
    )


if __name__ == "__main__":
    print(f"ST-Relay running on http://0.0.0.0:{PORT}")
    print(f"Upstream: {UPSTREAM_BASE_URL}")
    print(f"Config file: {CONFIG_FILE}")
    print(f"Message file: {MESSAGE_FILE}")
    print(f"Response file: {RESPONSE_FILE}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
