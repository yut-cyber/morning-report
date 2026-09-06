# AI 每日早报

每天北京时间 **10:00** 自动生成一份「AI 每日早报」：网页看板 + 微信推送，全程免费、无人值守。

## 它能做什么

| 数据源 | 内容 | 数量 |
|---|---|---|
| GitHub Trending | 热门开源项目（名称/描述/星数/今日新增） | Top 10 |
| HuggingFace Papers | 每日热门 AI 论文 | 6 篇 |
| HackerNews | 技术/AI 快讯（AI 相关优先） | 8 条 |

所有内容通过 OpenRouter 免费大模型生成**中文一句话摘要**（未配置 key 时自动降级为免费机器翻译），然后：

1. 生成报纸风格响应式看板 → `docs/index.html`（GitHub Pages 托管）
2. 历史归档 → `docs/archive/日期.html`
3. Markdown 卡片推送到**个人微信**（PushPlus）
4. 任何环节失败 → 微信收到「⚠️ 早报生成失败」告警，绝不悄悄漏跑

## 快速开始

完整步骤见 [DEPLOY.md](DEPLOY.md)，概要：

1. Fork/创建本仓库（Public）
2. 配置仓库 Secrets：`OPENROUTER_API_KEY`、`PUSHPLUS_TOKEN`
3. 配置仓库 Variables：`DASHBOARD_URL`（Pages 网址）、可选 `OPENROUTER_MODEL`
4. 开启 GitHub Pages：Settings → Pages → Branch 选 `main` + `/docs`
5. Actions 页手动 Run workflow 验证

## 配置项

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `OPENROUTER_API_KEY` | 推荐 | OpenRouter 的 key，`sk-or-v1-` 开头；不配则用免费翻译兜底 |
| `PUSHPLUS_TOKEN` | 推荐 | [pushplus.plus](http://www.pushplus.plus/) 微信扫码登录获取；**需实名认证**后才能推送 |
| `DASHBOARD_URL` | 可选 | 微信卡片顶部「查看完整看板」链接 |
| `OPENROUTER_MODEL` | 可选 | 默认 `google/gemma-4-31b-it:free`；可换 OpenRouter 上任意模型 ID |

> ⚠️ OpenRouter 免费模型会不定期下线，若日志报 404/「unavailable for free」，去 https://openrouter.ai/models 筛选 `:free` 换一个模型 ID 填入变量即可，无需改代码。

## 本地运行

```bash
pip install -r requirements.txt

# 不配 key 也能跑（翻译兜底、跳过推送），仅生成页面
python main.py
```

生成的页面在 `docs/index.html`，浏览器直接打开。

## 工作原理

```
GitHub Actions cron (UTC 02:00 = 北京 10:00)
  → main.py 抓取三个数据源
  → OpenRouter 免费模型生成中文摘要（失败自动降级翻译）
  → 写入 docs/index.html + docs/archive/日期.html，bot 自动提交
  → PushPlus 推送 Markdown 卡片到微信
```

## 定时与排错

- GitHub 免费版 cron 有排队延迟，一般 10:00–10:20 送达，属正常现象
- 常见问题对照表见 [DEPLOY.md 第 8 节](DEPLOY.md)

## 安全

所有密钥只放 GitHub Secrets，不入代码库。怀疑泄露时去 OpenRouter / pushplus 重置后更新 Secrets 即可。
