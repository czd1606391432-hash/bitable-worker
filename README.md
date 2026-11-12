# bitable-worker

把飞书多维表格当成任务入口：使用者在表格里传文件、选一个选项，后台程序自动处理，把结果文件写回同一行。

不需要给使用者做新页面、开新账号，他们只用自己每天都在用的表格。程序只用 Python 标准库，一个进程、一个配置文件。

> Turn a Feishu/Lark Bitable into a job queue: users drop a file into a row, a worker processes it and writes the result back to the same row. Also ships template distribution, a roll-up ledger and stale-row rules. Pure Python standard library.

## 能做什么

| 功能 | 说明 |
|---|---|
| **任务表** | 一行就是一个任务。传文件 → 选选项 → 状态自动走「待处理 / 处理中 / 已完成 / 失败」→ 结果文件和说明写回这一行。想重做，把状态改回「待处理」。 |
| **处理器可插拔** | 具体怎么处理文件由你决定：写一个 Python 函数，或者把文件转发给你已有的 HTTP 服务。 |
| **母版分发** | 在一张「分发登记表」里填「新表名称 + 交给谁」，程序复制母版、把所有者转给对方、把副本自动接入处理，并写回链接。 |
| **总账** | 把每张分发出去的表里的汇总数据，定时合并到一张总表，一眼看全所有项目。 |
| **超期规则** | 某一列停在某个值超过 N 天的行，自动改成别的值（比如「待筛选」超过 7 天自动改「淘汰」）。 |

后三项是可选的，只用任务表也完全可以。

## 使用者看到的样子

1. 在表格里新增一行，把文件拖进附件列。
2. 在选项列选一项（比如「标准版」）。
3. 状态变成「处理中」，约半分钟后变成「已完成」，结果列里出现处理好的文件。
4. 如果失败，「说明」列会写明原因和怎么重试。

## 快速开始

需要 Python 3.8 以上，不需要安装任何第三方包。

**1. 在飞书开放平台建一个企业自建应用**，开通多维表格和云文档的权限，拿到 App ID 和 App Secret。详细步骤见 [docs/setup.md](docs/setup.md)。

**2. 让应用能访问你的表格**：打开多维表格 → 右上角「…」→「更多」→「添加文档应用」→ 选你的应用 → 权限给「可管理」。

**3. 下载代码，写好凭证**

```bash
git clone https://github.com/czd1606391432-hash/bitable-worker.git && cd bitable-worker
```

```bash
cp examples/env.example .env
```

把 `.env` 里的 App ID 和 App Secret 换成你自己的。

**4. 准备表格和配置**

你的表里要先有一个附件列（下面假设叫「文件」）。运行下面的命令，程序会补上它要用的几列，并打印一段可以直接粘进配置的内容：

```bash
cp examples/config.example.json config.json
```

```bash
python3 -m bitable_worker init-table <app_token> <table_id> --input 文件 --option 输出内容 --choices 仅正文,正文+字数统计
```

`app_token` 是表格链接里 `/base/` 后面那一段，`table_id` 是链接里 `table=` 后面那一段。把打印出来的 `app_token`、`table_id` 填进 `config.json`。

**5. 运行**

```bash
python3 -m bitable_worker info
```

```bash
python3 -m bitable_worker run
```

`info` 用来检查每张表的列是否都对得上。`run` 启动后，在表里传一个 `.docx`、选一个选项，就能看到示例处理器把它转成文本写回来。

## 处理器怎么写

最简单的写法是一个 Python 函数：

```python
from bitable_worker.handlers import HandlerError, Result

def handle(job):
    # job.name 文件名, job.data 文件内容(bytes), job.option 使用者选的选项
    if not job.name.endswith(".pdf"):
        raise HandlerError("只支持 PDF")          # 这句话会原样显示在表格的「说明」列
    out = do_something(job.data, job.option)
    return Result(files=[("结果.docx", out)], note="已生成，请复核。")
```

如果你已经有一个现成的服务（任何语言），用 `http` 类型的处理器把文件转发过去即可。两种写法的完整约定见 [docs/handlers.md](docs/handlers.md)，可运行的例子在 [examples/](examples/)。

## 配置

完整例子见 [examples/config.full.json](examples/config.full.json)。最常用的几项：

