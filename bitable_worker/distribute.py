"""母版分发:在「分发登记表」里填「新表名称 + 交给谁」,程序复制母版、交给那个人、把副本接入处理。

为什么副本必须由程序复制:人手动复制出来的副本是全新的多维表格,不继承协作者,
程序既不知道它存在,也没有权限读写。
"""
import time

from .config import ConfigError, log
from .feishu import FeishuError, cond, text

MAX_PER_ROUND = 2      # 一轮最多分发几张(每张约 1 分钟),批量登记时不堵住文件处理


def owner_of(fs, app_token):
    d = fs.call("POST", "/drive/v1/metas/batch_query?user_id_type=open_id",
                {"request_docs": [{"doc_token": app_token, "doc_type": "bitable"}], "with_url": False})
    return ((d.get("metas") or [{}])[0]).get("owner_id") or ""


def give(ctx, app_token, open_id):
    """所有者转给 open_id(应用保留可管理权限,才能继续读写);转不了就加成可管理协作者。返回一句说明。"""
    member = {"member_type": "openid", "member_id": open_id}
    try:
        ctx.fs.call("POST", "/drive/v1/permissions/%s/members/transfer_owner?type=bitable&need_notification=true"
                            "&remove_old_owner=false&stay_put=false&old_owner_perm=full_access" % app_token, member)
        return ""
    except FeishuError as e:
        ctx.fs.call("POST", "/drive/v1/permissions/%s/members?type=bitable&need_notification=true" % app_token,
                    dict(member, perm="full_access"))
        return ctx.L["registry"]["owner_failed"].format(err=str(e)[:80])


def set_share(fs, app_token, link="closed"):
    """链接分享。closed=只有协作者能打开;tenant_readable / tenant_editable=组织内拿到链接的人可读/可编辑。对外分享一律关闭。"""
    try:
        fs.call("PATCH", "/drive/v1/permissions/%s/public?type=bitable" % app_token,
                {"external_access": False, "link_share_entity": link, "invite_external": False})
        return True
    except FeishuError:
        return False


def master_tables(ctx):
    m = ctx.cfg.get("master") or {}
    return [t for t in ctx.cfg.get("tables") or [] if t["app_token"] == m.get("app_token")]


def copy_master(ctx, new_name, owner_id):
    """复制母版 → 清示例行 → 恢复自动化开关 → 设分享 → 转所有者 → 接入处理。返回 (链接, 说明)。"""
    fs, m, R = ctx.fs, ctx.cfg["master"], ctx.L["registry"]
    src = m["app_token"]
    # 母版此刻的表名、列名(母版改过名也跟得上)
    tab_names = {x["table_id"]: x["name"] for x in fs.tables(src)}
    plan = []
    for mt in master_tables(ctx):
        if mt["table_id"] not in tab_names:
            raise FeishuError("母版里找不到数据表 %s" % mt["table_id"])
        plan.append((mt, tab_names[mt["table_id"]], ctx.roles(mt, force=True)["names"]))
    # 必须带内容复制:只复制结构的话,自动化流程和仪表盘都不会带过去
    app = fs.call("POST", "/bitable/v1/apps/%s/copy" % src,
                  {"name": new_name, "without_content": False, "time_zone": m.get("time_zone") or "Asia/Shanghai"},
                  timeout=120)["app"]
    tok, url = app["app_token"], app.get("url") or ""
    new, err = [], None
    for _ in range(8):      # 副本刚建好时表结构可能还没就绪,等一等
        new, err = [], None
        try:
            ctabs = {x["name"]: x["table_id"] for x in fs.tables(tok)}
            for mt, tname, names in plan:
                if tname not in ctabs:
                    err = "副本里找不到数据表「%s」" % tname
                    break
                by_name = {x["field_name"]: x["field_id"] for x in fs.fields(tok, ctabs[tname])}
                ids = {r: by_name.get(nm) for r, nm in names.items()}
                if not all(ids.values()):
                    err = "副本的「%s」里缺列" % tname
                    break
                e = {k: v for k, v in mt.items() if k not in ("app_token", "table_id", "fields", "label")}
                e.update({"app_token": tok, "table_id": ctabs[tname], "fields": ids, "label": new_name, "url": url,
                          "source": "distributed", "copied_at": time.strftime("%Y-%m-%d %H:%M")})
                new.append(e)
        except FeishuError as e:
            err = str(e)
        if not err:
            break
        time.sleep(3)
    note = []
    # 1) 清掉从母版带过来的示例行(此刻副本里的自动化全是停用状态,删行不会触发任何流程)
    if not m.get("keep_rows"):
        try:
            for x in fs.tables(tok):
                for _ in range(20):
                    rids = [r["record_id"] for r in fs.search(tok, x["table_id"], cap=500)]
                    if not rids:
                        break
                    fs.batch(tok, x["table_id"], "delete", rids, size=500)
        except FeishuError:
            note.append(R["rows_left"])
    # 2) 接口复制出来的自动化默认全部停用 → 按母版此刻的开关逐条恢复
    try:
        flows = lambda a: fs.call("GET", "/bitable/v1/apps/%s/workflows?page_size=100" % a).get("workflows") or []
        on = {x.get("title") for x in flows(src) if x.get("status") == "Enable"}
        bad = []
        for x in flows(tok):
            if x.get("title") in on and x.get("status") != "Enable":
                try:
                    fs.call("PUT", "/bitable/v1/apps/%s/workflows/%s" % (tok, x["workflow_id"]), {"status": "Enable"})
                except FeishuError:
                    bad.append(str(x.get("title")))
        if bad:
            note.append(R["flows_off"].format(titles="、".join(bad)))
    except FeishuError:
        note.append(R["flows_unknown"])
    # 3) 链接分享 4) 转所有者(应用自己建的表,人是看不到的,必须交出去)
    if not set_share(fs, tok, m.get("link_share") or "closed"):
        note.append(R["share_failed"])
    g = give(ctx, tok, owner_id)
    if g:
        note.append(g)
    if err:
        raise FeishuError(R["not_wired"].format(url=url, err=err))
    ctx.state.data.setdefault("copies", []).append({"app_token": tok, "url": url, "label": new_name})
    ctx.state.data["tables"] += new
    ctx.state.save()
    return url, "；".join(note)


