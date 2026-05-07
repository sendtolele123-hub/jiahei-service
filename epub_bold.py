"""EPUB 加黑流水线：读 EPUB → 逐 <p> 调 MiMo 加黑 → 写新 EPUB。

可作为模块导入：from epub_bold import process_epub
也可命令行运行：python3 epub_bold.py <input.epub> <output.epub> [ratio] [concurrency]
"""
import json, os, sys, re, time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def collect_paragraphs(book) -> tuple[list, list[str]]:
    """扫一遍 book，收集所有 <p>。返回:
    items_data: [(item, soup, xml_decl, [(p_element, global_idx), ...]), ...]
    all_texts:  [orig_text_0, orig_text_1, ...]
    """
    items_data = []
    all_texts: list[str] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        raw = item.get_content().decode("utf-8")
        xml_decl = ""
        m = re.match(r'(<\?xml[^?]*\?>\s*)', raw)
        if m:
            xml_decl = m.group(1)
        soup = BeautifulSoup(raw, "html.parser")
        ps = []
        for p in soup.find_all("p"):
            orig = p.get_text()
            if len(orig.strip()) < 8:
                continue
            idx = len(all_texts)
            all_texts.append(orig)
            ps.append((p, idx))
        items_data.append((item, soup, xml_decl, ps))
    return items_data, all_texts


def process_epub(in_path: str, out_path: str, target_ratio: float = 0.30,
                 concurrency: int = 10, progress_cb=None) -> dict:
    """处理 EPUB 文件，并发调 LLM 加黑后重新打包。

    Args:
        in_path:       输入 EPUB
        out_path:      输出 EPUB
        target_ratio:  目标加黑比例（0.05~0.6）
        concurrency:   并发调 LLM 路数（默认 10）
        progress_cb:   可选回调 fn(done, total) 每完成一段调一次

    Returns 统计字典。
    """
    book = epub.read_epub(in_path)
    items_data, all_texts = collect_paragraphs(book)
    total_paras = len(all_texts)
    print(f"  total paragraphs: {total_paras}, concurrency={concurrency}")

    # 并发调 LLM
    results: list[str] = [""] * total_paras
    completed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(bold_text, t, target_ratio): i for i, t in enumerate(all_texts)}
        for f in as_completed(futures):
            i = futures[f]
            try:
                results[i] = f.result()
            except Exception as e:
                print(f"  [warn] para {i} exception: {e!r}", file=sys.stderr)
                results[i] = all_texts[i]  # fallback to orig
            completed += 1
            if progress_cb:
                try: progress_cb(completed, total_paras)
                except Exception: pass
            if completed % 25 == 0 or completed == total_paras:
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total_paras - completed) / rate if rate > 0 else 0
                print(f"  progress: {completed}/{total_paras} "
                      f"({100*completed/total_paras:.0f}%) "
                      f"rate={rate:.1f}/s eta={eta:.0f}s")

    # 串行替换 + 重打包
    total_orig = total_bold = 0
    fail_count = 0
    for item, soup, xml_decl, ps in items_data:
        for p, idx in ps:
            bolded = results[idx]
            orig = all_texts[idx]
            if bolded == orig and len(orig) >= 8:
                fail_count += 1
            o, b = stats(f"<p>{bolded}</p>")
            total_orig += o
            total_bold += b
            new_p = BeautifulSoup(f"<p>{bolded}</p>", "html.parser").p
            p.replace_with(new_p)
        out_html = xml_decl + str(soup)
        item.set_content(out_html.encode("utf-8"))

    epub.write_epub(out_path, book, {})
    overall = total_bold / total_orig * 100 if total_orig else 0
    print(f"\n=== Done ===")
    print(f"段落数: {total_paras} (失败 {fail_count})")
    print(f"原文字数: {total_orig}, 加黑字数: {total_bold}, 总比例: {overall:.1f}%")
    print(f"耗时: {time.time()-t0:.1f}s, 输出: {out_path}")
    return {
        "para_count": total_paras,
        "fail_count": fail_count,
        "total_chars": total_orig,
        "bold_chars": total_bold,
        "ratio_pct": round(overall, 1),
        "elapsed_sec": round(time.time() - t0, 1),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python3 epub_bold.py <input.epub> <output.epub> [ratio=0.30] [concurrency=10]", file=sys.stderr)
        sys.exit(1)
    in_p, out_p = sys.argv[1], sys.argv[2]
    ratio = float(sys.argv[3]) if len(sys.argv) > 3 else 0.30
    conc  = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    process_epub(in_p, out_p, ratio, conc)
