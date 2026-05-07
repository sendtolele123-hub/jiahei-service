"""GH Actions 触发：拉收件箱、找未处理 EPUB 邮件、加黑、回信。"""
import os, sys, re, base64, tempfile, urllib.request, urllib.error, urllib.parse, json, traceback
from pathlib import Path
from epub_bold import process_epub


def _q(s: str) -> str:
    """URL-encode 一个 path segment（message_id 含 <>@ 等特殊字符）。"""
    return urllib.parse.quote(s, safe="")

AGENTMAIL_KEY = os.environ["AGENTMAIL_API_KEY"]
INBOX = os.environ.get("JIAHEI_INBOX", "jiahei@agentmail.to")

LABEL_PROCESSED = "jiahei-processed"
LABEL_FAILED    = "jiahei-failed"
DEFAULT_RATIO   = 0.30
MAX_EPUB_BYTES  = 25 * 1024 * 1024  # 25 MB 上限


def api(method: str, path: str, body=None, raw=False):
    url = f"https://api.agentmail.to{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {AGENTMAIL_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AgentMail API {method} {path} HTTP {e.code}: {body}")
    if raw:
        return content
    return json.loads(content) if content else {}


def list_unprocessed():
    """返回未打 processed/failed label 的『收到』消息列表（排除自己 sent 的）。"""
    resp = api("GET", f"/v0/inboxes/{_q(INBOX)}/messages?limit=20")
    msgs = resp.get("messages", [])
    out = []
    for m in msgs:
        labels = set(m.get("labels", []))
        if "received" not in labels:
            continue  # 排除自己发出的回信
        if LABEL_PROCESSED in labels or LABEL_FAILED in labels:
            continue
        out.append(m)
    return out


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    meta = api("GET", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}/attachments/{_q(attachment_id)}")
    download_url = meta.get("download_url") or meta.get("url")
    if not download_url:
        raise RuntimeError(f"no download_url in attachment meta: {meta}")
    with urllib.request.urlopen(download_url, timeout=120) as r:
        return r.read()


def reply(message_id: str, text: str, attachments=None, add_labels=None):
    body = {"text": text}
    if attachments:
        body["attachments"] = attachments
    if add_labels:
        body["labels"] = add_labels
    return api("POST", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}/reply", body)


def add_label(message_id: str, label: str):
    return api("PATCH", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}", {"add_labels": label})


def parse_ratio(subject: str) -> float:
    m = re.search(r'\[ratio=([\d.]+)\]', subject or "")
    if not m:
        return DEFAULT_RATIO
    try:
        v = float(m.group(1))
        if v > 1:  # 用户写 30 而不是 0.3
            v /= 100
        return max(0.05, min(v, 0.6))
    except ValueError:
        return DEFAULT_RATIO


HELP_TEXT = """你好！我是加黑机器人 jiahei@agentmail.to。

用法：
- 给我发一封邮件，附带一个 .epub 文件
- 我会用 AI 给文中的核心词汇加粗，再把加粗后的 EPUB 发回给你
- 主题里加 [ratio=0.4] 可指定加黑比例（默认 0.3）

注意：
- 目前只支持中文 EPUB
- 单个文件 25 MB 以内
- 处理时间一般 1-10 分钟，请耐心等候

由 MiMo v2.5 + GitHub Actions 驱动。
"""


def handle(msg: dict):
    msg_id  = msg["message_id"]
    subject = msg.get("subject") or ""
    sender  = msg.get("from", "<unknown>")
    print(f"  subject={subject!r} from={sender}")

    epub_atts = [a for a in msg.get("attachments", [])
                 if a.get("filename", "").lower().endswith(".epub")]
    if not epub_atts:
        reply(msg_id, HELP_TEXT)
        add_label(msg_id, LABEL_PROCESSED)
        return

    att = epub_atts[0]
    if att.get("size", 0) > MAX_EPUB_BYTES:
        reply(msg_id, f"文件过大（{att.get('size')} 字节，上限 {MAX_EPUB_BYTES}）。")
        add_label(msg_id, LABEL_FAILED)
        return

    print(f"  downloading attachment {att['attachment_id']} ({att.get('size','?')} bytes)")
    epub_bytes = download_attachment(msg_id, att["attachment_id"])

    ratio = parse_ratio(subject)
    print(f"  target ratio = {ratio}")

    with tempfile.TemporaryDirectory() as td:
        in_p  = Path(td) / "in.epub"
        out_p = Path(td) / "out.epub"
        in_p.write_bytes(epub_bytes)
        result = process_epub(str(in_p), str(out_p), ratio)
        out_b64 = base64.b64encode(out_p.read_bytes()).decode()

    reply_text = (
        f"加黑完成 ✅\n"
        f"- 段落数：{result['para_count']}（失败 {result['fail_count']}）\n"
        f"- 原文字数：{result['total_chars']}\n"
        f"- 加黑字数：{result['bold_chars']}（{result['ratio_pct']}%）\n"
        f"- 目标比例：{int(ratio*100)}%\n\n"
        f"附件即处理后的 EPUB。"
    )
    out_filename = att["filename"].rsplit(".", 1)[0] + "_bolded.epub"
    reply(msg_id, reply_text, attachments=[{
        "filename": out_filename,
        "content_type": "application/epub+zip",
        "content": out_b64,
    }])
    add_label(msg_id, LABEL_PROCESSED)
    print(f"  ✅ replied to {sender}")


def main():
    msgs = list_unprocessed()
    print(f"unprocessed: {len(msgs)}")
    for msg in msgs:
        msg_id = msg.get("message_id", "?")
        try:
            print(f"\n=== handling {msg_id} ===")
            handle(msg)
        except Exception:
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            try:
                reply(msg_id, f"处理失败 ❌\n\n```\n{tb[-1500:]}\n```")
            except Exception:
                pass
            try:
                add_label(msg_id, LABEL_FAILED)
            except Exception:
                pass


if __name__ == "__main__":
    main()
