# AI 每日早报 · 部署说明书

> 目标：每天北京时间 10:00 自动生成「AI 每日早报」网页，并推送 Markdown 卡片到个人微信。
> 全程免费，总耗时约 15 分钟。按顺序执行，每步都有验收标准，打勾后再进行下一步。

---

## 0. 你需要准备的东西

| 物品 | 用途 | 获取方式 |
|---|---|---|
| GitHub 账号 | 托管代码 + 定时任务 + 网页托管 | 已有即可 |
| OpenRouter API Key | 调免费大模型生成中文摘要 | 见第 2 步 |
| PushPlus Token | 推送消息到个人微信 | 见第 3 步 |

项目文件清单（已在 `morning-report/` 目录内，无需修改任何代码）：

```
morning-report/
├── main.py                      # 核心流水线(抓取→摘要→生成页面→推送)
├── requirements.txt             # Python 依赖
├── .github/workflows/daily.yml  # GitHub Actions 定时任务
├── .gitignore
└── docs/                        # 生成的网页会写到这里(GitHub Pages 源)
```

---

## 1. 创建 GitHub 仓库并推送代码（约 3 分钟）

1. 打开 https://github.com/new ，仓库名填 `morning-report`，**Public**（私有仓库的 Pages 需要付费版），不要勾选任何初始化文件，点 Create。
2. 在本机项目目录执行：

```bash
cd morning-report
git init
git add .
git commit -m "init: AI daily morning report"
git branch -M main
git remote add origin https://github.com/<你的用户名>/morning-report.git
git push -u origin main
```

> 也可以直接在 GitHub 网页上 `Add file → Upload files` 把整个文件夹拖进去上传。

**✅ 验收**：浏览器打开仓库页，能看到 `main.py` 和 `.github/workflows/daily.yml`。

---

## 2. 获取 OpenRouter API Key（约 3 分钟）

1. 打开 https://openrouter.ai/ ，用 Google 或 GitHub 登录。
2. 左侧菜单 **Keys** → **Create Key**，名字随便填（如 `morning-report`）。
3. 复制生成的 key（形如 `sk-or-v1-xxxx...`），**只显示一次，立即保存**。

> 默认模型 `deepseek/deepseek-chat-v3-0324:free` 是免费的，不充值也能用。免费模型有每日调用上限，早报每天只调 1 次，完全够用。

**✅ 验收**：手里有一串 `sk-or-v1-` 开头的 key。

---

## 3. 获取 PushPlus Token（约 2 分钟）

1. 打开 http://www.pushplus.plus/ ，点「微信扫码登录」，用**接收早报的那个微信号**扫码。
2. 登录后首页「一对一推送」处会显示你的 **token**，复制保存。

> PushPlus 免费版每天 200 条，早报每天 1-2 条，绰绰有余。消息通过「pushplus 推送加」公众号送达微信。

**✅ 验收**：手里有一串 32 位 token；微信里已关注推送公众号。

---

## 4. 配置仓库密钥（约 2 分钟）

进入仓库 → 顶部 **Settings** → 左侧 **Secrets and variables** → **Actions**。

### 4.1 Secrets 标签页，点 `New repository secret`，逐个添加：

| Name | Value |
|---|---|
| `OPENROUTER_API_KEY` | 第 2 步的 `sk-or-v1-...` |
| `PUSHPLUS_TOKEN` | 第 3 步的 32 位 token |

### 4.2 Variables 标签页，点 `New repository variable`，添加：

| Name | Value | 说明 |
|---|---|---|
| `DASHBOARD_URL` | 先留空或随便填，第 6 步后回填 | 微信卡片里「完整看板」的链接 |
| `OPENROUTER_MODEL` | 可不填 | 不填则用默认免费模型；想换模型填模型 ID，如 `qwen/qwen3-235b-a22b:free` |

**✅ 验收**：Secrets 页能看到 2 条记录（值显示为 `***` 属正常）。

---

## 5. 开启 GitHub Pages（约 1 分钟）

1. 仓库 **Settings** → 左侧 **Pages**。
2. **Source** 选 `Deploy from a branch`。
3. **Branch** 选 `main`，文件夹选 `/docs`，点 **Save**。

