"""全流程测试:用内存里的假飞书,不联网。运行: python3 -m unittest discover -s tests -v"""
import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bitable_worker import distribute, jobs, ledger, rules          # noqa: E402
from bitable_worker.cli import main as cli_main, one_round          # noqa: E402
from bitable_worker.config import ConfigError, Context, State       # noqa: E402
from bitable_worker.feishu import FeishuError                       # noqa: E402
from bitable_worker.handlers import Busy, HandlerError, Job, Result, http_call, _disposition_name   # noqa: E402
from fake_feishu import FakeFeishu                                  # noqa: E402

DAY = 86400000
CALLS = []


def upper(job):
    """测试用处理器:把内容转成大写。文件名带 busy / bad / boom 时模拟各种失败。"""
    CALLS.append(job)
    if "busy" in job.name:
        raise Busy("忙")
    if "bad" in job.name:
        raise HandlerError("文件内容看不懂")
    if "boom" in job.name:
        raise RuntimeError("bug")
    return Result([(job.name.rsplit(".", 1)[0] + "-%s.txt" % (job.option or "x"), job.data.upper())],
                  fields={"姓名": "张三", "不存在的列": "x"})


JOB_FIELDS = [("姓名", 1), ("附件", 17), ("版本", 3), ("状态", 3), ("结果", 17), ("说明", 1)]
ROLE = {"input": "附件", "option": "版本", "status": "状态", "output": "结果", "note": "说明"}


def make(fs, cfg):
    d = tempfile.mkdtemp()
    cfg.setdefault("handlers", {"default": {"type": "python", "target": "test_all:upper"}})
    return Context(cfg, State(os.path.join(d, "state.json")), fs, d)


