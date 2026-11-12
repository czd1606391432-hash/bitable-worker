"""示例处理器(python 类型):把 Word 文档转成纯文本。只用标准库,可以直接跑。

配置:  "handlers": {"default": {"type": "python", "target": "examples.handler_docx_text:handle"}}
"""
import io
import re
import zipfile
from xml.etree import ElementTree

from bitable_worker.handlers import HandlerError, Result

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data):
    try:
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise HandlerError("这不是一个有效的 Word 文档(.docx)")
    paras = []
    for p in ElementTree.fromstring(xml).iter(W + "p"):
        paras.append("".join(t.text or "" for t in p.iter(W + "t")))
    return "\n".join(paras).strip()


def handle(job):
    """job.name 文件名 / job.data 文件内容 / job.option 使用者在选项列里选的值(经 options 映射后)。"""
    if not job.name.lower().endswith(".docx"):
        raise HandlerError("只支持 .docx")
    body = docx_text(job.data)
    if not body:
        raise HandlerError("文档里没有文字")
    chars = len(re.sub(r"\s", "", body))
    if job.option == "stats":
        body += "\n\n----\n段落数: %d\n字数(不含空白): %d\n" % (body.count("\n") + 1, chars)
    stem = job.name.rsplit(".", 1)[0]
    return Result(files=[(stem + ".txt", body.encode("utf-8"))],
                  fields={"字数": chars},              # 表里有「字数」这一列且为空时会顺带填上;没有这一列就忽略
                  note="已转成文本，共 %d 字。" % chars)