**✅ 验收**：Pages 页面顶部出现提示，稍后（首次约 1-5 分钟）会显示绿色「Your site is live at `https://<用户名>.github.io/morning-report/`」。

---

## 6. 首次手动触发 + 全链路验证（约 5 分钟）

1. 仓库顶部 **Actions** → 左侧选 **Daily Morning Report** → 右侧 **Run workflow** → 点绿色按钮确认。
2. 等 1-2 分钟，刷新看运行状态：
   - 🟢 绿色：成功，继续下面验证
   - 🔴 红色：点进去看日志，对照第 8 节排错
3. **三项验证**：
   - ✅ 微信收到「AI 早报 YYYY-MM-DD」卡片，里面有 GitHub 热门 / 论文 / 快讯
   - ✅ 打开 `https://<用户名>.github.io/morning-report/` 能看到当天的看板页面
   - ✅ 仓库 `docs/` 目录多了 `index.html` 和 `archive/日期.html` 的新提交（由 github-actions[bot] 提交）
4. 把 Pages 网址回填：Settings → Secrets and variables → Actions → Variables，把 `DASHBOARD_URL` 改为 `https://<用户名>.github.io/morning-report`。

至此全部完成。之后每天北京时间 10:00 自动运行，无需任何维护。

---

## 7. 日常工作原理（出问题时看这里）

```
GitHub Actions 定时(UTC 02:00 = 北京 10:00)
  → main.py 抓取 GitHub Trending / HuggingFace Papers / HackerNews
  → 调 OpenRouter 免费模型生成中文摘要(失败则自动降级为机器翻译)
  → 生成 docs/index.html + docs/archive/日期.html, bot 自动提交
  → PushPlus 推送 Markdown 卡片到微信
任何环节异常 → 微信收到「⚠️ 早报生成失败」告警 + 错误堆栈
```

---

## 8. 常见问题排查

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 微信没收到，Actions 绿色 | PushPlus token 错；公众号取关了 | 检查 Secret 值；重新关注 pushplus 公众号 |
| 收到「⚠️ 早报生成失败」 | 看卡片里的错误堆栈 | 多数是 GitHub/HF 临时抽风，手动 Run workflow 重试即可 |
| 页面内容是英文 | `OPENROUTER_API_KEY` 没配或失效 | 检查 Secret；key 无效时去 OpenRouter 重新生成 |
| 摘要质量差/想换模型 | 默认模型不合口味 | Variables 加 `OPENROUTER_MODEL`，填 OpenRouter 上任意模型 ID（带 `:free` 后缀的免费） |
| 10:00 没跑，10:10 才收到 | GitHub 免费版 cron 排队延迟 | 正常现象（延迟一般 < 20 分钟）。要求准点可改用 Cloudflare Workers Cron 触发，找维护者升级 |
| Actions 红色，日志显示推送失败 | PushPlus 当天超限或 token 失效 | 重新登录 pushplus.plus 确认 token |
| Pages 打开 404 | Pages 没开或分支/目录选错 | 检查 Settings → Pages 是 `main` + `/docs`；首次部署等 5 分钟 |
| 想改推送时间 | — | 编辑 `.github/workflows/daily.yml` 里的 cron（注意是 UTC，北京时间 −8 小时） |

---

## 9. 安全须知

- **任何时候不要把 `OPENROUTER_API_KEY` 和 `PUSHPLUS_TOKEN` 写进代码或提交到仓库**，只放 GitHub Secrets。
- 如果怀疑泄露：OpenRouter 后台删 key 重建、pushplus.plus 重置 token，然后更新 Secrets 即可，无需改代码。

---

## 10. 交接清单（全部打勾即完成）

- [ ] 仓库已创建且代码已推送
- [ ] Secrets：`OPENROUTER_API_KEY`、`PUSHPLUS_TOKEN`
- [ ] Variables：`DASHBOARD_URL`（已填 Pages 网址）
- [ ] Pages 已开启，网址可访问
- [ ] 手动触发一次，Actions 绿色
- [ ] 微信收到早报卡片
- [ ] 看板页面正常、内容为中文