class JobsTest(unittest.TestCase):
    def setUp(self):
        del CALLS[:]
        self.fs = FakeFeishu()
        self.app = self.fs.add_app("业务表")
        self.tid, self.ids = self.fs.add_table(self.app, "任务", JOB_FIELDS)
        self.t = {"label": "任务", "app_token": self.app, "table_id": self.tid, "fields": dict(ROLE),
                  "options": {"甲": "a", "乙": "b"}}
        self.ctx = make(self.fs, {"tables": [self.t]})

    def add(self, name=None, **f):
        if name:
            f["附件"] = [self.fs.add_file(name, b"hello")]
        return self.fs.add_row(self.app, self.tid, f)

    def run_table(self):
        return jobs.process_table(self.ctx, self.t)

    def test_success_and_backfill(self):
        rid = self.add("a.txt", 版本="甲")
        idle = self.add("b.txt")                         # 没选版本:业务表里不该动它
        self.assertEqual(self.run_table(), 1)
        r = self.fs.row(self.app, self.tid, rid)
        self.assertEqual(r["状态"], "已完成")
        self.assertEqual(r["结果"][0]["name"], "a-a.txt")
        self.assertEqual(self.fs.files[r["结果"][0]["file_token"]][1], b"HELLO")
        self.assertEqual(r["姓名"], "张三")                # 处理器顺带回填的列
        self.assertNotIn("不存在的列", r)
        self.assertNotIn("状态", self.fs.row(self.app, self.tid, idle))
        self.assertEqual(self.run_table(), 0)            # 做完的行不会重复做

    def test_backfill_never_overwrites(self):
        rid = self.add("a.txt", 版本="甲", 姓名="李四")
        self.run_table()
        self.assertEqual(self.fs.row(self.app, self.tid, rid)["姓名"], "李四")

    def test_redo_by_resetting_status(self):
        rid = self.add("a.txt", 版本="甲")
        self.run_table()
        self.fs.row(self.app, self.tid, rid).update({"状态": "待处理", "版本": "乙"})
        self.assertEqual(self.run_table(), 1)
        self.assertEqual(self.fs.row(self.app, self.tid, rid)["结果"][0]["name"], "a-b.txt")

    def test_no_file(self):
        rid = self.add(版本="甲")
        self.run_table()
        r = self.fs.row(self.app, self.tid, rid)
        self.assertEqual(r["状态"], "失败")
        self.assertIn("还没有上传文件", r["说明"])

    def test_handler_error_shown_to_user(self):
        rid = self.add("bad.txt", 版本="甲")
        self.run_table()
        r = self.fs.row(self.app, self.tid, rid)
        self.assertEqual(r["状态"], "失败")
        self.assertIn("文件内容看不懂", r["说明"])
        self.assertIn("待处理", r["说明"])

    def test_handler_bug_does_not_kill_table(self):
        a, b = self.add("boom.txt", 版本="甲"), self.add("ok.txt", 版本="甲")
        self.assertEqual(self.run_table(), 2)
        self.assertEqual(self.fs.row(self.app, self.tid, a)["状态"], "失败")
        self.assertEqual(self.fs.row(self.app, self.tid, b)["状态"], "已完成")

    def test_busy_requeues(self):
        rid = self.add("busy.txt", 版本="甲")
        with self.assertRaises(Busy):
            self.run_table()
        r = self.fs.row(self.app, self.tid, rid)
        self.assertEqual(r["状态"], "待处理")
        self.assertIn("排队", r["说明"])

    def test_partial_success(self):
        rid = self.fs.add_row(self.app, self.tid, {"版本": "甲", "附件": [
            self.fs.add_file("a.txt", b"x"), self.fs.add_file("bad.txt", b"x")]})
        self.run_table()
        r = self.fs.row(self.app, self.tid, rid)
        self.assertEqual(r["状态"], "已完成")
        self.assertEqual(len(r["结果"]), 1)
        self.assertIn("未成功", r["说明"])

    def test_accept_and_unknown_option(self):
        self.t["accept"] = [".pdf"]
        a = self.add("a.txt", 版本="甲")
        b = self.add("b.pdf", 版本="丙", 状态="待处理")       # 丙不在 options 里
        self.run_table()
        self.assertIn("只支持", self.fs.row(self.app, self.tid, a)["说明"])
        self.assertEqual(self.fs.row(self.app, self.tid, b)["状态"], "失败")
        self.assertIn("版本", self.fs.row(self.app, self.tid, b)["说明"])

    def test_per_round_limit_and_recover(self):
        for i in range(5):
            self.add("f%d.txt" % i, 版本="甲")
        self.assertEqual(self.run_table(), 3)             # 一张表一轮最多 3 行
        self.assertEqual(self.run_table(), 2)
        stuck = self.add("s.txt", 版本="甲", 状态="处理中")
        jobs.recover(self.ctx, self.t)
        self.assertEqual(self.fs.row(self.app, self.tid, stuck)["状态"], "待处理")

    def test_rename_column_still_works(self):
        rid = self.add("a.txt", 版本="甲")
        self.run_table()                                   # 第一次运行把列名解析成字段 ID
        for f in self.fs.apps[self.app]["tables"][self.tid]["fields"]:
            if f["field_name"] == "状态":
                f["field_name"] = "进度"
        row = self.fs.row(self.app, self.tid, rid)
        row["进度"] = row.pop("状态")
        self.ctx.cache.clear()
        rid2 = self.add("b.txt", 版本="甲")
        self.assertEqual(self.run_table(), 1)
        self.assertEqual(self.fs.row(self.app, self.tid, rid2)["进度"], "已完成")

    def test_trigger_input_and_manual(self):
        self.t["trigger"] = "input"
        a = self.add("a.txt", 版本="甲")
        self.assertEqual(self.run_table(), 1)
        self.assertEqual(self.fs.row(self.app, self.tid, a)["状态"], "已完成")
        t2 = dict(self.t, fields={k: v for k, v in ROLE.items() if k != "option"})
        t2.pop("trigger")
        ctx = make(self.fs, {"tables": [t2]})
        self.assertEqual(ctx.trigger(t2), "manual")        # 没有选项列时默认只处理手动标记的行
        b, c = self.add("b.txt"), self.add("c.txt", 状态="待处理")
        self.assertEqual(jobs.process_table(ctx, t2), 1)
        self.assertNotIn("状态", self.fs.row(self.app, self.tid, b))
        self.assertEqual(self.fs.row(self.app, self.tid, c)["状态"], "已完成")

    def test_bad_config(self):
        with self.assertRaises(ConfigError):
            make(self.fs, {"tables": [dict(self.t, fields={"input": "附件"})]})
        with self.assertRaises(ConfigError):
            make(self.fs, {"tables": [dict(self.t, handler="没有")]})
        ctx = make(self.fs, {"tables": [dict(self.t, fields=dict(ROLE, note="没有这列"))]})
        with self.assertRaises(ConfigError):
            ctx.roles(ctx.tables()[0])

    def test_failing_table_does_not_block_others(self):
        app2 = self.fs.add_app("另一张")
        tid2, _ = self.fs.add_table(app2, "任务", JOB_FIELDS)
        t2 = dict(self.t, app_token=app2, table_id=tid2, label="另一张")
        ctx = make(self.fs, {"tables": [dict(self.t, table_id="tblgone"), t2]})
        rid = self.fs.add_row(app2, tid2, {"版本": "甲", "附件": [self.fs.add_file("a.txt", b"x")]})
        self.assertEqual(jobs.round_tables(ctx), 1)
        self.assertEqual(self.fs.row(app2, tid2, rid)["状态"], "已完成")


