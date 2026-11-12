"""总账:把每张已分发表格里的一张「汇总表」读出来,合并到登记表所在多维表格的「总账」里,一行对应来源的一行。

以登记表为准:登记行删掉 → 总账里对应的行消失;来源表被删或没权限 → 标「表格不可用」。
总账里手改的内容会被下一次同步覆盖。
"""
import json
import time

from .config import ConfigError, log
from .distribute import registered
from .feishu import FeishuError, text

KINDS = ("text", "select", "user", "num", "percent", "date")
SRC_TTL_S = 600


def cell(v, kind):
    """把来源单元格的值转成能写进总账的值。公式/引用列读出来是 {"type": .., "value": [..]}。"""
    if isinstance(v, dict) and "value" in v:
        v = v.get("value")
        if kind != "user":
            v = v[0] if isinstance(v, list) and v else None
    if v is None:
        return None
    if kind == "text":
        return text(v).strip() or None
    if kind == "select":
        return text(v) or None
    if kind == "user":
        ids = [{"id": x["id"]} for x in v if isinstance(x, dict) and x.get("id")] if isinstance(v, list) else []
        return ids or None
    try:
        return int(v) if kind == "date" else round(float(v), 4)
    except (TypeError, ValueError):
        return None


def columns(ctx):
    cols = (ctx.cfg.get("ledger") or {}).get("columns") or []
    for c in cols:
        if not c.get("name") or not c.get("source") or c.get("kind") not in KINDS:
            raise ConfigError("ledger.columns 每一项都要有 name、source、kind(%s)" % "/".join(KINDS))
    G = ctx.L["ledger"]
    clash = {c["name"] for c in cols} & {G[k] for k in ("link", "owner", "sync", "note", "at", "key")}
    if clash:      # 这几列是程序自己用的,重名会互相覆盖
        raise ConfigError("ledger.columns 里的列名和总账的内置列重名了: %s(改个名字,或在 labels.ledger 里改内置列的名字)" % "、".join(sorted(clash)))
    return cols


def source_rows(ctx, tok):
    """读一张来源表格的汇总表:返回 [(record_id, {总账列名: 值})]。"""
    led = ctx.cfg["ledger"]
    memo = ctx.memo.setdefault("ledger_src", {})
    c = memo.get(tok)
    if not c or time.time() - c["at"] > SRC_TTL_S:
        tabs = ctx.fs.tables(tok)
        tid = (c or {}).get("tid")
        if tid not in [x["table_id"] for x in tabs]:      # 首次按表名找;之后按表 ID,对方把表改名也不怕
            tid = next((x["table_id"] for x in tabs if x["name"] == led["source_table"]), None)
        if not tid:
            raise FeishuError("找不到「%s」数据表" % led["source_table"], code="no_source_table")
        items = ctx.fs.fields(tok, tid)
        by_id = {x["field_id"]: x["field_name"] for x in items}
        names = set(by_id.values())
        # source 可以写字段 ID(副本里的字段 ID 与母版相同,对方改列名也不影响)或列名
        cols = {col["name"]: by_id.get(col["source"]) or (col["source"] if col["source"] in names else None)
                for col in columns(ctx)}
        c = memo[tok] = {"tid": tid, "cols": cols, "at": time.time()}
    kinds = {col["name"]: col["kind"] for col in columns(ctx)}
    out = []
    for it in ctx.fs.search(tok, c["tid"], cap=500):
        f = it.get("fields") or {}
        row = {name: cell(f.get(src), kinds[name]) if src else None for name, src in c["cols"].items()}
        if any(v is not None for v in row.values()):      # 全空的行不算
            out.append((it["record_id"], row))
    return out


def sync(ctx):
    lt = ctx.ledger_table()
    if not lt or not (ctx.cfg.get("ledger") or {}).get("source_table"):
        return
    fs, G = ctx.fs, ctx.L["ledger"]
    first = columns(ctx)[0]["name"]
    want = {}
    for tok, label, link, owners in registered(ctx):
        common = {G["link"]: {"link": link, "text": label}, G["owner"]: owners}
        try:
            rows = source_rows(ctx, tok)
        except FeishuError as e:
            if e.transient:
                raise      # 飞书临时故障:整轮作废,下次再来,别把表误标成不可用
            want[tok + ":-"] = dict(common, **{first: label, G["sync"]: G["gone"], G["note"]: G["gone_note"]})
            continue
        if not rows:
            want[tok + ":-"] = dict(common, **{first: label, G["sync"]: G["empty"],
                                    G["note"]: G["empty_note"].format(table=ctx.cfg["ledger"]["source_table"])})
        for rid, row in rows:
            row = dict(row, **common)
            row[first] = row.get(first) or label
            row[G["sync"]], row[G["note"]] = G["ok"], None
            want[tok + ":" + rid] = row
    have = {}
    for it in fs.search(lt["app_token"], lt["table_id"], cap=5000):
        k = text((it.get("fields") or {}).get(G["key"]))
        if k and k not in have:
            have[k] = it["record_id"]
        else:
            have["?" + it["record_id"]] = it["record_id"]      # 没有来源编号或重复的行 → 清掉
    now = int(time.time() * 1000)
    last = ctx.memo.setdefault("ledger_sig", {})
    add, upd, sigs = [], [], {}
    for k, row in want.items():
        sig = json.dumps(row, ensure_ascii=False, sort_keys=True)
        sigs[k] = sig
        if k not in have:
            add.append({"fields": dict({c: v for c, v in row.items() if v is not None}, **{G["key"]: k, G["at"]: now})})
        elif last.get(k) != sig:      # 内容没变就不写,省接口调用
            upd.append({"record_id": have[k], "fields": dict(row, **{G["at"]: now})})
    drop = [rid for k, rid in have.items() if k not in want]
    fs.batch(lt["app_token"], lt["table_id"], "create", add)
    fs.batch(lt["app_token"], lt["table_id"], "update", upd)
    fs.batch(lt["app_token"], lt["table_id"], "delete", drop)
    ctx.memo["ledger_sig"] = sigs
    if add or upd or drop:
        log("总账已同步 表格数=%d 新增=%d 更新=%d 删除=%d" % (len({k.split(":")[0] for k in want}), len(add), len(upd), len(drop)))