| 配置项 | 含义 |
|---|---|
| `tables[].fields` | 程序用到的列：`input` 待处理文件、`option` 选项（可不要）、`status` 状态、`output` 结果、`note` 说明、`done_at` 完成时间（可不要）。可以写列名，也可以写字段 ID。 |
| `tables[].trigger` | 什么时候开始处理，见下表。 |
| `tables[].options` | 选项列显示的文字 → 传给处理器的值。配了之后，选项不在这里面的行不会处理。 |
| `tables[].accept` | 只接受哪些扩展名，例如 `[".pdf", ".docx"]`。 |
| `handlers` | 处理器，可以定义多个，每张表用 `handler` 指定用哪个。 |
| `limits` | 每行最多几个文件、单个文件上限、每天上限、每张表每轮最多处理几行。 |
| `labels` | 表里出现的所有文字（状态名、提示语）都可以在这里改。 |

`trigger` 的三种取值：

| 取值 | 什么时候处理 | 适合 |
|---|---|---|
| `option` | 选项列选了值、且状态为空 | 已有的业务表。大多数行不需要处理，只有主动选了选项的才处理。配了选项列时的默认值。 |
| `manual` | 只处理状态被改成「待处理」的行 | 没有选项列的业务表。没配选项列时的默认值。 |
| `input` | 传了文件、状态为空就处理 | 专门用来处理文件的新表。**不要用在已有很多附件的老表上**，否则会把历史行全部处理一遍。 |

不管哪种取值，把状态改回「待处理」都会重做这一行。

列是按字段 ID 识别的：第一次运行时把配置里的列名解析成字段 ID 并记下来，之后表里的列改名、调顺序都不影响。删列或者改单选项的名字会影响。

程序会在配置文件旁边生成一个 `config.state.json`，记录分发出去的表和解析好的字段 ID。这个文件由程序维护，不要手改，要和配置文件一起备份。

## 母版分发、总账、超期规则

三项功能的用法见 [docs/distribute-ledger-rules.md](docs/distribute-ledger-rules.md)。要点：

- 副本必须由程序复制。手动复制出来的表是全新的，程序既不认识也没有权限。
- 分发出去的表和母版从此各自独立，母版后续的改动不会同步到已分发的表。
- 超期规则会直接改数据，启用前先运行 `python3 -m bitable_worker rules-dry` 看会影响多少行。

## 部署

常驻运行推荐用 systemd，模板在 [examples/bitable-worker.service](examples/bitable-worker.service)。进程中断后重启即可，卡在「处理中」的行会在启动时自动放回队列。

## 命令

| 命令 | 作用 |
|---|---|
| `run` | 常驻运行 |
| `once` | 跑一轮就退出，联调用 |
| `info` | 查看接入了哪些表、每张表的列是否都对得上 |
| `init-table` | 给一张已有的表补上程序要用的列 |
| `setup-registry` | 建「分发登记表」，只做一次 |
| `setup-ledger` | 建「总账」，只做一次 |
| `rules-dry` | 只算不改，看每条超期规则会影响多少行 |

配置文件默认读当前目录的 `config.json`，也可以用 `-c` 指定。

## 已知局限

- **有延迟**。程序是定时去查表的（默认 20 秒一次），不是事件推送，所以从选好选项到开始处理最多要等一个周期。
- **一个进程串行处理**。一张表每轮最多处理 3 行就轮到下一张，免得一张表批量导入时堵住别的表；总吞吐取决于你的处理器有多快。
- **`trigger: input` 每轮最多看 500 行**状态为空的记录，所以只适合专用的小表。
- **飞书接口本身的限制**：列的顺序、看板视图的分组、仪表盘图表都没法用接口设置，需要在界面上手动调。
- 这个项目是从一套在线运行的程序里抽出来重构的，接口的调用方式沿用了线上验证过的写法。重构后的 v0.1.0 目前只跑过离线测试，还没有在真实租户里完整回归。
- 原程序只在飞书（中国版）上运行过，国际版 Lark 没有验证。

## 踩过的坑

把这套东西跑通的过程中，在飞书多维表格的接口上踩了不少坑，比如「接口复制出来的表自动化全是停用的」「只复制结构的话自动化根本不带过去」。都记在 [docs/pitfalls.md](docs/pitfalls.md) 里，就算不用这个项目，自己对接多维表格接口时也用得上。

## 测试

```bash
python3 -m unittest discover -s tests
```

测试用的是内存里的假飞书（[tests/fake_feishu.py](tests/fake_feishu.py)），不联网、不需要凭证。

## 许可

MIT