class DistributeLedgerTest(unittest.TestCase):
    def setUp(self):
        del CALLS[:]
        fs = self.fs = FakeFeishu()
        self.master = fs.add_app("母版", owner="ou_boss")
        self.tid, _ = fs.add_table(self.master, "任务", JOB_FIELDS)
        self.sum_tid, self.sum_ids = fs.add_table(self.master, "项目汇总", [("项目", 1), ("完成率", 2), ("负责人", 11), ("截止", 5)])
        fs.add_row(self.master, self.tid, {"姓名": "示例行"})
        fs.add_row(self.master, self.sum_tid, {"项目": "示例项目"})
        fs.apps[self.master]["workflows"] = [{"workflow_id": "wf1", "title": "提醒", "status": "Enable"},
                                             {"workflow_id": "wf2", "title": "没开的", "status": "Disable"}]
        self.cfg = {
            "master": {"app_token": self.master, "link_share": "tenant_editable"},
            "tables": [{"label": "母版", "app_token": self.master, "table_id": self.tid, "fields": dict(ROLE)}],
            "ledger": {"source_table": "项目汇总", "columns": [
                {"name": "项目", "source": self.sum_ids["项目"], "kind": "text"},
                {"name": "完成率", "source": "完成率", "kind": "percent"},
                {"name": "负责人", "source": "负责人", "kind": "user"},
                {"name": "截止", "source": "截止", "kind": "date"}]},
        }
        self.ctx = make(fs, self.cfg)
        self.url, note = distribute.setup_registry(self.ctx, "分发登记")
        self.assertEqual(note, "")
        self.reg = self.ctx.registry()

    def register(self, name="华东项目", owner="ou_alice", **extra):
        f = {"新表名称": name, "交给谁": [{"id": owner}] if owner else None}
        f.update(extra)
        return self.fs.add_row(self.reg["app_token"], self.reg["table_id"], f)

    def reg_row(self, rid):
        return self.fs.row(self.reg["app_token"], self.reg["table_id"], rid)

    def test_setup_registry(self):
        a = self.fs.apps[self.reg["app_token"]]
        self.assertEqual(a["owner"], "ou_boss")                       # 登记表交给母版的所有者
        self.assertEqual([t["name"] for t in a["tables"].values()], ["分发登记"])
        self.assertEqual(a["public"]["link_share_entity"], "closed")
        with self.assertRaises(ConfigError):
            distribute.setup_registry(self.ctx, "再建一次")

    def test_distribute_end_to_end(self):
        rid = self.register()
        self.assertEqual(distribute.dispatch_round(self.ctx), 1)
        r = self.reg_row(rid)
        self.assertEqual(r["状态"], "已分发", r.get("说明"))
        tok = r["新表链接"]["link"].split("/base/")[1]
        copy_ = self.fs.apps[tok]
        self.assertEqual(copy_["owner"], "ou_alice")
        self.assertIn({"id": "app", "perm": "full_access"}, copy_["members"])      # 应用保留管理权限
        self.assertEqual(copy_["public"], {"external_access": False, "link_share_entity": "tenant_editable", "invite_external": False})
        self.assertEqual({w["title"]: w["status"] for w in copy_["workflows"]}, {"提醒": "Enable", "没开的": "Disable"})
        self.assertTrue(all(not t["records"] for t in copy_["tables"].values()))   # 示例行已清
        self.assertEqual(self.fs.rows(self.master, self.tid) != {}, True)          # 母版不动
        # 副本已接入处理,而且重启(重新读状态文件)后还在
        new = [t for t in self.ctx.tables() if t["app_token"] == tok]
        self.assertEqual(len(new), 1)
        ctx2 = Context(self.cfg, State(self.ctx.state.path), self.fs, self.ctx.base_dir)
        t = [x for x in ctx2.tables() if x["app_token"] == tok][0]
        row = self.fs.add_row(tok, t["table_id"], {"版本": "甲", "附件": [self.fs.add_file("a.txt", b"hi")]})
        self.assertEqual(jobs.round_tables(ctx2), 1)
        self.assertEqual(self.fs.row(tok, t["table_id"], row)["状态"], "已完成")
        self.assertEqual(distribute.dispatch_round(self.ctx), 0)                    # 不会重复分发

    def test_incomplete_registration(self):
        a = self.register(owner=None)
        b = self.register(owner=None, 状态="待分发")
        self.assertEqual(distribute.dispatch_round(self.ctx), 0)
        self.assertNotIn("状态", self.reg_row(a))                       # 还在填,不打扰
        self.assertEqual(self.reg_row(b)["状态"], "失败")

    def test_owner_transfer_fallback(self):
        rid = self.register()
        self.fs.fail_next.append(("POST", "transfer_owner", FeishuError("code=1063002: no permission")))
        distribute.dispatch_round(self.ctx)
        r = self.reg_row(rid)
        self.assertEqual(r["状态"], "已分发")
        self.assertIn("可管理协作者", r["说明"])

    def test_copy_failure_marks_row(self):
        rid = self.register()
        self.fs.fail_next.append(("POST", "/copy", FeishuError("code=1254036: Base is copying")))
        distribute.dispatch_round(self.ctx)
        self.assertEqual(self.reg_row(rid)["状态"], "失败")
        self.assertEqual(len(self.ctx.state.data["tables"]), 0)

    def test_at_most_two_per_round(self):
        for i in range(3):
            self.register(name="表%d" % i)
        self.assertEqual(distribute.dispatch_round(self.ctx), 2)
        self.assertEqual(distribute.dispatch_round(self.ctx), 1)

    def test_ledger(self):
        fs = self.fs
        tid, notes = ledger.setup_ledger(self.ctx)
        lt = self.ctx.ledger_table()
        self.assertEqual(fs.rows(lt["app_token"], tid), {})             # 自带的空行已清
        ra, rb = self.register("甲项目", "ou_a"), self.register("乙项目", "ou_b")
        distribute.dispatch_round(self.ctx)
        ta, tb = [self.reg_row(x)["新表链接"]["link"].split("/base/")[1] for x in (ra, rb)]
        sa = next(k for k, v in fs.apps[ta]["tables"].items() if v["name"] == "项目汇总")
        p1 = fs.add_row(ta, sa, {"项目": "甲-一期", "完成率": {"type": 2, "value": [0.5]}, "负责人": [{"id": "ou_pm", "name": "x"}], "截止": 1800000000000})
        fs.add_row(ta, sa, {"项目": "甲-二期", "完成率": 0.25})
        ledger.sync(self.ctx)
        rows = sorted(fs.rows(lt["app_token"], tid).values(), key=lambda r: r["来源编号"])
        self.assertEqual(len(rows), 3)
        by = {r["项目"]: r for r in rows}
        self.assertEqual(by["甲-一期"]["完成率"], 0.5)                    # 公式列的值被拆出来
        self.assertEqual(by["甲-一期"]["负责人"], [{"id": "ou_pm"}])
        self.assertEqual(by["甲-一期"]["截止"], 1800000000000)
        self.assertEqual(by["甲-一期"]["来源表格"]["text"], "甲项目")
        self.assertEqual(by["甲-一期"]["表格负责人"], [{"id": "ou_a"}])
        self.assertEqual(by["甲-一期"]["同步状态"], "正常")
        self.assertEqual(by["乙项目"]["同步状态"], "未填写")              # 乙还没填汇总表
        n_calls = len(fs.calls)
        ledger.sync(self.ctx)                                           # 没变化 → 不写
        self.assertFalse([c for c in fs.calls[n_calls:] if "batch_" in c[1]])
        fs.row(ta, sa, p1)["完成率"] = 0.75                              # 来源改了 → 更新
        ledger.sync(self.ctx)
        self.assertEqual({r["项目"]: r for r in fs.rows(lt["app_token"], tid).values()}["甲-一期"]["完成率"], 0.75)
        del fs.apps[tb]                                                 # 乙表被删 → 标不可用
        ledger.sync(self.ctx)
        self.assertEqual({r["项目"]: r for r in fs.rows(lt["app_token"], tid).values()}["乙项目"]["同步状态"], "表格不可用")
        fs.fail_next.append(("GET", "/apps/%s/tables" % ta, FeishuError("HTTP 502", transient=True)))
        self.ctx.memo.pop("ledger_src")
        with self.assertRaises(FeishuError):                            # 临时故障:整轮作废,不误标
            ledger.sync(self.ctx)
        self.assertEqual(len(fs.rows(lt["app_token"], tid)), 3)
        del fs.apps[self.reg["app_token"]]["tables"][self.reg["table_id"]]["records"][ra]
        ledger.sync(self.ctx)                                           # 登记行删掉 → 总账对应行消失
        self.assertEqual([r["项目"] for r in fs.rows(lt["app_token"], tid).values()], ["乙项目"])

    def test_ledger_column_clash(self):
        self.cfg["ledger"]["columns"].append({"name": "表格负责人", "source": "负责人", "kind": "user"})
        with self.assertRaises(ConfigError):
            ledger.setup_ledger(self.ctx)

    def test_full_round(self):
        ledger.setup_ledger(self.ctx)
        rid = self.register()
        self.assertEqual(one_round(self.ctx), 1)
        self.assertEqual(self.reg_row(rid)["状态"], "已分发")
        lt = self.ctx.ledger_table()
        self.assertEqual(len(self.fs.rows(lt["app_token"], lt["table_id"])), 1)     # 分发后立刻同步了一次总账


