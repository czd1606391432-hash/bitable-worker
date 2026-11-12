# 处理器

处理器决定「拿到文件之后做什么」。一行里有几个文件，处理器就被调用几次，每次处理一个文件。

## Python 处理器

配置：

```json
"handlers": {"default": {"type": "python", "target": "my_handlers:handle"}}
```

`target` 是 `模块名:函数名`。模块从配置文件所在的目录导入，所以把 `my_handlers.py` 放在 `config.json` 旁边就行。

```python
from bitable_worker.handlers import Busy, HandlerError, Result

def handle(job):
    ...
    return Result(files=[("结果.docx", data)], fields={"姓名": "张三"}, note="已生成，请复核。")
```

`job` 里有什么：

| 属性 | 内容 |
|---|---|
| `job.name` | 文件名 |
| `job.data` | 文件内容（bytes） |
| `job.option` | 使用者选的选项。配置了 `options` 映射时是映射后的值，没有选项列时是 `None` |
| `job.option_label` | 选项列里显示的文字 |
| `job.record_id` | 这一行的记录 ID |
| `job.row` | 这一行的全部字段，接口返回的原始格式 |
| `job.table` | `{"label", "app_token", "table_id"}` |

可以返回什么：

| 返回值 | 效果 |
|---|---|
| `Result(files, fields, note)` | 见下 |
| `bytes` | 当成一个输出文件，文件名自动取 |
| `(文件名, bytes)` | 一个输出文件 |
| 上面几种的列表 | 多个输出文件 |

`Result` 的三个部分：

- `files`：`[(文件名, bytes), ...]`，写进结果列。
- `fields`：`{列名: 值}`，顺带回填这一行的其它列。只填**表里存在、当前为空**的列，不会覆盖人已经写的内容；列不存在就忽略。
- `note`：写进说明列。不给就用默认的「已完成。」

出错时抛什么：

| 抛出 | 效果 |
|---|---|
| `HandlerError("原因")` | 这个文件失败，「原因」原样写进说明列给使用者看。用它来报「文件格式不对」「内容看不懂」这类使用者能解决的问题。 |
| `Busy("...")` | 这一行放回队列，状态回到「待处理」，下一轮自动重试。用它来报「后端忙」「被限流」。 |
| 其它异常 | 这个文件失败，说明列只写「处理失败」，详细信息记在日志里。不会影响别的行和别的表。 |

## HTTP 处理器

配置：

```json
"handlers": {
  "default": {
    "type": "http",
    "url": "http://127.0.0.1:8080/convert",
    "timeout": 300,
    "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
    "form": {"lang": "zh"},
    "file_field": "file",
    "option_field": "option",
    "unzip": false
  }
}
```

只有 `type` 和 `url` 是必填的。`headers` 和 `form` 的值里可以用 `${环境变量名}`，这样密钥不用写进配置文件。

### 请求

程序向 `url` 发一个 `multipart/form-data` 的 POST：

| 表单字段 | 内容 |
|---|---|
| `file` | 文件（字段名可用 `file_field` 改） |
| `option` | 使用者选的选项（字段名可用 `option_field` 改） |
| `record_id` | 记录 ID |
| `table` | 这张表在配置里的 `label` |
| `form` 里配的其它字段 | 原样带上 |

### 响应

**成功，返回一个文件**：状态码 2xx，响应体就是文件内容。文件名取自 `Content-Disposition`，推荐用 `filename*=UTF-8''<URL 编码的文件名>` 这种写法，中文不会乱码。没给文件名的话，按原文件名加 `-output` 自动取。

**成功，返回多个文件**：两种办法。

- 把文件打成一个 zip 返回，并在配置里写 `"unzip": true`，zip 里的每个文件都会写进结果列。
- 返回 JSON（见下）。

**成功，返回 JSON**：`Content-Type: application/json`，可以同时回填其它列、自定义说明：

```json
{
  "files": [{"name": "结果.docx", "content_base64": "UEsDB..."}],
  "fields": {"姓名": "张三"},
  "note": "已生成，请复核。"
}
```

**失败**：

| 响应 | 效果 |
|---|---|
| 状态码 4xx 或 5xx，响应体是 `{"error": "原因"}` | 这个文件失败，「原因」写进说明列 |
| 状态码 2xx，JSON 里有 `error` | 同上 |
| 状态码 429 / 502 / 503 / 504，或者连不上 | 当作后端忙，这一行放回队列，下一轮自动重试 |

可运行的例子：[examples/http_handler_server.py](../examples/http_handler_server.py)。

## 用多个处理器

`handlers` 里可以定义多个，每张表用 `handler` 指定：

```json
"handlers": {
  "to_text": {"type": "python", "target": "my_handlers:to_text"},
  "to_pdf": {"type": "http", "url": "http://127.0.0.1:8080/pdf"}
},
"tables": [
  {"label": "A 表", "handler": "to_text", "...": "..."},
  {"label": "B 表", "handler": "to_pdf", "...": "..."}
]
```
