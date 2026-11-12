import io
import json
import urllib.request
import uuid


def get_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "newsbot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_multipart(url: str, fields: dict, files: dict[str, tuple[str, bytes, str]],
                   timeout: float = 30) -> dict:
    """POST multipart/form-data (p. ej. sendPhoto de Telegram). files: {campo: (nombre, bytes, tipo)}."""
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data, ctype) in files.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(url, data=body.getvalue(), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "newsbot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_json(url: str, payload: dict, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "newsbot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)