class RulesTest(unittest.TestCase):
    def setUp(self):
        fs = self.fs = FakeFeishu()
        self.app = fs.add_app("候选人表")
        self.tid, _ = fs.add_table(self.app, "候选人", [("姓名", 1), ("环节", 3), ("推荐日期", 5), ("原因", 1)])
        self.now = int(time.time() * 1000)
        self.rule = {"name": "超期淘汰", "tables": [self.app], "table": "候选人", "when": {"field": "环节", "is": "待筛选"},
                     "date_field": "推荐日期", "older_than_days": 7, "set": {"环节": "淘汰"},
                     "set_if_empty": {"原因": "超过7天未进入面试，系统自动淘汰", "没有的列": "x"}}
        self.ctx = make(fs, {"tables": [], "rules": [self.rule]})

    def add(self, **f):
        created = f.pop("created", self.now)
        return self.fs.add_row(self.app, self.tid, f, created=created)

    def test_stale_rows(self):
        old = self.add(环节="待筛选", 推荐日期=self.now - 10 * DAY)
        fresh = self.add(环节="待筛选", 推荐日期=self.now - 3 * DAY)
        other = self.add(环节="面试中", 推荐日期=self.now - 30 * DAY)
        by_created = self.add(环节="待筛选", created=self.now - 8 * DAY)               # 没填日期 → 按创建时间
        has_reason = self.add(环节="待筛选", 推荐日期=self.now - 9 * DAY, 原因="人工备注")
        self.assertEqual(rules.dry_run(self.ctx), [("超期淘汰", self.app, 3, "")])
        self.assertEqual(self.fs.row(self.app, self.tid, old)["环节"], "待筛选")        # 试算不改数据
        rules.run_due(self.ctx)
        g = lambda rid: self.fs.row(self.app, self.tid, rid)
        self.assertEqual((g(old)["环节"], g(old)["原因"]), ("淘汰", "超过7天未进入面试，系统自动淘汰"))
        self.assertEqual(g(fresh)["环节"], "待筛选")
        self.assertEqual(g(other)["环节"], "面试中")
        self.assertEqual(g(by_created)["环节"], "淘汰")
        self.assertEqual((g(has_reason)["环节"], g(has_reason)["原因"]), ("淘汰", "人工备注"))
        n = len(self.fs.calls)
        rules.run_due(self.ctx)                                                       # 没到下一次执行时间
        self.assertEqual(len(self.fs.calls), n)

    def test_scopes(self):
        self.ctx.cfg["master"] = {"app_token": "appM"}
        self.ctx.state.data["tables"] = [{"app_token": "appD", "table_id": "t"}]
        self.assertEqual(rules.targets(self.ctx, {"tables": ["master", "distributed"]}), ["appM", "appD"])
        self.assertEqual(rules.targets(self.ctx, {"tables": "all"}), ["appD"])
        self.assertEqual(rules.targets(self.ctx, {"tables": ["appX", "appX"]}), ["appX"])

    def test_missing_column_reports(self):
        self.rule["set"] = {"没有": "x"}
        self.assertIn("没有这些列", rules.dry_run(self.ctx)[0][3])


