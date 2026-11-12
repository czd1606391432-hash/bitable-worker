"""配置、状态文件、运行时上下文。

config.json  用户写的,程序只读。
state.json   程序写的(分发出去的表、解析好的字段 ID、建好的登记表/总账),不要手改。
"""
import copy
import json
import os
import sys
import time
from pathlib import Path

from .feishu import FeishuError

NAMES_TTL_S = 600                 # 列名缓存多久(列按字段 ID 识别,列名只是调接口时要用)
REQUIRED_ROLES = ("input", "status", "output", "note")
OPTIONAL_ROLES = ("option", "done_at")
TRIGGERS = ("input", "option", "manual")

# 表里出现的所有文字。想改措辞或换成别的语言,在 config.json 的 "labels" 里覆盖对应的键即可。
LABELS = {
    "status": {"wait": "待处理", "run": "处理中", "ok": "已完成", "fail": "失败"},
    "note": {
        "running": "正在处理…",
        "ok": "已完成。",
        "retry": "把状态改回「{wait}」可重试。",
        "no_input": "这一行还没有上传文件。传好后",
        "no_option": "请先在「{option}」列选好，选好后自动开始。",
        "no_option_retry": "请先在「{option}」列选好。选好后",
        "queued": "排队中，稍后自动重试…",
        "too_many": "一行最多处理 {n} 份文件，其余请另起一行",
        "too_big": "文件超过 {mb}MB",
        "bad_ext": "只支持这些格式：{exts}",
        "failed": "处理失败",
        "partial": "（未成功：{errs}）",
    },
    "registry": {
        "table": "分发登记", "view": "全部",
        "name": "新表名称", "owner": "交给谁", "status": "状态", "link": "新表链接", "note": "说明",
        "creator": "登记人", "created": "登记时间",
        "wait": "待分发", "run": "分发中", "ok": "已分发", "fail": "失败",
        "need_both": "「{name}」和「{owner}」都要填。填好后把状态改回「{wait}」。",
        "delivered": "已交付，对方会收到飞书通知。",
        "rows_left": "母版里的示例行没清干净，请手动删除",
        "flows_off": "这些自动化没能自动开启，请在新表里手动打开：{titles}",
        "flows_unknown": "自动化的开关没能自动恢复，请在新表里检查",
        "share_failed": "链接分享没能自动设置，请在表格右上角「分享」里手动设置",
        "owner_failed": "（所有者没转成，已改为加成可管理协作者：{err}）",
        "not_wired": "表已复制并交付（{url}），但文件处理功能没接上：{err}",
    },
    "ledger": {
        "table": "总账", "view": "全部",
        "link": "来源表格", "owner": "表格负责人", "sync": "同步状态", "note": "同步说明",
        "at": "数据更新时间", "key": "来源编号",
        "ok": "正常", "empty": "未填写", "gone": "表格不可用",
        "gone_note": "表格已删除，或程序没有权限。不再需要的话，删掉登记表里对应的那一行。",
        "empty_note": "这张表的「{table}」还没有填。",
    },
}


class ConfigError(Exception):
    pass


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def tkey(t):
    return "%s:%s" % (t["app_token"], t["table_id"])


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"tables": [], "ids": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(self.path))


