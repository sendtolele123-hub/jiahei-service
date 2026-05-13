"""Jiahei Agent: MiMo v2.5 driven email-to-bolded-markdown agent.

设计哲学：
- LLM 是决策者，程序是工具的提供者
- Agent 自己决定：理解邮件意图、选择处理方式、错误恢复、合理批处理
- 加新格式 = 在 tools.py 加新工具，不动 agent 代码

入口：python3 agent.py（GH Actions cron 调用）
"""
import os, sys, json, time, traceback, urllib.request, urllib.error
from tools import TOOL_SCHEMAS, TOOL_FUNCS, MIMO_API_KEY, MIMO_URL, MIMO_MODEL

# === System prompt ===
SYSTEM_PROMPT = """你是 jiahei 邮件加黑助手，跑在 GitHub Actions cron 里。每次启动时按以下流程处理收件箱：

# 任务
任何人发邮件到 jiahei@agentmail.to 附中文 EPUB，你的任务：理解意图 → 解析 → 给核心词汇用 markdown ** 加黑 → 回信附 .md 文件。

# Workspace 模式（重要！）
EPUB 经常有几百到几千段，**不能塞进对话上下文**。所以：
- `extract_text_blocks` 把 blocks 存进 workspace，只返回 `workspace_key` + 摘要给你
- `bold_workspace_paragraphs(workspace_key, ...)` **原地**更新 workspace 里的段落
- `assemble_workspace_markdown(workspace_key)` 拼成 md，存进 workspace 返回 `markdown_workspace_key`
- `send_reply_with_workspace_markdown(message_id, body_text, markdown_workspace_key, filename)` 发出去

你**全程只看 workspace_key 引用 + summary**，不直接看大数据。这样上下文不会爆。

# 标准工作流（每封邮件）

1. `list_unprocessed_emails` 看有没有要处理的。
2. 没有 → `done`。
3. 有 → 对每封：
   a. 看 from/subject/preview 理解用户意图
      - 默认：加黑核心术语，target_ratio=0.30
      - subject 里有 `[ratio=0.4]` → 用 0.4
      - 其他特殊指令暂不支持，回信告知就行
   b. 如果 `already_acked=true` 跳过 ack 步骤（这是上次崩溃续跑）
   c. 否则调 `send_ack`，预估时间公式：每 10000 字 ≈ 1 分钟
   d. **没附件** → `send_reply_text` 回 help → `mark_label processed` → 下一封
   e. **有附件**（找 .epub 的；其他格式回 "v1 暂不支持" + mark_failed）：
      i.   `download_attachment` → file_path
      ii.  `extract_text_blocks(file_path)` → workspace_key + 摘要
      iii. `bold_workspace_paragraphs(workspace_key, target_ratio, concurrency=10)`
           **此步是核心，处理所有段落，调一次即可，不要循环！**
      iv.  `assemble_workspace_markdown(workspace_key)` → markdown_workspace_key
      v.   `send_reply_with_workspace_markdown(message_id, body_text, markdown_workspace_key, filename)`
           body_text 写简短统计（段落数 / 加黑比例 / 耗时 / 字数）
      vi.  `mark_label processed`
   f. 工具失败 → `send_reply_text` 告知错误 → `mark_label failed` → 下一封
4. 全处理完 → `done`。

# 关键约束
- bold_workspace_paragraphs 是**一次性批处理整本书**，工具内部已经并发，**禁止**对一本书多次调用
- mark_label 必须做（漏了会被下次 cron 重跑）
- 错误隔离：一封失败不影响下一封
- 不要盲目重试：工具失败先看错误信息再决定

# 风格
专业、简洁。邮件正文友好但不啰嗦。
"""