class _Srv(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        mode = self.path.strip("/")
        _Srv.seen = (dict(self.headers), body)
        if mode == "file":
            out, hdr = body.upper(), {"Content-Type": "text/plain", "Content-Disposition": "attachment; filename*=UTF-8''%E7%BB%93%E6%9E%9C.txt"}
        elif mode == "json":
            out = json.dumps({"files": [{"name": "r.txt", "content_base64": base64.b64encode(b"R").decode()}],
                              "fields": {"姓名": "王五"}, "note": "好了"}).encode()
            hdr = {"Content-Type": "application/json; charset=utf-8"}
        elif mode == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("a.txt", "A")
                z.writestr("d/b.txt", "B")
            out, hdr = buf.getvalue(), {"Content-Type": "application/zip"}
        elif mode == "busy":
            self.send_response(503)
            self.end_headers()
            return
        else:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "文件是空的"}).encode())
            return
        self.send_response(200)
        for k, v in hdr.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(out)


class HttpHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Srv)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, mode, **spec):
        return http_call(dict(spec, url=self.url + mode), Job("简历.txt", b"abc", option="a", record_id="rec1", table={"label": "T"}))

    def test_file_response(self):
        os.environ["BTW_TEST_KEY"] = "s3"
        r = self.call("file", headers={"X-Key": "${BTW_TEST_KEY}"}, form={"lang": "zh"})
        self.assertEqual(r.files[0][0], "结果.txt")
        hdr, body = _Srv.seen
        self.assertEqual(hdr["X-Key"], "s3")
        for part in (b'name="option"\r\n\r\na', b'name="record_id"\r\n\r\nrec1', b'name="lang"\r\n\r\nzh', b'name="file"; filename="', b"abc"):
            self.assertIn(part, body)

    def test_json_zip_and_errors(self):
        r = self.call("json")
        self.assertEqual((r.files, r.fields, r.note), ([("r.txt", b"R")], {"姓名": "王五"}, "好了"))
        self.assertEqual(self.call("zip", unzip=True).files, [("a.txt", b"A"), ("b.txt", b"B")])
        with self.assertRaises(Busy):
            self.call("busy")
        with self.assertRaises(HandlerError) as c:
            self.call("oops")
        self.assertEqual(str(c.exception), "文件是空的")
        with self.assertRaises(Busy):
            http_call({"url": "http://127.0.0.1:9/x", "timeout": 2}, Job("a", b"x"))

    def test_disposition(self):
        self.assertEqual(_disposition_name('attachment; filename="a b.docx"'), "a b.docx")
        self.assertEqual(_disposition_name("attachment; filename=x.pdf"), "x.pdf")
        self.assertEqual(_disposition_name(""), "")


