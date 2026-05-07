# jiahei-service

公开的 EPUB 加黑邮件服务：给 `jiahei@agentmail.to` 发邮件附 EPUB，自动用 MiMo v2.5 给中文核心词汇加粗后发回。

## 用法

1. 任意邮箱给 `jiahei@agentmail.to` 发一封邮件
2. 附件带一个 `.epub` 文件（中文）
3. 主题里可加 `[ratio=0.4]` 自定义加黑比例（默认 0.3）
4. 等 1-10 分钟（GH Actions cron 每分钟轮询一次）
5. 收到回信附件 `xxx_bolded.epub`

## 架构

- 触发：GitHub Actions cron（`* * * * *`，公开 repo 免费无限）
- 邮件：[AgentMail](https://agentmail.to/) 免费层
- LLM：[小米 MiMo v2.5](https://token-plan-cn.xiaomimimo.com)（必须 `thinking:disabled`）
- 处理：[ebooklib](https://github.com/aerkalov/ebooklib) + BeautifulSoup `html.parser`

## 部署

```bash
# 1) 创建 GitHub 公开 repo
gh repo create jiahei-service --public --source . --remote origin --push

# 2) 设 Secrets
gh secret set MIMO_API_KEY      --body 'tp-...'
gh secret set AGENTMAIL_API_KEY --body 'am_us_...'
gh secret set JIAHEI_INBOX      --body 'jiahei@agentmail.to'

# 3) 手动触发首次运行
gh workflow run process.yml

# 4) 看日志
gh run list --limit 3
gh run view <run-id> --log
```

## 本地调试

```bash
export MIMO_API_KEY=tp-...
export AGENTMAIL_API_KEY=am_us_...
export JIAHEI_INBOX=jiahei@agentmail.to

pip install -r requirements.txt
python3 process.py
```

## 文件

- `process.py` — 主入口：拉收件箱、找未处理 EPUB、加黑、回信
- `epub_bold.py` — EPUB 解析+加黑+打包流水线
- `.github/workflows/process.yml` — cron 触发器
- `requirements.txt` — Python 依赖

## 已知限制

- 中文为主（英文 prompt 规则不匹配，效果差）
- 单个 EPUB 25 MB 上限
- GH Actions cron 实际延迟可达 5-15min（高负载期）
- 加黑比例实测平均 20% 左右（目标 30%，模型偏保守）