class Context:
    def __init__(self, cfg, state, fs, base_dir="."):
        self.cfg, self.state, self.fs, self.base_dir = cfg, state, fs, str(base_dir)
        self.L = _merge(LABELS, cfg.get("labels"))
        self.poll_s = int(cfg.get("poll_seconds") or 20)
        lim = cfg.get("limits") or {}
        self.max_files_per_row = int(lim.get("files_per_row") or 5)
        self.max_file_bytes = int(lim.get("file_mb") or 20) * 1024 * 1024
        self.daily_cap = int(lim.get("per_day") or 2000)              # 一天最多处理多少份(防误操作)
        self.max_per_round = int(lim.get("per_table_per_round") or 3)  # 一张表一轮最多处理几行,然后轮到下一张
        self.cache = {}        # 表 → 字段缓存
        self.rt = {}           # 表 → 运行时计数(连续失败次数等)
        self.count = {"day": "", "n": 0}
        self.timers = {}
        self.memo = {}
        self._handlers = {}
        if self.base_dir not in sys.path:
            sys.path.insert(0, self.base_dir)      # 让 python 类型的处理器能从配置文件所在目录导入
        self._validate()

    def _validate(self):
        for t in self.cfg.get("tables") or []:
            for k in ("app_token", "table_id", "fields"):
                if not t.get(k):
                    raise ConfigError("tables 里有一项缺少 %s" % k)
            miss = [r for r in REQUIRED_ROLES if not t["fields"].get(r)]
            if miss:
                raise ConfigError("表「%s」的 fields 缺少: %s" % (t.get("label") or t["table_id"], ",".join(miss)))
            trig = self.trigger(t)
            if trig not in TRIGGERS:
                raise ConfigError("trigger 只能是 input / option / manual")
            if trig == "option" and not t["fields"].get("option"):
                raise ConfigError("表「%s」trigger=option,但 fields 里没有 option 列" % (t.get("label") or t["table_id"]))
            name = t.get("handler") or "default"
            if name not in (self.cfg.get("handlers") or {}):
                raise ConfigError("表「%s」用的处理器「%s」没有在 handlers 里定义" % (t.get("label") or t["table_id"], name))

    @staticmethod
    def trigger(t):
        """默认取最保守的:有选项列就等人选了才处理;没有选项列就只处理手动标成「待处理」的行。"""
        return t.get("trigger") or ("option" if (t.get("fields") or {}).get("option") else "manual")

    def tables(self):
        """所有要处理的表:配置里写的 + 程序分发出去的。"""
        return list(self.cfg.get("tables") or []) + list(self.state.data.get("tables") or [])

    def registry(self):
        """分发登记表 {"app_token", "table_id", "url"}:配置里写了就用配置的,否则用 setup-registry 建好后记在状态文件里的。"""
        return self.cfg.get("registry") or self.state.data.get("registry")

    def ledger_table(self):
        """总账所在的表(和登记表在同一个多维表格里)。"""
        reg, led = self.registry(), self.cfg.get("ledger") or {}
        tid = led.get("table_id") or (self.state.data.get("ledger") or {}).get("table_id")
        return {"app_token": reg["app_token"], "table_id": tid} if reg and tid else None

    def handler(self, t):
        from .handlers import load
        name = t.get("handler") or "default"
        if name not in self._handlers:
            spec = (self.cfg.get("handlers") or {}).get(name)
            if not spec:
                raise ConfigError("处理器「%s」没有在 handlers 里定义" % name)
            self._handlers[name] = load(spec)
        return self._handlers[name]

    def roles(self, t, force=False):
        """返回 {"names": {角色: 此刻的列名}, "all": 全部列名, "by_name": {列名: 字段信息}}。

        配置里的列可以写列名或字段 ID。第一次解析成字段 ID 后记进状态文件,
        之后列改名、调顺序都不影响;删列或改单选项的名字会影响。
        """
        key = tkey(t)
        c = self.cache.get(key)
        if c and not force and time.time() - c["at"] < NAMES_TTL_S:
            return c
        items = self.fs.fields(t["app_token"], t["table_id"])
        by_id = {x["field_id"]: x for x in items}
        by_name = {x["field_name"]: x for x in items}
        spec = {r: v for r, v in t["fields"].items() if v}
        saved = self.state.data["ids"].get(key)
        if not saved or saved.get("spec") != spec:
            ids = {r: (v if v in by_id else (by_name.get(v) or {}).get("field_id")) for r, v in spec.items()}
            miss = ["%s=%s" % (r, spec[r]) for r in spec if not ids[r]]
            if miss:
                raise ConfigError("表「%s」里找不到这些列: %s" % (t.get("label") or key, ", ".join(miss)))
            saved = self.state.data["ids"][key] = {"spec": spec, "ids": ids}
            self.state.save()
        names = {r: (by_id.get(fid) or {}).get("field_name") for r, fid in saved["ids"].items()}
        miss = [r for r in names if not names[r]]
        if miss:
            raise FeishuError("表里缺少必需的列(可能被删了): %s" % ",".join(miss))
        c = self.cache[key] = {"at": time.time(), "names": names, "all": set(by_name), "by_name": by_name}
        return c

    def due(self, name, every_s, force=False):
        """定时器:距离上次执行够久了(或 force)就返回 True 并重新计时。"""
        if force or time.time() - self.timers.get(name, 0) >= every_s:
            self.timers[name] = time.time()
            return True
        return False


def load(config_path, fs=None):
    from .feishu import Feishu, load_env_file
    p = Path(config_path)
    if not p.exists():
        raise ConfigError("没有配置文件: %s(可以从 examples/config.example.json 抄一份)" % p)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError("配置文件不是合法的 JSON: %s" % e)
    env = (p.parent / cfg["env_file"]).resolve() if cfg.get("env_file") else None
    if env and env.exists():      # 没有这个文件就算了,凭证也可以直接放在环境变量里
        load_env_file(str(env))
    state = State(cfg.get("state_file") and (p.parent / cfg["state_file"]) or p.with_name(p.stem + ".state.json"))
    fs = fs or Feishu(domain=(cfg.get("feishu") or {}).get("domain"))
    return Context(cfg, state, fs, p.parent.resolve())