def make_docx(*paras):
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paras)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>%s</w:body></w:document>" % body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class ExamplesTest(unittest.TestCase):
    def test_python_handler(self):
        from examples.handler_docx_text import handle
        r = handle(Job("报告.docx", make_docx("第一段", "第二段 abc"), option="stats"))
        self.assertEqual(r.files[0][0], "报告.txt")
        text_ = r.files[0][1].decode("utf-8")
        self.assertIn("第一段\n第二段 abc", text_)
        self.assertIn("字数(不含空白): 9", text_)
        self.assertEqual(r.fields, {"字数": 9})
        with self.assertRaises(HandlerError):
            handle(Job("x.docx", b"not a zip"))
        with self.assertRaises(HandlerError):
            handle(Job("x.pdf", b""))

    def test_http_server_roundtrip(self):
        from examples.http_handler_server import H
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/convert" % srv.server_address[1]
            r = http_call({"url": url}, Job("报告.docx", make_docx("你好"), option="text"))
            self.assertEqual(r.files, [("报告.txt", "你好".encode("utf-8"))])
            with self.assertRaises(HandlerError) as c:
                http_call({"url": url}, Job("坏的.docx", b"oops"))
            self.assertIn("不是一个有效的 Word 文档", str(c.exception))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_example_configs_load(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("config.example.json", "config.full.json"):
            with open(os.path.join(root, "examples", name), encoding="utf-8") as fh:
                cfg = json.load(fh)
            ctx = Context(cfg, State(os.path.join(tempfile.mkdtemp(), "s.json")), FakeFeishu(), root)
            self.assertEqual(len(ctx.tables()), 1)
            for rule in cfg.get("rules") or []:
                rules.check(rule)
            if cfg.get("ledger"):
                self.assertTrue(ledger.columns(ctx))


class CliTest(unittest.TestCase):
    def test_help_and_missing_config(self):
        self.assertEqual(cli_main([]), 0)
        self.assertEqual(cli_main(["-c", "/nonexistent/config.json", "info"]), 2)

    def test_clean_errors(self):
        d = tempfile.mkdtemp()
        bad = os.path.join(d, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{oops")
        self.assertEqual(cli_main(["-c", bad, "info"]), 2)
        ok = os.path.join(d, "config.json")                 # 配了 env_file 但文件不存在、也没有凭证 → 给出明确提示而不是崩溃
        with open(ok, "w") as fh:
            json.dump({"env_file": ".env", "handlers": {}, "tables": []}, fh)
        saved = {k: os.environ.pop(k, None) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")}
        try:
            self.assertEqual(cli_main(["-c", ok, "info"]), 2)
        finally:
            os.environ.update({k: v for k, v in saved.items() if v})


if __name__ == "__main__":
    unittest.main()
