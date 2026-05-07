"""Atomic tools for the jiahei agent.

设计要点：
- 大对象（blocks 列表、md 字符串等）存在 WORKSPACE 字典里，agent 只看 workspace_key
  这样 agent 上下文不会被 1017 个段落撑爆
- 每个工具返回**摘要**（counts/stats/preview）+ workspace_key 引用，agent 据此决策
- 工具之间通过 workspace_key 串起来
"""
from __future__ import annotations
import os, json, time, base64, tempfile, urllib.request, urllib.error, urllib.parse, re, secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from bs4 import BeautifulSoup
from ebooklib import epub
import ebooklib

# === Config ===
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
AGENTMAIL_API_KEY = os.environ.get("AGENTMAIL_API_KEY", "")
INBOX = os.environ.get("JIAHEI_INBOX", "jiahei@agentmail.to")
MIMO_URL = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-pro"   # pro 模型 + 默认 thinking 开启（加黑质量提升，速度变慢）

LABEL_PROCESSED = "jiahei-processed"
LABEL_FAILED    = "jiahei-failed"
LABEL_ACKED     = "jiahei-acked"

MAX_REPLY_BYTES = 4 * 1024 * 1024

# In-process workspace: agent 通过 key 引用大对象
WORKSPACE: dict[str, object] = {}


def _new_key(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# === HTTP helpers ===
def _agentmail(method: str, path: str, body=None):
    url = f"https://api.agentmail.to{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {AGENTMAIL_API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            content = r.read()
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"AgentMail API {method} {path} HTTP {e.code}: {body_txt}")
    return json.loads(content) if content else {}


# ============================================================
# Tool implementations
# ============================================================

def list_unprocessed_emails() -> dict:
    """列出 jiahei inbox 里所有未处理（无 jiahei-processed/jiahei-failed 标）的邮件。"""
    resp = _agentmail("GET", f"/v0/inboxes/{_q(INBOX)}/messages?limit=20&include_spam=true")
    msgs = resp.get("messages", [])
    out = []
    for m in msgs:
        labels = set(m.get("labels", []))
        if "received" not in labels:
            continue
        if LABEL_PROCESSED in labels or LABEL_FAILED in labels:
            continue
        out.append({
            "message_id": m["message_id"],
            "from": m.get("from", ""),
            "subject": m.get("subject", "") or "",
            "preview": (m.get("preview") or "")[:200],
            "already_acked": LABEL_ACKED in labels,
            "attachments": [
                {
                    "attachment_id": a["attachment_id"],
                    "filename": a.get("filename", ""),
                    "size": a.get("size", 0),
                    "content_type": a.get("content_type", ""),
                }
                for a in (m.get("attachments") or [])
            ],
        })
    return {"count": len(out), "emails": out}


def send_ack(message_id: str, body_text: str) -> dict:
    """回 ack 信 + 加 jiahei-acked label。"""
    _agentmail("POST", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}/reply",
               {"text": body_text})
    _agentmail("PATCH", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}",
               {"add_labels": LABEL_ACKED})
    return {"ok": True}


def send_reply_text(message_id: str, body_text: str) -> dict:
    """只回文字（用于 help / 错误）。"""
    _agentmail("POST", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}/reply",
               {"text": body_text})
    return {"ok": True}


def mark_label(message_id: str, label: str) -> dict:
    _agentmail("PATCH", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}",
               {"add_labels": label})
    return {"ok": True, "label": label}


def download_attachment(message_id: str, attachment_id: str) -> dict:
    """下载附件到 /tmp，临时文件**保留原始扩展名**（让后续 extract_text_blocks 按格式分支）。"""
    meta = _agentmail("GET",
        f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}/attachments/{_q(attachment_id)}")
    download_url = meta.get("download_url") or meta.get("url")
    if not download_url:
        raise RuntimeError("no download_url in attachment meta")
    with urllib.request.urlopen(download_url, timeout=300) as r:
        content = r.read()
    filename = meta.get("filename", "")
    ext = Path(filename).suffix.lower() or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="jiahei_", suffix=f"_{attachment_id[:8]}{ext}")
    os.write(fd, content)
    os.close(fd)
    return {
        "file_path": tmp_path,
        "size_bytes": len(content),
        "filename": filename,
        "content_type": meta.get("content_type", ""),
    }


