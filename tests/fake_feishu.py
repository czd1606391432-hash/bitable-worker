"""内存里的假飞书:只实现本项目用到的接口,给测试用。行为按真实接口的已知表现来写:

- 复制多维表格:字段 ID 不变、数据表 ID 会变、自动化全部变成停用
- 查询支持 filter(and/or、children、is/isEmpty/isNotEmpty)和翻页
"""
import copy
import itertools
import re
import urllib.parse

from bitable_worker.feishu import FeishuError, text


class FakeFeishu:
    base = "https://fake/open-apis"

    def __init__(self):
        self.apps, self.files, self.calls = {}, {}, []
        self._n = itertools.count(1)
        self.fail_next = []          # [(方法, 路径正则, FeishuError)]:让下一次匹配的调用失败

    # ---- 测试里用来搭数据的工具 ----
    def _id(self, prefix):
        return "%s%04d" % (prefix, next(self._n))

    def add_app(self, name, owner="ou_owner"):
        tok = self._id("app")
        self.apps[tok] = {"name": name, "owner": owner, "tables": {}, "workflows": [], "public": {}, "members": [],
                          "url": "https://x.feishu.cn/base/" + tok}
        return tok

    def add_table(self, app, name, fields):
        """fields: [(列名, 类型)];返回 (table_id, {列名: field_id})"""
        tid = self._id("tbl")
        fl = [{"field_id": self._id("fld"), "field_name": n, "type": ty} for n, ty in fields]
        self.apps[app]["tables"][tid] = {"name": name, "fields": fl, "records": {}, "views": [
            {"view_id": self._id("vew"), "view_name": "表格", "view_type": "grid"}]}
        return tid, {x["field_name"]: x["field_id"] for x in fl}

    def add_row(self, app, tid, fields, created=None):
        rid = self._id("rec")
        self.apps[app]["tables"][tid]["records"][rid] = {"fields": dict(fields), "created_time": created or 1700000000000}
        return rid

    def add_file(self, name, data):
        tok = self._id("file")
        self.files[tok] = (name, data)
        return {"file_token": tok, "name": name, "size": len(data), "url": self.media_url(tok)}

    def row(self, app, tid, rid):
        return self.apps[app]["tables"][tid]["records"][rid]["fields"]

    def rows(self, app, tid):
        return {k: v["fields"] for k, v in self.apps[app]["tables"][tid]["records"].items()}

    # ---- 与 Feishu 相同的对外接口 ----
    def token(self):
        return "t-fake"

    def media_url(self, file_token):
        return self.base + "/drive/v1/medias/%s/download" % file_token

    def download(self, url, limit=20 * 1024 * 1024):
        tok = url.rsplit("/medias/", 1)[1].split("/")[0]
        if tok not in self.files:
            raise FeishuError("下载附件失败 HTTP 404")
        return self.files[tok][1]

    def upload(self, app_token, fname, data):
        tok = self._id("file")
        self.files[tok] = (fname, data)
        return tok

    def call(self, method, path, body=None, timeout=30):
        self.calls.append((method, path))
        for i, (m, pat, err) in enumerate(self.fail_next):
            if m == method and re.search(pat, path):
                self.fail_next.pop(i)
                raise err
        u = urllib.parse.urlparse(path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        for m, pat, fn in self._routes():
            mo = re.fullmatch(pat, u.path)
            if m == method and mo:
                return fn(*mo.groups(), body=body or {}, q=q)
        raise FeishuError("假飞书没有实现: %s %s" % (method, u.path), code=404)

    # 下面这几个方法直接复用真实客户端的实现
    from bitable_worker.feishu import Feishu as _F
    pages, tables, fields, search, batch = _F.pages, _F.tables, _F.fields, _F.search, _F.batch

    def put(self, app, tid, rid, fields):      # 测试里不重试、不 sleep
        self.call("PUT", "/bitable/v1/apps/%s/tables/%s/records/%s" % (app, tid, rid), {"fields": fields})

    # ---- 路由 ----
    def _routes(self):
        A, T = r"/bitable/v1/apps/(\w+)", r"/tables/(\w+)"
        return [
            ("POST", r"/bitable/v1/apps", self._app_create),
            ("POST", A + "/copy", self._app_copy),
            ("GET", A + "/tables", self._tables),
            ("POST", A + "/tables", self._table_create),
            ("DELETE", A + T, self._table_delete),
            ("GET", A + T + "/fields", self._fields),
            ("POST", A + T + "/fields", self._field_create),
            ("POST", A + T + "/records/search", self._search),
            ("PUT", A + T + r"/records/(\w+)", self._record_put),
            ("POST", A + T + "/records/batch_create", self._batch_create),
            ("POST", A + T + "/records/batch_update", self._batch_update),
            ("POST", A + T + "/records/batch_delete", self._batch_delete),
            ("GET", A + T + "/views", self._views),
            ("PATCH", A + T + r"/views/(\w+)", self._view_patch),
            ("GET", A + "/workflows", self._flows),
            ("PUT", A + r"/workflows/(\w+)", self._flow_put),
            ("POST", r"/drive/v1/metas/batch_query", self._metas),
            ("POST", r"/drive/v1/permissions/(\w+)/members/transfer_owner", self._transfer),
            ("POST", r"/drive/v1/permissions/(\w+)/members", self._member_add),
            ("PATCH", r"/drive/v1/permissions/(\w+)/public", self._public),
        ]

    def _app(self, tok):
        if tok not in self.apps:
            raise FeishuError("code=91402: NOTEXIST", code=91402)
        return self.apps[tok]

    def _tab(self, tok, tid):
        a = self._app(tok)
        if tid not in a["tables"]:
            raise FeishuError("code=1254004: WrongTableId", code=1254004)
        return a["tables"][tid]

    @staticmethod
    def _page(items, q, key="items"):
        size, start = int(q.get("page_size") or 20), int(q.get("page_token") or 0)
        part, more = items[start:start + size], start + size < len(items)
        return {key: part, "has_more": more, "page_token": str(start + size) if more else "", "total": len(items)}

    def _app_create(self, body, q):
        tok = self.add_app(body.get("name") or "未命名", owner="app")
        tid, _ = self.add_table(tok, "数据表", [("文本", 1)])
        self.add_row(tok, tid, {})
        return {"app": {"app_token": tok, "url": self.apps[tok]["url"], "default_table_id": tid}}

    def _app_copy(self, src, body, q):
        a = self._app(src)
        tok = self.add_app(body.get("name") or a["name"], owner="app")
        new = self.apps[tok]
        for tid, t in a["tables"].items():
            nt = copy.deepcopy(t)          # 字段 ID 原样保留
            if body.get("without_content"):
                nt["records"] = {}
            new["tables"][self._id("tbl")] = nt
        if not body.get("without_content"):      # 只复制结构时,自动化不会带过去
            new["workflows"] = [dict(w, workflow_id=self._id("wf"), status="Disable") for w in a["workflows"]]
        return {"app": {"app_token": tok, "url": new["url"]}}

    def _tables(self, tok, body, q):
        return self._page([{"table_id": k, "name": v["name"]} for k, v in self._app(tok)["tables"].items()], q)

    def _table_create(self, tok, body, q):
        t = body["table"]
        fl = t.get("fields") or []
        if fl and fl[0]["type"] in (11, 17, 1003):
            raise FeishuError("code=1254001: Unsupported field type", code=1254001)
        for x in fl:
            fmt = (x.get("property") or {}).get("formatter")
            if x["type"] == 2 and fmt and "%" in fmt and fmt != "0.00%" and x.get("ui_type") != "Progress":
                raise FeishuError("code=1254001: WrongRequestBody", code=1254001)
        tid, _ = self.add_table(tok, t["name"], [(x["field_name"], x["type"]) for x in fl])
        self.add_row(tok, tid, {})
        return {"table_id": tid}

    def _table_delete(self, tok, tid, body, q):
        self._tab(tok, tid)
        del self.apps[tok]["tables"][tid]
        return {}

    def _fields(self, tok, tid, body, q):
        return self._page(self._tab(tok, tid)["fields"], q)

    def _field_create(self, tok, tid, body, q):
        f = {"field_id": self._id("fld"), "field_name": body["field_name"], "type": body["type"]}
        self._tab(tok, tid)["fields"].append(f)
        return {"field": f}

    def _match(self, f, flt):
        if not flt:
            return True
        res = []
        for c in flt.get("conditions") or []:
            v = f.get(c["field_name"])
            empty = v in (None, "", [], {})
            op = c["operator"]
            res.append(empty if op == "isEmpty" else (not empty) if op == "isNotEmpty" else
                       (text(v) == (c.get("value") or [""])[0]) if op == "is" else False)
        res += [self._match(f, ch) for ch in flt.get("children") or []]
        return all(res) if flt.get("conjunction", "and") == "and" else any(res)

    def _search(self, tok, tid, body, q):
        t = self._tab(tok, tid)
        names = {x["field_name"] for x in t["fields"]}
        for c in self._conds(body.get("filter")):
            if c["field_name"] not in names:
                raise FeishuError("code=1254045: FieldNameNotFound", code=1254045)
        items = []
        for rid, r in t["records"].items():
            if self._match(r["fields"], body.get("filter")):
                it = {"record_id": rid, "fields": {k: v for k, v in r["fields"].items() if v not in (None, "", [])}}
                if body.get("automatic_fields"):
                    it["created_time"] = r["created_time"]
                    it["last_modified_time"] = r.get("last_modified_time") or r["created_time"]
                items.append(it)
        return self._page(items, q)

    def _conds(self, flt):
        if not flt:
            return []
        return list(flt.get("conditions") or []) + [c for ch in flt.get("children") or [] for c in self._conds(ch)]

    def _write(self, tok, tid, rid, fields):
        t = self._tab(tok, tid)
        names = {x["field_name"]: x for x in t["fields"]}
        for k in fields:
            if k not in names:
                raise FeishuError("code=1254045: FieldNameNotFound", code=1254045)
        rec = t["records"].setdefault(rid, {"fields": {}, "created_time": 1700000000000})
        for k, v in fields.items():
            if names[k]["type"] == 17 and isinstance(v, list):      # 附件列:补上文件名和大小
                v = [dict(x, name=self.files[x["file_token"]][0], size=len(self.files[x["file_token"]][1]),
                          url=self.media_url(x["file_token"])) for x in v]
            rec["fields"][k] = v

    def _record_put(self, tok, tid, rid, body, q):
        if rid not in self._tab(tok, tid)["records"]:
            raise FeishuError("code=1254043: RecordIdNotFound", code=1254043)
        self._write(tok, tid, rid, body["fields"])
        return {}

    def _batch_create(self, tok, tid, body, q):
        for r in body["records"]:
            self._write(tok, tid, self._id("rec"), r["fields"])
        return {}

    def _batch_update(self, tok, tid, body, q):
        for r in body["records"]:
            self._write(tok, tid, r["record_id"], r["fields"])
        return {}

    def _batch_delete(self, tok, tid, body, q):
        t = self._tab(tok, tid)
        for rid in body["records"]:
            t["records"].pop(rid, None)
        return {}

    def _views(self, tok, tid, body, q):
        return self._page(self._tab(tok, tid)["views"], q)

    def _view_patch(self, tok, tid, vid, body, q):
        next(v for v in self._tab(tok, tid)["views"] if v["view_id"] == vid).update(body)
        return {}

    def _flows(self, tok, body, q):
        return {"workflows": self._app(tok)["workflows"]}

    def _flow_put(self, tok, wid, body, q):
        next(w for w in self._app(tok)["workflows"] if w["workflow_id"] == wid)["status"] = body["status"]
        return {}

    def _metas(self, body, q):
        return {"metas": [{"doc_token": d["doc_token"], "owner_id": self._app(d["doc_token"])["owner"]}
                          for d in body["request_docs"]]}

    def _transfer(self, tok, body, q):
        a = self._app(tok)
        a["members"].append({"id": a["owner"], "perm": q.get("old_owner_perm")})
        a["owner"] = body["member_id"]
        return {}

    def _member_add(self, tok, body, q):
        self._app(tok)["members"].append({"id": body["member_id"], "perm": body.get("perm")})
        return {}

    def _public(self, tok, body, q):
        self._app(tok)["public"] = body
        return {}