# === MiMo API 调用 ===
def call_mimo(messages: list, tools: list = None, max_retries: int = 5) -> dict:
    # pro + thinking 默认开启；不写死 max_tokens 让 API 自己分配
    # 决策步骤用 high reasoning_effort（步骤少但每步要想清楚）
    body = {
        "model": MIMO_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "reasoning_effort": "high",
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    payload = json.dumps(body).encode()

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(MIMO_URL, data=payload, headers={
                "Authorization": f"Bearer {MIMO_API_KEY}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:200]
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  [mimo 429, wait {wait}s] {err[:100]}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"MiMo HTTP {e.code}: {err}")
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("MiMo retries exhausted")


# === Truncate large tool results to avoid blowing context ===
def _truncate_for_llm(value, depth=0, max_str=2000, max_list=200):
    """Massive tool results (e.g. 1000 段 blocks 列表) 不能整个塞回 LLM。
    递归截断超长字符串/列表，保留结构信息。"""
    if isinstance(value, str):
        if len(value) > max_str:
            return value[:max_str] + f"...[truncated, total {len(value)} chars]"
        return value
    if isinstance(value, list):
        if len(value) > max_list:
            sample = value[:5] + value[-3:]
            return [_truncate_for_llm(v, depth+1, max_str, max_list) for v in sample] + \
                   [f"...[truncated, list has {len(value)} items, showing 5 head + 3 tail]"]
        return [_truncate_for_llm(v, depth+1, max_str, max_list) for v in value]
    if isinstance(value, dict):
        return {k: _truncate_for_llm(v, depth+1, max_str, max_list) for k, v in value.items()}
    return value


# === Agent loop ===
def run_agent(max_steps: int = 80) -> dict:
    """主 agent loop。返回 {steps, done_summary, tool_calls_count}"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": "现在开始处理收件箱。"},
    ]
    # 工具调用上下文（agent 没法把整个 blocks 送回来 token 太多，所以我们存在外层 dict）
    workspace: dict = {}

    tool_count = 0
    for step in range(max_steps):
        print(f"\n--- step {step+1} ---")
        resp = call_mimo(messages, tools=TOOL_SCHEMAS)
        choice = resp["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason")

        # 把 assistant 的回复加到 history
        # MiMo / OpenAI compat: assistant message with optional tool_calls
        # MiMo thinking 模式要求把上一轮的 reasoning_content 原封回传，否则 400
        assistant_entry = {"role": "assistant"}
        if msg.get("content"):
            assistant_entry["content"] = msg["content"]
            print(f"  agent: {msg['content'][:200]}")
        else:
            assistant_entry["content"] = None
        if msg.get("reasoning_content"):
            assistant_entry["reasoning_content"] = msg["reasoning_content"]
        if msg.get("tool_calls"):
            assistant_entry["tool_calls"] = msg["tool_calls"]
        messages.append(assistant_entry)

        if not msg.get("tool_calls"):
            # Agent 没调工具也没说啥 → 异常退出
            print(f"  [agent stopped without tool_calls. finish={finish}]")
            break

        # 执行所有 tool calls
        for tc in msg["tool_calls"]:
            tool_count += 1
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception as e:
                args = {}
            print(f"  → tool: {fn_name}({json.dumps(args, ensure_ascii=False)[:200]})")

            # 特殊：done = sentinel 退出
            if fn_name == "done":
                print(f"  [agent done] {args.get('summary','')}")
                return {
                    "steps": step + 1,
                    "tool_calls": tool_count,
                    "done_summary": args.get("summary", ""),
                }

            # workspace shortcuts: 让大 result 不进 LLM 上下文
            # 用法：tools 返回里的 "blocks"/"results" 等大字段可由 agent 用 reference 名字回填
            # 但简单起见：bold_chunks_batch 的 results 是必须回到 agent，
            # 所以下面我们给它特殊截断让 agent 能引用而非看完。
            try:
                fn = TOOL_FUNCS.get(fn_name)
                if not fn:
                    raise RuntimeError(f"unknown tool: {fn_name}")
                result = fn(**args)
            except Exception as e:
                tb = traceback.format_exc()
                print(f"    [tool err] {tb[-500:]}", file=sys.stderr)
                result = {"error": str(e), "type": type(e).__name__}

            # 截断后塞回 LLM
            truncated = _truncate_for_llm(result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(truncated, ensure_ascii=False)[:8000],
            })
            print(f"    ← result: {json.dumps(truncated, ensure_ascii=False)[:300]}")

    # 走出循环说明 max_steps 到了
    print(f"  [hit max_steps={max_steps}]")
    return {"steps": max_steps, "tool_calls": tool_count, "done_summary": "max_steps reached"}


def main():
    if not MIMO_API_KEY:
        print("MIMO_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    try:
        result = run_agent()
        print(f"\n=== AGENT DONE ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