def extract_text_blocks(file_path: str) -> dict:
    """解析文件 → blocks 数组存 workspace。

    支持 .epub / .txt / .md。
    返回：workspace_key + 摘要（不返回 blocks 本身，太大）+ 前几个 block 的 preview。
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(file_path)
    ext = path.suffix.lower()
    blocks: list[dict] = []

    if ext == ".epub":
        book = epub.read_epub(str(path))
        spine_ids = [iid for iid, _ in book.spine]
        items = []
        for sid in spine_ids:
            it = book.get_item_with_id(sid)
            if it and it.get_type() == ebooklib.ITEM_DOCUMENT:
                items.append(it)
        if not items:
            items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        for item in items:
            try:
                raw = item.get_content().decode("utf-8", errors="replace")
            except Exception:
                continue
            soup = BeautifulSoup(raw, "html.parser")
            body = soup.body or soup
            for el in body.find_all(["h1","h2","h3","h4","h5","h6","p"]):
                text = el.get_text(separator="", strip=True)
                if not text:
                    continue
                if el.name.startswith("h"):
                    blocks.append({"kind":"heading","level":int(el.name[1]),"text":text})
                else:
                    blocks.append({"kind":"para","level":0,"text":text})
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        for para in text.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            blocks.append({"kind":"para","level":0,"text":para})

    para_count = sum(1 for b in blocks if b["kind"] == "para")
    heading_count = sum(1 for b in blocks if b["kind"] == "heading")
    total_chars = sum(len(b["text"]) for b in blocks)

    key = _new_key("blocks")
    WORKSPACE[key] = blocks

    preview = []
    for b in blocks[:5]:
        preview.append({
            "kind": b["kind"],
            "level": b.get("level", 0),
            "text_preview": b["text"][:80],
        })
    return {
        "workspace_key": key,
        "blocks_count": len(blocks),
        "para_count": para_count,
        "heading_count": heading_count,
        "total_chars": total_chars,
        "format": ext.lstrip("."),
        "preview_first_5": preview,
    }


# === bold helpers ===
BATCH_SIZE = 20         # 一个 prompt 塞多少段（reasoning 摊薄）
BACKOFF = [1, 2, 4, 8, 16, 32]


def _build_batch_prompt(texts: list[str], target_ratio: float) -> str:
    parts = []
    for i, t in enumerate(texts):
        parts.append(f"<<PARA_{i}>>\n{t}")
    body_str = "\n\n".join(parts)
    return f"""你是阅读辅助工具。给下面 {len(texts)} 段中文文本加粗约 {int(target_ratio*100)}% 的核心词汇。

【规则】
- 加粗用 markdown 双星号包裹：`**词**`
- 只加粗核心名词、术语、专有概念；**不加粗**虚词、连词、动词后缀、修饰短语
- 每个加粗短语 2-4 个汉字（严禁整段、整句加粗）
- 比例约 {int(target_ratio*100)}%（宁少勿多）
- **不许改字、不许加字、不许换行**（原文一字不差，只插入 ** 标记）

【输出格式严格如下】（输入有 {len(texts)} 段，输出也必须有 {len(texts)} 段）
<<PARA_0>>
<加粗后的段落 0>
<<PARA_1>>
<加粗后的段落 1>
...
<<PARA_{len(texts)-1}>>
<加粗后的段落 {len(texts)-1}>

不要任何前后缀、说明、代码块标记。

输入：

