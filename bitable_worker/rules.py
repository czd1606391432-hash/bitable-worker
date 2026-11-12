"""超期规则:某一列等于某个值、且超过 N 天没动的行,自动改成别的值。

例:「当前环节 = 待筛选」超过 7 天 → 改成「淘汰」,并在「原因」为空时填上一句话。
先用 `bitable-worker rules-dry` 看会影响多少行,再启用。
"""
import time

from .config import ConfigError, log
from .feishu import FeishuError, cond, text
from .ledger import cell

DAY_MS = 86400000
MAX_PER_RUN = 500


def targets(ctx, rule):
    """规则作用于哪些多维表格。tables 可写 "all"、"master"、"distributed"、具体的 app_token,或它们的列表。"""
    spec = rule.get("tables") or "all"
    spec = [spec] if isinstance(spec, str) else list(spec)
    master = (ctx.cfg.get("master") or {}).get("app_token")
    out = []
    for s in spec:
        if s == "all":
            out += [t["app_token"] for t in ctx.tables()]
        elif s == "distributed":
            out += [t["app_token"] for t in ctx.state.data.get("tables") or []]
        elif s == "master":
            out += [master] if master else []
        else:
            out.append(s)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _table_id(ctx, rule, tok):
    want = rule.get("table")
    if not want:      # 没指定数据表 → 用这个多维表格里的任务表
        return next((t["table_id"] for t in ctx.tables() if t["app_token"] == tok), None)
    for x in ctx.fs.tables(tok):
        if want in (x["table_id"], x["name"]):
            return x["table_id"]
    return None


def check(rule):
    w = rule.get("when") or {}
    if not w.get("field") or not w.get("is") or not rule.get("older_than_days") or not rule.get("set"):
        raise ConfigError("规则「%s」要有 when.field、when.is、older_than_days、set" % rule.get("name"))


def apply(ctx, rule, tok, dry=False):
    """对一个多维表格执行一条规则,返回命中的行数。"""
    check(rule)
    tid = _table_id(ctx, rule, tok)
    if not tid:
        raise FeishuError("找不到规则要用的数据表")
    names = {x["field_name"] for x in ctx.fs.fields(tok, tid)}
    w = rule["when"]
    need = [w["field"]] + list(rule["set"])
    miss = [x for x in need if x not in names]
    if miss:
        raise FeishuError("表里没有这些列: %s" % "、".join(miss))
    date_field = rule.get("date_field") if rule.get("date_field") in names else None
    fill = {k: v for k, v in (rule.get("set_if_empty") or {}).items() if k in names}
    now, limit = int(time.time() * 1000), float(rule["older_than_days"]) * DAY_MS
    flt = {"conjunction": "and", "conditions": [cond(w["field"], "is", w["is"])]}
    recs = []
    for r in ctx.fs.search(tok, tid, flt, cap=20000, automatic=True):
        f = r.get("fields") or {}
        if text(f.get(w["field"])) != w["is"]:
            continue
        base = (cell(f.get(date_field), "date") if date_field else None) or r.get("created_time")      # 没填日期就按这一行的创建时间
        if not base or now - int(base) <= limit:
            continue
        fields = dict(rule["set"])
        fields.update({k: v for k, v in fill.items() if not text(f.get(k)).strip()})
        recs.append({"record_id": r["record_id"], "fields": fields})
    if recs and not dry:
        ctx.fs.batch(tok, tid, "update", recs[:MAX_PER_RUN])
        log("规则「%s」%s 处理 %d 行" % (rule.get("name"), tok[:8], min(len(recs), MAX_PER_RUN)))
    return len(recs)


def run_due(ctx):
    for i, rule in enumerate(ctx.cfg.get("rules") or []):
        if rule.get("enabled") is False or not ctx.due("rule%d" % i, int(rule.get("every_seconds") or 300)):
            continue
        for tok in targets(ctx, rule):
            try:
                apply(ctx, rule, tok)
            except Exception as e:
                log("规则「%s」%s 没执行成" % (rule.get("name"), tok[:8]), type(e).__name__, str(e)[:200])


def dry_run(ctx):
    out = []
    for rule in ctx.cfg.get("rules") or []:
        for tok in targets(ctx, rule):
            try:
                out.append((rule.get("name"), tok, apply(ctx, rule, tok, dry=True), ""))
            except Exception as e:
                out.append((rule.get("name"), tok, 0, str(e)[:200]))
    return out
