"""处理器:拿到一行里的一个文件,返回要写回这一行的东西。

两种写法:
  python  {"type": "python", "target": "模块名:函数名"}       函数签名 handle(job) -> Result
  http    {"type": "http", "url": "http://127.0.0.1:8080/x"}  把文件 POST 给你自己的服务

处理器里抛 HandlerError:文案会原样写进这一行的「说明」列给使用者看。
处理器里抛 Busy:这一行放回队列,下一轮再试(适合「后端忙」「被限流」)。
"""
import base64
import importlib
import io
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


class HandlerError(Exception):
    """处理失败,且原因适合直接给表格使用者看。"""


class Busy(Exception):
    """后端暂时忙,稍后自动重试。"""


class Job:
    """一次处理任务:一行里的一个文件。"""

    def __init__(self, name, data, option=None, option_label=None, record_id="", row=None, table=None):
        self.name = name                    # 文件名
        self.data = data                    # 文件内容(bytes)
        self.option = option                # 选项列的值(配置了 options 映射时是映射后的值)
        self.option_label = option_label    # 选项列里显示的文字
        self.record_id = record_id
        self.row = row or {}                # 这一行的全部字段(接口返回的原始格式)
        self.table = table or {}            # {"label", "app_token", "table_id"}


class Result:
    def __init__(self, files=None, fields=None, note=""):
        self.files = list(files or [])      # [(文件名, bytes), ...] 写进「输出」附件列
        self.fields = dict(fields or {})    # {列名: 值} 顺带回填其它列(只填空单元格,不覆盖人写的内容)
        self.note = note or ""              # 写进「说明」列;不给就用默认文案


def normalize(ret, job):
    """处理器的返回值可以是 Result、bytes、(文件名, bytes) 或它们的列表。"""
    if isinstance(ret, Result):
        return ret
    if ret is None:
        return Result()
    if isinstance(ret, (bytes, bytearray)):
        return Result([(_default_name(job.name, ""), bytes(ret))])
    if isinstance(ret, tuple) and len(ret) == 2 and isinstance(ret[0], str):
        return Result([(ret[0], bytes(ret[1]))])
    if isinstance(ret, list):
        out = Result()
        for x in ret:
            r = normalize(x, job)
            out.files += r.files
            out.fields.update(r.fields)
        return out
    raise TypeError("处理器返回了不认识的类型: %s" % type(ret).__name__)


def load(spec):
    kind = spec.get("type")
    if kind == "python":
        mod, _, fn = (spec.get("target") or "").partition(":")
        if not (mod and fn):
            raise ValueError("python 处理器的 target 要写成 模块名:函数名")
        func = getattr(importlib.import_module(mod), fn)
        return lambda job: normalize(func(job), job)
    if kind == "http":
        if not spec.get("url"):
            raise ValueError("http 处理器缺少 url")
        return lambda job: http_call(spec, job)
    raise ValueError("处理器的 type 只能是 python 或 http")


# ---------------- http 处理器 ----------------
def _default_name(src, ctype):
    stem = src.rsplit(".", 1)[0] if "." in src else src
    ext = mimetypes.guess_extension((ctype or "").split(";")[0].strip()) if ctype else None
    return "%s-output%s" % (stem or "file", ext or (".bin" if ctype else ""))


def _disposition_name(v):
    """从 Content-Disposition 里取文件名,兼容 filename*=UTF-8''... 和 filename="..."。"""
    if not v:
        return ""
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", v)
    if m:
        return urllib.parse.unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]*)"', v) or re.search(r"filename\s*=\s*([^;]+)", v)
    if not m:
        return ""
    name = m.group(1).strip()
    try:                                    # 有的服务直接把 UTF-8 文件名塞进头里
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _expand(v):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), v) if isinstance(v, str) else v


def _multipart(fields, file_field, fname, data):
    boundary = "----btw" + uuid.uuid4().hex
    safe = re.sub(r'["\r\n]', "_", fname)
    body = b""
    for k, v in fields.items():
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode("utf-8")
    body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
             % (boundary, file_field, safe, mimetypes.guess_type(safe)[0] or "application/octet-stream")).encode("utf-8")
    body += data + ("\r\n--%s--\r\n" % boundary).encode()
    return body, "multipart/form-data; boundary=" + boundary


def _error_text(raw):
    try:
        d = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return ""
    if isinstance(d, dict):
        for k in ("error", "message", "detail", "msg"):
            if isinstance(d.get(k), str) and d[k]:
                return d[k]
    return ""


def http_call(spec, job):
    """把文件 POST 给 spec["url"]。约定见 docs/handler-http.md。"""
    form = {spec.get("option_field") or "option": job.option or "",
            "record_id": job.record_id, "table": job.table.get("label") or ""}
    form.update({k: _expand(v) for k, v in (spec.get("form") or {}).items()})
    body, ctype = _multipart(form, spec.get("file_field") or "file", job.name, job.data)
    headers = {"Content-Type": ctype, "User-Agent": "bitable-worker"}
    headers.update({k: _expand(v) for k, v in (spec.get("headers") or {}).items()})
    req = urllib.request.Request(spec["url"], data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=int(spec.get("timeout") or 300)) as r:
            raw, rtype, disp = r.read(), r.headers.get("Content-Type") or "", r.headers.get("Content-Disposition") or ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.code in (429, 502, 503, 504):
            raise Busy("后端忙 HTTP %s" % e.code)
        raise HandlerError(_error_text(raw) or "处理失败(HTTP %s)" % e.code)
    except Exception as e:
        raise Busy("后端连不上: %s" % type(e).__name__)
    if rtype.split(";")[0].strip().lower() == "application/json":
        try:
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HandlerError("处理失败(返回内容异常)")
        if not isinstance(d, dict):
            raise HandlerError("处理失败(返回内容异常)")
        if d.get("error"):
            raise HandlerError(str(d["error"]))
        files = []
        for f in d.get("files") or []:
            try:
                files.append((str(f["name"]), base64.b64decode(f["content_base64"])))
            except Exception:
                raise HandlerError("处理失败(返回的文件格式不对)")
        return Result(files, d.get("fields") if isinstance(d.get("fields"), dict) else None, str(d.get("note") or ""))
    if spec.get("unzip") and raw[:2] == b"PK":      # 后端一次返回多个文件时,可以打成 zip
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise HandlerError("处理失败(返回内容异常)")
        files = [(n.rsplit("/", 1)[-1], z.read(n)) for n in z.namelist() if not n.endswith("/")]
        if not files:
            raise HandlerError("处理失败(没有返回文件)")
        return Result(files)
    if not raw:
        raise HandlerError("处理失败(没有返回文件)")
    return Result([(_disposition_name(disp) or _default_name(job.name, rtype), raw)])
