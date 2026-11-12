# 飞书后台配置

## 1. 建应用

1. 打开[飞书开放平台](https://open.feishu.cn/app)，「创建企业自建应用」。
2. 在「凭证与基础信息」里拿到 **App ID** 和 **App Secret**，写进 `.env`。
3. 在「权限管理」里开通下面两类权限：

| 权限 | 用来做什么 |
|---|---|
| 查看、评论、编辑和管理多维表格（`bitable:app`） | 读写记录、加列、建表、复制多维表格、开关自动化 |
| 查看、评论、编辑和管理云空间中所有文件（`drive:drive`） | 下载和上传附件、转移所有者、设置链接分享 |

权限名称以开放平台当前页面为准。如果某个接口报权限不足，到该接口的文档页看它要求哪一项权限，补上即可。

4. 「版本管理与发布」里创建版本并发布。企业自建应用需要管理员审核通过后权限才生效。

## 2. 让应用能访问表格

应用默认看不到任何表格，有两种情况：

- **已有的表格**：打开多维表格 → 右上角「…」→「更多」→「添加文档应用」→ 搜索你的应用 → 权限选「可管理」。不做这一步，所有接口都会报 `91403`。
- **程序自己建或复制出来的表格**：应用天然是所有者，不需要额外操作。但人是看不到这些表的，所以程序会把所有者转给指定的人（应用保留管理权限）。

## 3. 找到 app_token 和 table_id

打开表格，看浏览器地址栏：

```text
https://xxx.feishu.cn/base/AbCdEfGhIjKlMnOp?table=tblXXXXXXXXXXXXXX&view=vewYYYYYY
```

- `app_token`：`/base/` 后面那一段（`AbCdEfGhIjKlMnOp`）
- `table_id`：`table=` 后面那一段（`tblXXXXXXXXXXXXXX`）

放在知识库里的多维表格，链接是 `/wiki/` 开头的，那一段不是 `app_token`，需要先用知识库接口换出真正的 token。

## 4. 给表格补列

程序要用到「状态」「结果」「说明」三列，有选项的话再加一个单选列。可以手动建，也可以让程序建：

```bash
python3 -m bitable_worker init-table <app_token> <table_id> --input 文件 --option 版本 --choices 标准版,精简版
```

已经存在的列不会动。列名可以用 `--status`、`--output`、`--note` 指定。

手动建的话，注意三点：

- 状态列是**单选**，选项名必须和配置里 `labels.status` 的四个值完全一致（默认是 待处理 / 处理中 / 已完成 / 失败）。
- 结果列是**附件**类型，说明列是**文本**类型。
- 选项列**不要设默认值**。否则每新增一行都会立刻触发处理，没传文件的行会被标成「失败」。

## 5. 常驻运行

把代码放到服务器上（下面以 `/opt/bitable-worker` 为例），`.env` 和 `config.json` 放在同一目录：

```bash
cp examples/bitable-worker.service /etc/systemd/system/
```

```bash
systemctl daemon-reload && systemctl enable --now bitable-worker
```

```bash
tail -f /var/log/bitable-worker.log
```

改了 `config.json` 之后重启服务才生效：

```bash
systemctl restart bitable-worker
```

`.env` 里有应用密钥，记得把权限收紧：

```bash
chmod 600 /opt/bitable-worker/.env
```