{body_str}"""


def _parse_batch_output(content: str, n: int) -> list[str | None]:
    """从模型输出抽出 [bolded_0, bolded_1, ...]。失败的段返回 None。"""
    if content.startswith("```"):
        content = re.sub(r'^```\w*\n', '', content)
        content = re.sub(r'\n```\s*$', '', content)
    # 用 lookahead 切：每个 <<PARA_i>>...直到下一个 <<PARA_>> 或文末
    pattern = r'<<PARA_(\d+)>>\s*\n?(.*?)(?=<<PARA_\d+>>|\Z)'
    out: list[str | None] = [None] * n
    for m in re.finditer(pattern, content, re.DOTALL):
        idx = int(m.group(1))
        text = m.group(2).strip()
        if 0 <= idx < n and out[idx] is None:
            out[idx] = text
    return out


def _bold_batch(texts: list[str], target_ratio: float, max_retries: int = 3) -> list[str]:
    """一次 prompt 处理 N 段。返回与输入等长数组（失败的段回退原文）。"""
    if not texts:
        return []
    prompt = _build_batch_prompt(texts, target_ratio)
    body = json.dumps({
        "model": MIMO_MODEL,
        "messages": [{"role":"user","content":prompt}],
        "temperature": 0.3,
        "reasoning_effort": "low",
    }).encode()

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(MIMO_URL, data=body, headers={
                "Authorization": f"Bearer {MIMO_API_KEY}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
            content = (data["choices"][0]["message"].get("content") or "").strip()
            parsed = _parse_batch_output(content, len(texts))
            # 校验每段：去 ** 必须等于原文（trim 后），否则回退
            results = []
            for i, p in enumerate(parsed):
                orig = texts[i].strip()
                if p is None:
                    results.append(orig)
                    continue
                stripped = re.sub(r'\*\*', '', p).strip()
                if stripped == orig:
                    results.append(p)
                else:
                    # 部分匹配宽松：去标点空格再比
                    a = re.sub(r'\s+', '', stripped)
                    b = re.sub(r'\s+', '', orig)
                    if a == b:
                        results.append(p)
                    else:
                        results.append(orig)
            return results
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF)-1)])
                continue
        except Exception:
            pass
        time.sleep(BACKOFF[min(attempt, len(BACKOFF)-1)])
    # 全部失败回退原文
    return [t.strip() for t in texts]


def bold_workspace_paragraphs(workspace_key: str, target_ratio: float = 0.30,
                              concurrency: int = 5, batch_size: int = BATCH_SIZE) -> dict:
    """对 workspace 里 blocks 的所有 para 加黑：批量化（每 batch_size 段一个 prompt）+ 多 batch 并发。

    单段串行 reasoning 浪费严重（每段几千 tokens reasoning）。
    批量化让一次 reasoning 摊到 N 段，速度提升 N 倍。
    """
    blocks = WORKSPACE.get(workspace_key)
    if blocks is None:
        raise RuntimeError(f"workspace_key not found: {workspace_key}")
    target_ratio = float(target_ratio)
    if not (0.05 <= target_ratio <= 0.6):
        raise ValueError(f"target_ratio out of range: {target_ratio}")
    concurrency = max(1, min(int(concurrency), 10))
    batch_size = max(1, min(int(batch_size), 50))

    para_indices = [i for i, b in enumerate(blocks)
                    if b["kind"] == "para" and len(b["text"]) >= 8]
    if not para_indices:
        return {"workspace_key": workspace_key, "para_processed": 0,
                "fail_count": 0, "total_chars": 0, "bold_chars": 0,
                "ratio_pct": 0, "elapsed_sec": 0}

    # 按 batch_size 分批
    batches: list[list[int]] = []
    for s in range(0, len(para_indices), batch_size):
        batches.append(para_indices[s:s+batch_size])
    print(f"  bold: {len(para_indices)} paras → {len(batches)} batches "
          f"(batch_size={batch_size}, concurrency={concurrency})")

    completed_batches = 0
    fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        def run_batch(batch_idx: int, indices: list[int]) -> tuple[int, list[str]]:
            texts = [blocks[i]["text"] for i in indices]
            results = _bold_batch(texts, target_ratio)
            return batch_idx, indices, results
        futures = [ex.submit(run_batch, bi, idx_list)
                   for bi, idx_list in enumerate(batches)]
        for f in as_completed(futures):
            bi, indices, results = f.result()
            for idx, r in zip(indices, results):
                blocks[idx]["text"] = r
                if r.strip() == re.sub(r'\*\*','', r).strip():
                    # r 没 ** 标签 = 全失败回退
                    pass
            # 失败统计
            for idx, r in zip(indices, results):
                stripped = re.sub(r'\*\*', '', r)
                if stripped == r:  # 没加任何 **
                    fail += 1
            completed_batches += 1
            el = time.time() - t0
            rate_paras = sum(len(b) for b in batches[:completed_batches]) / el if el > 0 else 0
            done_paras = sum(len(b) for b in batches[:completed_batches])
            eta = (len(para_indices) - done_paras) / rate_paras if rate_paras > 0 else 0
            print(f"    batch {completed_batches}/{len(batches)} done. "
                  f"paras {done_paras}/{len(para_indices)} "
                  f"rate={rate_paras:.1f}/s eta={eta:.0f}s")

    total_orig = sum(len(re.sub(r'\*\*','',b["text"])) for b in blocks if b["kind"]=="para")
    total_bold = sum(sum(len(m) for m in re.findall(r'\*\*([^*]+)\*\*', b["text"]))
                     for b in blocks if b["kind"]=="para")
    return {
        "workspace_key": workspace_key,
        "para_processed": len(para_indices),
        "batches": len(batches),
        "fail_count": fail,
        "total_chars": total_orig,
        "bold_chars": total_bold,
        "ratio_pct": round(total_bold / total_orig * 100, 1) if total_orig else 0,
        "elapsed_sec": round(time.time() - t0, 1),
    }


def assemble_workspace_markdown(workspace_key: str) -> dict:
    """从 workspace 取 blocks → 拼成完整 md 字符串 → 存回 workspace 用新 key。"""
    blocks = WORKSPACE.get(workspace_key)
    if blocks is None:
        raise RuntimeError(f"workspace_key not found: {workspace_key}")
    lines = []
    for b in blocks:
        if b["kind"] == "heading":
            lvl = max(1, min(6, int(b.get("level", 2))))
            lines.append("#" * lvl + " " + b["text"])
        else:
            lines.append(b["text"])
        lines.append("")
    md = "\n".join(lines)
    md_key = _new_key("md")
    WORKSPACE[md_key] = md
    return {
        "markdown_workspace_key": md_key,
        "size_bytes": len(md.encode("utf-8")),
        "preview_head": md[:300],
        "preview_tail": md[-200:] if len(md) > 500 else "",
    }


def send_reply_with_workspace_markdown(message_id: str, body_text: str,
                                       markdown_workspace_key: str,
                                       filename: str) -> dict:
    """从 workspace 取 md 字符串 → base64 → reply。"""
    md = WORKSPACE.get(markdown_workspace_key)
    if md is None or not isinstance(md, str):
        raise RuntimeError(f"markdown_workspace_key not found or wrong type: {markdown_workspace_key}")
    md_bytes = md.encode("utf-8")
    if len(md_bytes) > MAX_REPLY_BYTES:
        raise RuntimeError(f"md too large to send inline: {len(md_bytes)} bytes (max {MAX_REPLY_BYTES})")
    if not filename.endswith((".md", ".txt")):
        filename += ".md"
    payload = {
        "text": body_text,
        "attachments": [{
            "filename": filename,
            "content_type": "text/markdown; charset=utf-8",
            "content": base64.b64encode(md_bytes).decode(),
        }],
    }
    _agentmail("POST", f"/v0/inboxes/{_q(INBOX)}/messages/{_q(message_id)}/reply",
               payload)
    return {"ok": True, "size_bytes": len(md_bytes)}


# ============================================================
# Tool schemas (OpenAI / MiMo function calling format)
# ============================================================

TOOL_SCHEMAS = [
    {"type":"function","function":{
        "name":"list_unprocessed_emails",
        "description":"列出 jiahei 收件箱里所有未处理（没有 jiahei-processed / jiahei-failed 标）的邮件。返回 emails 数组，每项含 message_id, from, subject, preview, already_acked, attachments[{attachment_id,filename,size}]。",
        "parameters":{"type":"object","properties":{}}
    }},
    {"type":"function","function":{
        "name":"send_ack",
        "description":"对指定邮件回一封 ack 信告知发件人「已收到、开始处理、预估耗时」，同时给原邮件加 jiahei-acked label。如果邮件已 acked 不要再调，直接处理即可。",
        "parameters":{"type":"object","properties":{
            "message_id":{"type":"string"},
            "body_text":{"type":"string"}
        },"required":["message_id","body_text"]}
    }},
    {"type":"function","function":{
        "name":"send_reply_text",
        "description":"只回纯文字邮件（无附件），用于 help text / 错误反馈 / 不支持格式说明。",
        "parameters":{"type":"object","properties":{
            "message_id":{"type":"string"},
            "body_text":{"type":"string"}
        },"required":["message_id","body_text"]}
    }},
    {"type":"function","function":{
        "name":"mark_label",
        "description":"给邮件加 label。完成处理后必须 mark 'jiahei-processed' 否则下次还会被 list；处理失败可 mark 'jiahei-failed'。",
        "parameters":{"type":"object","properties":{
            "message_id":{"type":"string"},
            "label":{"type":"string","enum":["jiahei-processed","jiahei-failed"]}
        },"required":["message_id","label"]}
    }},
    {"type":"function","function":{
        "name":"download_attachment",
        "description":"下载邮件附件到本地临时文件，返回 file_path / size_bytes / filename / content_type。",
        "parameters":{"type":"object","properties":{
            "message_id":{"type":"string"},
            "attachment_id":{"type":"string"}
        },"required":["message_id","attachment_id"]}
    }},
    {"type":"function","function":{
        "name":"extract_text_blocks",
        "description":"解析本地文件（.epub/.txt/.md）→ 抽出 blocks 数组按阅读顺序，**存到 workspace** 不直接返回内容（避免上下文爆炸）。返回 workspace_key + 段落数/标题数/总字数/格式/前 5 个 block 的 preview。后续 bold/assemble 工具用 workspace_key 引用即可。",
        "parameters":{"type":"object","properties":{
            "file_path":{"type":"string"}
        },"required":["file_path"]}
    }},
    {"type":"function","function":{
        "name":"bold_workspace_paragraphs",
        "description":"对 workspace 里 blocks 的所有 kind=='para' 段落**批量化加黑**（每 batch_size 段一次 prompt + concurrency 个 batch 并发），原地更新 blocks 的 text 字段。**这是处理一本书的核心步骤，调用一次即可处理所有段落，工具内部已批量+并发，不要循环调**。返回 stats（para_processed / batches / fail_count / ratio_pct / elapsed_sec）。",
        "parameters":{"type":"object","properties":{
            "workspace_key":{"type":"string","description":"extract_text_blocks 返回的 key"},
            "target_ratio":{"type":"number","default":0.30,"description":"目标加黑比例 0.05~0.6"},
            "concurrency":{"type":"integer","default":5,"description":"并发的 batch 数 1~10，推荐 5"},
            "batch_size":{"type":"integer","default":20,"description":"每个 prompt 塞多少段 1~50，推荐 20。增大可提速但 prompt 长度也变大。"}
        },"required":["workspace_key"]}
    }},
    {"type":"function","function":{
        "name":"assemble_workspace_markdown",
        "description":"从 workspace 里的 blocks 拼成完整 markdown 字符串（heading 用 #，段落空行分隔），存到 workspace 用新 key。返回 markdown_workspace_key + size_bytes + preview_head/tail。",
        "parameters":{"type":"object","properties":{
            "workspace_key":{"type":"string"}
        },"required":["workspace_key"]}
    }},
    {"type":"function","function":{
        "name":"send_reply_with_workspace_markdown",
        "description":"回信附 markdown 文件（从 workspace 取 md 字符串）。size_bytes 不能超 4MB（极少见超限）。filename 没 .md 后缀会自动加。",
        "parameters":{"type":"object","properties":{
            "message_id":{"type":"string"},
            "body_text":{"type":"string","description":"邮件正文（统计/说明）"},
            "markdown_workspace_key":{"type":"string"},
            "filename":{"type":"string"}
        },"required":["message_id","body_text","markdown_workspace_key","filename"]}
    }},
    {"type":"function","function":{
        "name":"done",
        "description":"所有未处理邮件都处理完毕，结束本次运行。每次启动 agent 都从头跑，所以 done 只是表示「现在没事干了」。",
        "parameters":{"type":"object","properties":{
            "summary":{"type":"string","description":"本次运行做了什么的简短摘要"}
        },"required":["summary"]}
    }},
]


TOOL_FUNCS = {
    "list_unprocessed_emails": list_unprocessed_emails,
    "send_ack": send_ack,
    "send_reply_text": send_reply_text,
    "mark_label": mark_label,
    "download_attachment": download_attachment,
    "extract_text_blocks": extract_text_blocks,
    "bold_workspace_paragraphs": bold_workspace_paragraphs,
    "assemble_workspace_markdown": assemble_workspace_markdown,
    "send_reply_with_workspace_markdown": send_reply_with_workspace_markdown,
    # 'done' 是 sentinel
}