def _field(col):
    n, k = col["name"], col["kind"]
    if k == "text":
        return {"field_name": n, "type": 1}
    if k == "select":
        return {"field_name": n, "type": 3, "property": {"options": [{"name": x} for x in col.get("options") or []]}}
    if k == "user":
        return {"field_name": n, "type": 11, "property": {"multiple": True}}
    if k == "num":
        return {"field_name": n, "type": 2, "property": {"formatter": col.get("format") or "0"}}
    if k == "percent":
        return {"field_name": n, "type": 2, "ui_type": "Progress",
                "property": {"formatter": "0%", "min": 0, "max": 1, "range_customize": True}}
    return {"field_name": n, "type": 5, "property": {"date_formatter": col.get("format") or "yyyy/MM/dd"}}


def setup_ledger(ctx):
    """在登记表所在的多维表格里建「总账」数据表(只做一次)。"""
    if ctx.ledger_table():
        raise ConfigError("已经有总账了")
    reg, led = ctx.registry(), ctx.cfg.get("ledger") or {}
    if not reg:
        raise ConfigError("先建分发登记表(setup-registry)")
    cols = columns(ctx)
    if not cols or not led.get("source_table"):
        raise ConfigError("先在配置里写好 ledger.source_table 和 ledger.columns")
    if cols[0]["kind"] != "text":
        raise ConfigError("ledger.columns 的第一列必须是 text(数据表的首列不能是人员等类型)")
    fs, G, tok = ctx.fs, ctx.L["ledger"], reg["app_token"]
    fields = [_field(c) for c in cols] + [
        {"field_name": G["link"], "type": 15},
        {"field_name": G["owner"], "type": 11, "property": {"multiple": True}},
        {"field_name": G["sync"], "type": 3, "property": {"options": [{"name": G[k]} for k in ("ok", "empty", "gone")]}},
        {"field_name": G["note"], "type": 1},
        {"field_name": G["at"], "type": 5, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
        {"field_name": G["key"], "type": 1},
    ]
    body = lambda: {"table": {"name": G["table"], "default_view_name": G["view"], "fields": fields}}
    notes = []
    try:
        t = fs.call("POST", "/bitable/v1/apps/%s/tables" % tok, body())
    except FeishuError as e:      # 进度条样式不被接受 → 退回普通百分比数字
        if not any(x.get("ui_type") == "Progress" for x in fields):
            raise
        notes.append("进度条样式没建成,改用百分比数字: %s" % str(e)[:100])
        for x in fields:
            if x.get("ui_type") == "Progress":
                x.pop("ui_type")
                x["property"] = {"formatter": "0.00%"}
        t = fs.call("POST", "/bitable/v1/apps/%s/tables" % tok, body())
    tid = t["table_id"]
    ctx.state.data["ledger"] = {"table_id": tid}
    ctx.state.save()
    try:      # 新建的表可能自带空行,清掉
        ids = [x["record_id"] for x in fs.search(tok, tid)]
        fs.batch(tok, tid, "delete", ids)
    except FeishuError as e:
        notes.append("清空行失败: %s" % str(e)[:100])
    try:      # 把「来源编号」这一技术列藏起来
        p = "/bitable/v1/apps/%s/tables/%s" % (tok, tid)
        kid = next(x["field_id"] for x in fs.fields(tok, tid) if x["field_name"] == G["key"])
        gv = next(x["view_id"] for x in fs.pages(p + "/views") if x.get("view_type") == "grid")
        fs.call("PATCH", p + "/views/" + gv, {"property": {"hidden_fields": [kid]}})
    except Exception as e:
        notes.append("隐藏「%s」列失败: %s" % (G["key"], str(e)[:100]))
    return tid, notes
