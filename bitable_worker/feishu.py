"""飞书开放平台最小客户端:只用标准库,只包了本项目用到的接口。"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

MAX_FILE_BYTES = 20 * 1024 * 1024
RATE_LIMIT_CODES = (99991400, 1254290, 1254291)      # 频控 / 写冲突:稍后重试即可


class FeishuError(RuntimeError):
    """飞书接口报错。transient=True 表示网络、网关或频控这类临时故障,稍后重试即可。"""

    def __init__(self, msg, code=None, transient=False):
        super().__init__(msg)
        self.code, self.transient = code, transient


def load_env_file(path):
    """读 KEY=VALUE 格式的文件,写进环境变量(已有的不覆盖)。"""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def text(v):
    """把单元格的值变成字符串。文本列读出来是 [{"text": ..}, ..],写进去是字符串。"""
    if isinstance(v, list):
        return "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in v)
    return "" if v is None else str(v)


def cond(name, op, val=None):
    return {"field_name": name, "operator": op, "value": [val] if val else []}


class Feishu:
    def __init__(self, app_id=None, app_secret=None, domain=None):
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        if not (self.app_id and self.app_secret):
            raise FeishuError("缺少飞书应用凭证:请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self.base = (domain or os.environ.get("FEISHU_DOMAIN") or "https://open.feishu.cn").rstrip("/") + "/open-apis"
        self._tok, self._exp = "", 0.0

    # ---- 底层 ----
    def token(self):
        if self._tok and time.time() < self._exp:
            return self._tok
        d = self._raw("POST", "/auth/v3/tenant_access_token/internal",
                      json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode(),
                      {"Content-Type": "application/json"}, auth=False)
        if not d.get("tenant_access_token"):
            raise FeishuError("取 tenant_access_token 失败: %s" % str(d.get("msg"))[:200], d.get("code"))
        self._tok, self._exp = d["tenant_access_token"], time.time() + int(d.get("expire") or 7200) - 120
        return self._tok

    def _raw(self, method, path, data=None, headers=None, auth=True, timeout=30):
        hdr = dict(headers or {})
        if auth:
            hdr["Authorization"] = "Bearer " + self.token()
        req = urllib.request.Request(self.base + path, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return json.loads(raw)
            except Exception:
                raise FeishuError("HTTP %s(飞书网关)" % e.code, transient=e.code >= 500 or e.code == 429)
        except Exception as e:
            raise FeishuError("网络错误: %s: %s" % (type(e).__name__, str(e)[:200]), transient=True)

    def call(self, method, path, body=None, timeout=30):
        """调一个接口,返回响应里的 data。出错抛 FeishuError。"""
        data = hdr = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdr = {"Content-Type": "application/json; charset=utf-8"}
        d = self._raw(method, path, data, hdr, timeout=timeout)
        if d.get("code") not in (0, None):
            raise FeishuError("code=%s: %s" % (d.get("code"), str(d.get("msg"))[:300]),
                              d.get("code"), transient=d.get("code") in RATE_LIMIT_CODES)
        return d.get("data") if "data" in d else d

    def download(self, url, limit=MAX_FILE_BYTES):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token()})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read(limit + 1)
        except urllib.error.HTTPError as e:
            raise FeishuError("下载附件失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))
        except Exception as e:
            raise FeishuError("下载附件失败: %s" % type(e).__name__, transient=True)
        if len(data) > limit:
            raise FeishuError("附件超过 %dMB" % (limit // 1024 // 1024))
        return data

    def media_url(self, file_token):
        return self.base + "/drive/v1/medias/%s/download" % file_token

    def upload(self, app_token, fname, data):
        """把文件传进某个多维表格的素材空间,返回 file_token(写进附件列用)。"""
        boundary = "----btw" + uuid.uuid4().hex
        fname = fname.replace('"', "_").replace("\r", "_").replace("\n", "_")
        parts = []
        for k, v in (("file_name", fname), ("parent_type", "bitable_file"),
                     ("parent_node", app_token), ("size", str(len(data)))):
            parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode())
        parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n' % (boundary, fname)).encode() + data + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        d = self._raw("POST", "/drive/v1/medias/upload_all", b"".join(parts),
                      {"Content-Type": "multipart/form-data; boundary=" + boundary}, timeout=120)
        if d.get("code") not in (0, None) or not (d.get("data") or {}).get("file_token"):
            raise FeishuError("上传文件失败: code=%s %s" % (d.get("code"), str(d.get("msg"))[:200]), d.get("code"))
        return d["data"]["file_token"]

    # ---- 多维表格常用操作(都建立在 call 之上) ----
    def pages(self, path, key="items", cap=5000):
        """GET 列表接口,自动翻页。"""
        out, page = [], ""
        while True:
            sep = "&" if "?" in path else "?"
            d = self.call("GET", path + sep + "page_size=100" + ("&page_token=" + page if page else ""))
            out += d.get(key) or []
            page = d.get("page_token") or ""
            if not d.get("has_more") or not page or len(out) >= cap:
                return out

    def tables(self, app):
        return self.pages("/bitable/v1/apps/%s/tables" % app)

    def fields(self, app, tid):
        return self.pages("/bitable/v1/apps/%s/tables/%s/fields" % (app, tid))

    def search(self, app, tid, flt=None, cap=500, automatic=False):
        """查记录。automatic=True 时每条记录带 created_time / last_modified_time。"""
        out, page = [], ""
        body = {}
        if flt:
            body["filter"] = flt
        if automatic:
            body["automatic_fields"] = True
        while True:
            q = "/bitable/v1/apps/%s/tables/%s/records/search?page_size=%d%s" % (
                app, tid, min(500, max(1, cap)), "&page_token=" + page if page else "")
            d = self.call("POST", q, body)
            out += d.get("items") or []
            page = d.get("page_token") or ""
            if not d.get("has_more") or not page or len(out) >= cap:
                return out

    def put(self, app, tid, rid, fields):
        """改一行。偶发失败(网关 502 等)会再试两次,免得行卡在「处理中」。"""
        for i in range(3):
            try:
                self.call("PUT", "/bitable/v1/apps/%s/tables/%s/records/%s" % (app, tid, rid), {"fields": fields})
                return
            except FeishuError:
                if i == 2:
                    raise
                time.sleep(2 + 3 * i)

    def batch(self, app, tid, op, records, size=200):
        """op: create / update / delete。records 为记录列表(delete 时是 record_id 列表)。"""
        p = "/bitable/v1/apps/%s/tables/%s/records/batch_%s" % (app, tid, op)
        for i in range(0, len(records), size):
            self.call("POST", p, {"records": records[i:i + size]})
