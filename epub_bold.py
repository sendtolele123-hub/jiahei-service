"""EPUB 加黑流水线：读 EPUB → 逐 <p> 调 MiMo 加黑 → 写新 EPUB。

可作为模块导入：from epub_bold import process_epub
也可命令行运行：python3 epub_bold.py <input.epub> <output.epub> [ratio]
"""
import json, os, sys, re, time
import urllib.request
from bs4 import BeautifulSoup
from ebooklib import epub
import ebooklib

API_URL = os.environ.get("MIMO_API_URL", "https://token-plan-cn.xiaomimimo.com/v1/chat/completions")
API_KEY = os.environ.get("MIMO_API_KEY", "")
MODEL   = os.environ.get("MIMO_MODEL", "mimo-v2.5")

PROMPT_TPL = """你是阅读辅助工具，给中文文本加粗"少量重点词汇"帮助快速扫读。

【硬性规则】
- 原文共 {total} 字。加粗字符数（不含 <b></b> 标签）必须落在 {low}~{high} 字之间
- **宁少勿多**：超过上限算失败
- 每个 <b>...</b> 内最多 4 个汉字（**严禁**整段、整句、长短语加粗）
- 只加粗：核心名词/术语/专有概念；不加粗：虚词、连词、动词后缀、修饰短语、整段定语

【输出格式】
- 直接输出加粗后的原文，不要任何说明、引号、前后缀
- 不许改字、不许加字、不许换行（保持原文一字不差，只插入 <b> 标签）

【正例】
原文："大语言模型的训练分为预训练和微调两个阶段"
正确：大<b>语言模型</b>的训练分为<b>预训练</b>和<b>微调</b>两个阶段

原文：
{text}"""


def bold_text(text: str, target_ratio: float = 0.30, retries: int = 2) -> str:
    """调 MiMo 给一段中文加黑，目标比例 target_ratio（默认 30%）。失败返回原文。"""
    if not API_KEY:
        raise RuntimeError("MIMO_API_KEY env var not set")
    text = text.strip()
    if len(text) < 8:
        return text
    total = len(text)
    low  = int(total * (target_ratio - 0.02))
    high = int(total * (target_ratio + 0.05))
    prompt = PROMPT_TPL.format(total=total, low=low, high=high, text=text)

    body = json.dumps({
        "model": MODEL,
        "messages": [{"role":"user","content":prompt}],
        "max_tokens": max(800, total * 3 + 200),
        "temperature": 0.3,
        "thinking": {"type":"disabled"},
    }).encode("utf-8")

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=body, headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"].strip()
            if not content:
                last_err = "empty content"
                continue
            stripped = re.sub(r'</?b>', '', content)
            if stripped != text:
                last_err = f"text mismatch (orig {len(text)} vs stripped {len(stripped)})"
                continue
            return content
        except Exception as e:
            last_err = repr(e)
            time.sleep(1)
    print(f"  [warn] bold failed after {retries+1} tries: {last_err}", file=sys.stderr)
    return text


def stats(html: str) -> tuple[int, int]:
    soup = BeautifulSoup(html, "lxml")
    plain = soup.get_text()
    bold_chars = sum(len(b.get_text()) for b in soup.find_all("b"))
    return len(plain), bold_chars


def process_epub(in_path: str, out_path: str, target_ratio: float = 0.30) -> dict:
    """处理 EPUB 文件，返回统计信息字典。"""
    book = epub.read_epub(in_path)
    total_orig = total_bold = 0
    para_count = 0
    fail_count = 0

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        raw = item.get_content().decode("utf-8")
        xml_decl = ""
        m = re.match(r'(<\?xml[^?]*\?>\s*)', raw)
        if m:
            xml_decl = m.group(1)
        soup = BeautifulSoup(raw, "html.parser")
        for p in soup.find_all("p"):
            orig_text = p.get_text()
            if len(orig_text.strip()) < 8:
                continue
            para_count += 1
            print(f"  para {para_count} ({len(orig_text)} chars)... ", end="", flush=True)
            bolded = bold_text(orig_text, target_ratio)
            if bolded == orig_text and len(orig_text) >= 8:
                fail_count += 1
            o, b = stats(f"<p>{bolded}</p>")
            total_orig += o
            total_bold += b
            ratio = b / o * 100 if o else 0
            print(f"{ratio:.1f}%")
            new_p = BeautifulSoup(f"<p>{bolded}</p>", "html.parser").p
            p.replace_with(new_p)
        out_html = xml_decl + str(soup)
        item.set_content(out_html.encode("utf-8"))

    epub.write_epub(out_path, book, {})
    overall = total_bold / total_orig * 100 if total_orig else 0
    print(f"\n=== Done ===")
    print(f"段落数: {para_count} (失败 {fail_count})")
    print(f"原文字数: {total_orig}, 加黑字数: {total_bold}, 总比例: {overall:.1f}%")
    print(f"输出: {out_path}")
    return {
        "para_count": para_count,
        "fail_count": fail_count,
        "total_chars": total_orig,
        "bold_chars": total_bold,
        "ratio_pct": round(overall, 1),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python3 epub_bold.py <input.epub> <output.epub> [ratio=0.30]", file=sys.stderr)
        sys.exit(1)
    in_p, out_p = sys.argv[1], sys.argv[2]
    ratio = float(sys.argv[3]) if len(sys.argv) > 3 else 0.30
    process_epub(in_p, out_p, ratio)
