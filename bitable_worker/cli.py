"""命令行入口。"""
import argparse
import json
import os
import sys
import time

from . import __version__, distribute, jobs, ledger, rules
from .config import ConfigError, load, log
from .feishu import FeishuError


def one_round(ctx, only_recover=False):
    total = 0
    if not only_recover:
        try:
            total += distribute.dispatch_round(ctx)
        except Exception as e:
            log("分发登记表本轮异常", type(e).__name__, str(e)[:200])
        led = ctx.cfg.get("ledger") or {}
        if ctx.ledger_table() and ctx.due("ledger", int(led.get("every_seconds") or 300), force=bool(total)):
            try:      # 定时同步;刚分发了新表则立刻同步一次
                ledger.sync(ctx)
            except Exception as e:
                log("总账本轮没同步成", type(e).__name__, str(e)[:200])
        rules.run_due(ctx)
    return total + jobs.round_tables(ctx, only_recover)


def cmd_run(ctx, a):
    log("启动,每 %ds 查一次,表数=%d" % (ctx.poll_s, len(ctx.tables())))
    one_round(ctx, only_recover=True)      # 启动时先把上次卡在「处理中」的行放回去
    while True:
        did = 0
        try:
            did = one_round(ctx)
        except Exception as e:      # 任何异常都不让常驻进程退出
            log("本轮异常", type(e).__name__, str(e)[:200])
        time.sleep(3 if did else ctx.poll_s)      # 还有活在排队就接着干


def cmd_once(ctx, a):
    one_round(ctx, only_recover=True)
    print("本轮处理:", one_round(ctx))


def cmd_info(ctx, a):
    reg = ctx.registry()
    print("母版:", (ctx.cfg.get("master") or {}).get("app_token") or "未配置",
          "| 分发登记表:", (reg or {}).get("url") or (reg or {}).get("app_token") or "未建",
          "| 总账:", (ctx.ledger_table() or {}).get("table_id") or "未建")
    bad = 0
    for t in ctx.tables():
        try:
            nm = ctx.roles(t)["names"]
        except Exception as e:
            nm, bad = "!! %s" % e, bad + 1
        print("-", t.get("label") or t["table_id"], t.get("url") or t["app_token"],
              "触发=%s 处理器=%s" % (ctx.trigger(t), t.get("handler") or "default"), nm)
    return 1 if bad else 0


def cmd_init_table(ctx, a):
    """给一张已有的表补上程序要用的列,并打印可以粘进配置的片段。已有的列不动。"""
    S = ctx.L["status"]
    have = {x["field_name"] for x in ctx.fs.fields(a.app_token, a.table_id)}
    if a.input not in have:
        raise ConfigError("表里没有「%s」这一列(要先有一个放文件的附件列)" % a.input)
    want = [(a.status, {"type": 3, "property": {"options": [{"name": S[k]} for k in ("wait", "run", "ok", "fail")]}}),
            (a.output, {"type": 17}), (a.note, {"type": 1})]
    if a.option:
        want.insert(0, (a.option, {"type": 3, "property": {"options": [{"name": x} for x in (a.choices or "").split(",") if x]}}))
    for name, spec in want:
        if name in have:
            print("已有列,跳过:", name)
            continue
        ctx.fs.call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (a.app_token, a.table_id), dict(spec, field_name=name))
        print("已加列:", name)
    fields = {"input": a.input, "status": a.status, "output": a.output, "note": a.note}
    if a.option:
        fields["option"] = a.option
    print("\n把下面这段加进 config.json 的 tables:\n")
    print(json.dumps({"label": "我的表", "app_token": a.app_token, "table_id": a.table_id,
                      "handler": "default", "fields": fields}, ensure_ascii=False, indent=2))


def cmd_setup_registry(ctx, a):
    url, note = distribute.setup_registry(ctx, a.title)
    print("分发登记表已建:", url, note or "(所有者已转交)")


def cmd_setup_ledger(ctx, a):
    tid, notes = ledger.setup_ledger(ctx)
    print("总账已建,数据表", tid, "；".join(notes))


def cmd_rules_dry(ctx, a):
    rows = rules.dry_run(ctx)
    if not rows:
        print("配置里没有规则")
    for name, tok, n, err in rows:
        print("规则「%s」 %s → %s" % (name, tok, ("出错: " + err) if err else "会改 %d 行" % n))


COMMANDS = {
    "run": (cmd_run, "常驻运行(配合 systemd 等使用)"),
    "once": (cmd_once, "跑一轮就退出(联调用)"),
    "info": (cmd_info, "看接入了哪些表、每张表的列是否都对得上"),
    "init-table": (cmd_init_table, "给一张已有的表补上程序要用的列"),
    "setup-registry": (cmd_setup_registry, "建「分发登记表」(只做一次)"),
    "setup-ledger": (cmd_setup_ledger, "建「总账」(只做一次)"),
    "rules-dry": (cmd_rules_dry, "只算不改:看每条超期规则会影响多少行"),
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bitable-worker", description="把飞书多维表格当任务入口:传文件 → 自动处理 → 结果写回。")
    ap.add_argument("-c", "--config", default=os.environ.get("BITABLE_WORKER_CONFIG", "config.json"), help="配置文件(默认 ./config.json)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    for name, (_, desc) in COMMANDS.items():
        p = sub.add_parser(name, help=desc)
        if name == "init-table":
            p.add_argument("app_token")
            p.add_argument("table_id")
            p.add_argument("--input", required=True, help="放待处理文件的附件列(必须已存在)")
            p.add_argument("--option", help="选项列(单选);不需要选项就不写")
            p.add_argument("--choices", help="选项列的可选值,逗号分隔")
            p.add_argument("--status", default="处理状态")
            p.add_argument("--output", default="处理结果")
            p.add_argument("--note", default="处理说明")
        if name == "setup-registry":
            p.add_argument("--title", default="母版分发登记", help="新建的多维表格叫什么")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    try:
        ctx = load(a.config)
        return COMMANDS[a.cmd][0](ctx, a) or 0
    except (ConfigError, FeishuError) as e:
        print("出错:", e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