def dispatch_round(ctx):
    reg = ctx.registry()
    if not reg or not ctx.cfg.get("master"):
        return 0
    fs, R = ctx.fs, ctx.L["registry"]
    app, tid = reg["app_token"], reg["table_id"]
    flt = {"conjunction": "or", "conditions": [cond(R["status"], "isEmpty"), cond(R["status"], "is", R["wait"])]}
    done = 0
    for it in fs.search(app, tid, flt):
        rid, f = it["record_id"], it.get("fields") or {}
        name = text(f.get(R["name"])).strip()
        owners = [x for x in (f.get(R["owner"]) or []) if isinstance(x, dict) and x.get("id")]
        if not name or not owners:
            if text(f.get(R["status"])) == R["wait"]:
                fs.put(app, tid, rid, {R["status"]: R["fail"], R["note"]: R["need_both"].format(
                    name=R["name"], owner=R["owner"], wait=R["wait"])})
            continue
        fs.put(app, tid, rid, {R["status"]: R["run"]})
        try:
            url, note = copy_master(ctx, name[:80], owners[0]["id"])
            fs.put(app, tid, rid, {R["status"]: R["ok"], R["link"]: {"link": url, "text": name[:80]},
                                   R["note"]: R["delivered"] + note})
            log("已分发", rid, "当前处理的表数=%d" % len(ctx.tables()))
        except Exception as e:
            fs.put(app, tid, rid, {R["status"]: R["fail"], R["note"]: str(e)[:300]})
            log("分发失败", rid, type(e).__name__, str(e)[:200])
        done += 1
        if done >= MAX_PER_ROUND:
            break
    return done


def registered(ctx):
    """登记表里「已分发且有链接」的表格:[(token, 名称, 链接, 负责人)]。手动登记的老表也算。"""
    reg, R = ctx.registry(), ctx.L["registry"]
    out = []
    for r in ctx.fs.search(reg["app_token"], reg["table_id"], cap=2000):
        f = r.get("fields") or {}
        link = (f.get(R["link"]) or {}).get("link") or "" if isinstance(f.get(R["link"]), dict) else ""
        if text(f.get(R["status"])) != R["ok"] or "/base/" not in link:
            continue
        tok = link.split("/base/", 1)[1].split("?")[0].strip("/")
        owners = [{"id": x["id"]} for x in (f.get(R["owner"]) or []) if isinstance(x, dict) and x.get("id")] or None
        out.append((tok, text(f.get(R["name"])).strip() or tok, link, owners))
    return out


def setup_registry(ctx, title):
    """建「分发登记表」(只做一次)。所有者取自母版的所有者。"""
    if ctx.registry():
        raise ConfigError("已经有分发登记表了: %s" % (ctx.registry().get("url") or ctx.registry()["app_token"]))
    m = ctx.cfg.get("master")
    if not m:
        raise ConfigError("先在配置里写好 master(母版)")
    fs, R = ctx.fs, ctx.L["registry"]
    owner = owner_of(fs, m["app_token"])
    if not owner:
        raise ConfigError("查不到母版的所有者,没有建表")
    app = fs.call("POST", "/bitable/v1/apps", {"name": title})["app"]
    tok = app["app_token"]
    fields = [
        {"field_name": R["name"], "type": 1},
        {"field_name": R["owner"], "type": 11, "property": {"multiple": False}},
        {"field_name": R["status"], "type": 3, "property": {"options": [{"name": R[k]} for k in ("wait", "run", "ok", "fail")]}},
        {"field_name": R["link"], "type": 15},
        {"field_name": R["note"], "type": 1},
        {"field_name": R["creator"], "type": 1003},
        {"field_name": R["created"], "type": 1001, "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
    ]
    t = fs.call("POST", "/bitable/v1/apps/%s/tables" % tok,
                {"table": {"name": R["table"], "default_view_name": R["view"], "fields": fields}})
    ctx.state.data["registry"] = {"app_token": tok, "table_id": t["table_id"], "url": app.get("url")}
    ctx.state.save()
    if app.get("default_table_id"):      # 新建多维表格自带一张空表,删掉
        try:
            fs.call("DELETE", "/bitable/v1/apps/%s/tables/%s" % (tok, app["default_table_id"]))
        except FeishuError:
            pass
    set_share(fs, tok)
    return app.get("url"), give(ctx, tok, owner)
