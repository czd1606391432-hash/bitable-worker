"""任务表:一行 = 一个任务。传文件 →(选选项)→ 状态自动走 待处理/处理中/已完成/失败 → 结果写回这一行。"""
import time

from .config import log
from .feishu import FeishuError, cond, text
from .handlers import Busy, HandlerError, Job

HINT_AFTER_S = 90          # trigger=input 且配了选项列:传了文件但一直没选 → 多久后在说明里提醒一次
RECOVER_EVERY_S = 900      # 多久回收一次卡在「处理中」的行
SLOW_AFTER_FAILS, SLOW_EVERY_S = 15, 600      # 某张表连续出错这么多轮后,改为每 10 分钟才查它一次


def _rt(ctx, t):
    return ctx.rt.setdefault("%s:%s" % (t["app_token"], t["table_id"]), {"fails": 0, "last_try": 0, "recovered_at": 0, "seen": {}})


def recover(ctx, t):
    """上次进程中断时卡在「处理中」的行 → 放回「待处理」。"""
    S, n = ctx.L["status"], ctx.roles(t)["names"]
    flt = {"conjunction": "and", "conditions": [cond(n["status"], "is", S["run"])]}
    for it in ctx.fs.search(t["app_token"], t["table_id"], flt):
        ctx.fs.put(t["app_token"], t["table_id"], it["record_id"], {n["status"]: S["wait"]})
        log("恢复卡住的行", t.get("label"), it["record_id"])


def process_table(ctx, t):
    """处理一张表里到期的行,返回处理了几行。后端忙时抛 Busy(整轮收工,下轮再来)。"""
    fs, S, N = ctx.fs, ctx.L["status"], ctx.L["note"]
    app, tid = t["app_token"], t["table_id"]
    meta = ctx.roles(t)
    n = meta["names"]
    F_IN, F_ST, F_OUT, F_NOTE, F_OPT = n["input"], n["status"], n["output"], n["note"], n.get("option")
    trig = ctx.trigger(t)
    retry = N["retry"].format(wait=S["wait"])
    want_retry = {"conjunction": "and", "conditions": [cond(F_ST, "is", S["wait"])]}
    if trig == "input":      # 专用表:状态为空的行都看
        flt = {"conjunction": "or", "conditions": [cond(F_ST, "isEmpty"), cond(F_ST, "is", S["wait"])]}
    elif trig == "option":   # 业务表:大多数行并不需要处理 → 只看「选了选项且状态为空」或「待处理」
        flt = {"conjunction": "or", "children": [
            {"conjunction": "and", "conditions": [cond(F_OPT, "isNotEmpty"), cond(F_ST, "isEmpty")]}, want_retry]}
    else:                    # manual:只处理被人标成「待处理」的行
        flt = want_retry
    handler = ctx.handler(t)
    omap = t.get("options") or None
    accept = tuple(x.lower() for x in (t.get("accept") or []))
    seen = _rt(ctx, t)["seen"]
    limit = int(t.get("per_round") or ctx.max_per_round)
    done = 0
    for it in fs.search(app, tid, flt):
        rid, f = it["record_id"], it.get("fields") or {}
        files = [x for x in (f.get(F_IN) or []) if isinstance(x, dict)]
        status = text(f.get(F_ST))
        olabel = text(f.get(F_OPT)) if F_OPT else ""
        if not files:
            if status == S["wait"] or (trig == "option" and olabel):
                fs.put(app, tid, rid, {F_ST: S["fail"], F_NOTE: N["no_input"] + retry})
            continue
        if trig == "input" and f.get(F_OUT) and status != S["wait"]:      # 已有结果且没要求重做
            continue
        option = None
        if F_OPT:
            if not olabel or (omap and olabel not in omap):
                if status == S["wait"]:
                    fs.put(app, tid, rid, {F_ST: S["fail"], F_NOTE: N["no_option_retry"].format(option=F_OPT) + retry})
                elif trig == "input":
                    t0 = seen.setdefault(rid, time.time())
                    if time.time() - t0 > HINT_AFTER_S and not text(f.get(F_NOTE)):
                        fs.put(app, tid, rid, {F_NOTE: N["no_option"].format(option=F_OPT)})
                continue
            option = omap[olabel] if omap else olabel
        seen.pop(rid, None)
        day = time.strftime("%Y%m%d")
        if ctx.count["day"] != day:
            ctx.count.update(day=day, n=0)
        if ctx.count["n"] >= ctx.daily_cap:
            log("已到当日上限", ctx.daily_cap)
            return done
        fs.put(app, tid, rid, {F_ST: S["run"], F_NOTE: N["running"]})
        t0 = time.time()
        outs, errs, notes, extra, ok_n, busy = [], [], [], {}, 0, None
        for x in files[:ctx.max_files_per_row]:
            fname = text(x.get("name")) or "file"
            try:
                if accept and not fname.lower().endswith(accept):
                    raise HandlerError(N["bad_ext"].format(exts=" ".join(accept)))
                if int(x.get("size") or 0) > ctx.max_file_bytes:
                    raise HandlerError(N["too_big"].format(mb=ctx.max_file_bytes // 1024 // 1024))
                data = fs.download(x.get("url") or fs.media_url(x["file_token"]), ctx.max_file_bytes)
                res = handler(Job(fname, data, option, olabel or None, rid, f,
                                  {"label": t.get("label"), "app_token": app, "table_id": tid}))
                if not (res.files or res.fields or res.note):
                    raise HandlerError(N["failed"])
                for oname, blob in res.files:
                    outs.append({"file_token": fs.upload(app, oname, blob)})
                extra.update(res.fields)
                if res.note:
                    notes.append(res.note)
                ok_n += 1
                ctx.count["n"] += 1
            except Busy as e:
                busy = e
                break
            except (HandlerError, FeishuError) as e:
                errs.append("%s：%s" % (fname[:40], str(e)[:120]))
            except Exception as e:      # 处理器自己的 bug:只让这个文件失败,不拖垮整张表
                errs.append("%s：%s" % (fname[:40], N["failed"]))
                log("处理器异常", t.get("label"), rid, type(e).__name__, str(e)[:200])
        if len(files) > ctx.max_files_per_row:
            errs.append(N["too_many"].format(n=ctx.max_files_per_row))
        if busy and not ok_n:
            fs.put(app, tid, rid, {F_ST: S["wait"], F_NOTE: N["queued"]})
            raise busy
        upd = {}
        if n.get("done_at"):
            upd[n["done_at"]] = int(time.time() * 1000)
        if ok_n:
            if outs:
                upd[F_OUT] = outs
            upd[F_ST] = S["ok"]
            upd[F_NOTE] = ("；".join(notes) or N["ok"]) + (N["partial"].format(errs="；".join(errs)) if errs else "")
            role_cols = set(n.values())
            for k, v in extra.items():      # 顺带回填:只填存在的、空着的、不是程序自己管的列
                if k in meta["all"] and k not in role_cols and not text(f.get(k)).strip() and v not in (None, ""):
                    upd[k] = v
        else:
            upd.update({F_ST: S["fail"], F_NOTE: ("；".join(errs) or N["failed"]) + "。" + retry})
        fs.put(app, tid, rid, upd)
        done += 1
        log("完成" if ok_n else "失败", t.get("label"), rid,
            "选项=%s 文件=%d 输出=%d 耗时=%.0fs" % (option, len(files), len(outs), time.time() - t0),
            ("原因=" + "；".join(e.split("：", 1)[-1] for e in errs)) if errs else "")
        if done >= limit:      # 一张表批量导入时,不把别的表堵住
            break
    return done


def round_tables(ctx, only_recover=False):
    total = 0
    for t in ctx.tables():
        rt = _rt(ctx, t)
        if rt["fails"] >= SLOW_AFTER_FAILS and time.time() - rt["last_try"] < SLOW_EVERY_S:
            continue
        rt["last_try"] = time.time()
        try:
            if only_recover or time.time() - rt["recovered_at"] > RECOVER_EVERY_S:
                recover(ctx, t)
                rt["recovered_at"] = time.time()
            if not only_recover:
                total += process_table(ctx, t)
            rt["fails"] = 0
        except Busy as e:
            log("后端忙,本轮收工", str(e))
            break
        except Exception as e:
            rt["fails"] += 1
            ctx.cache.pop("%s:%s" % (t["app_token"], t["table_id"]), None)      # 下轮重新对一次列名
            if rt["fails"] in (1, SLOW_AFTER_FAILS):
                log("表异常(连续 %d 轮)%s" % (rt["fails"], ",改为每 10 分钟查一次" if rt["fails"] >= SLOW_AFTER_FAILS else ""),
                    t.get("label"), t["app_token"][:8], type(e).__name__, str(e)[:200])
    return total
