# 虎牙虎粮自动发放

## 诊断与安全边界

- 手动 Action 和 `python main.py` 默认使用 **diagnose**，查询登录状态、普通虎粮和超粉虎粮库存。只有普通虎粮参与赠送。
- 显式 `python main.py --mode send` / Action `mode=send` 才赠送；定时任务仍使用 send，仅在默认分支运行。
- 已在 GitHub Actions 验证诊断及真实赠送：登录有效、普通虎粮 25 个送出、业务成功提示及库存归零均确认。定时执行仍可能受网络、页面变化和 Cookie 有效期影响；发生结果未知时请人工核对，不要盲目重跑。
- 页面使用 `page_load_strategy=none` 并等待关键元素就绪。每次赠送最多点击一次发送按钮和一次确认按钮。
- 登录优先调用只读 `getTimeSign` 和 `listTotal` 接口。业务状态 501 表示需要重新登录，其他查询异常会尝试网页登录检查。
- 登录通过后，Chrome 打开实际送礼包裹核对库存。如果背包页一直只有空列表，等待 30 秒后重新加载一次。两次都无法确认库存时，任务报告查询失败。赠送后的库存核对也使用这个只读流程。
- Actions 摘要显示模式、结果、库存和已确认数量。每次 send 都向 Telegram 报告结果。diagnose 在首次失败、原因变化及恢复时提醒，正常运行静默。去重状态来自同分支的历史 artifact（保留 90 天）。
- 通知需 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`TELEGRAM_THREAD_ID` 三个 Secret。手动 diagnose 可用 `test_notice=true` 发送接入测试；不进行送礼。
- GitHub 定时调度可能延迟，01:00 是期望触发时间，不保证准点。
- 公共 artifact 仅包含白名单 JSON（阶段、就绪状态、结果），不保存/上传 Cookie、原始 HTML、截图、完整 URL 或用户名。
- `DIAGNOSE_OK`：登录和库存查询通过；`EMPTY_STOCK`：确认库存为零；`INVENTORY_QUERY_FAILED`：查询失败；`SEND_FAILED_OR_UNKNOWN`：停止并人工核对，勿盲目 rerun。
- 每个房间送礼后必须出现本次页面新产生的“送礼成功”业务提示，才会计数并继续；拒绝/超时/结果未知立即停止，不补点。全部完成后重新查库存，库存核对不能取代平台赠送账单。
- 同 URL 重新导航也要求旧文档标记消失，并校验房间 lp/gid 参数。传输超时仅在导航/查询阶段有界重试，清理错误不会覆盖已记录结果。

本地安全测试（不使用账号，浏览器仅访问本机测试页）：
```bash
python -m unittest discover -s tests -v
RUN_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

轻量、开箱即用的 **虎牙虎粮自动批量赠送工具**，支持 GitHub Actions

---

## ✨ 特性
- 🔐 **凭证管理**：登录 Cookie 通过 GitHub Secrets 提供
- 🤖 **全自动运行**：支持定时执行 + 手动触发
- 📦 **无冗余依赖**：仅需 Selenium 环境，超轻量
- 🎯 **精准分配**：自动读取背包虎粮，批量分配到多个房间
- 🚀 **云原生**：直接在 GitHub Actions 运行，无需服务器

---

## 📁 项目结构
```
.github/workflows/auto.yml  # GitHub Actions 自动化配置
requirements.txt            # 依赖清单
main.py                     # 主程序（核心逻辑）
config.py                   # 页面选择器（唯一配置文件）
```

---

## 🚀 快速部署
### 1. 准备工作
1. 登录 [虎牙网页版](https://i.huya.com/)
2. F12 打开开发者工具 -> 网络 -> F5
3. 找到 "index.php?m=Msg&do=getMsgC" 项
4. 在标头中找到并复制整个 Cookie 字符串

### 2. GitHub 部署
1. Fork 本仓库
2. 进入仓库 **Settings → Secrets and variables → Actions**
3. 在 Secrets 中添加 `HUYA_COOKIE`（虎牙登录 Cookie）。定时任务固定使用房间 `998`。手动运行可通过 `rooms` 输入多个房间号，用英文逗号分隔。

4. 进入 **Actions** → 启用工作流
5. 完成！每天 **北京时间 01:00** 自动运行

---

## 🎮 手动运行
### 方式 1：GitHub 在线运行
1. 进入仓库 Actions
2. 选择 `虎牙虎粮自动发放`
3. 点击 `Run workflow`
4. 默认运行只读诊断。选择 `send` 可向 `rooms` 中指定的房间赠送，默认房间为 `998`。

### 方式 2：本地运行
```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量
export HUYA_COOKIE="你的Cookie"
export HUYA_ROOMS="518512,1964"

# 启动
python main.py
```

---

## ⚙️ 配置说明
| 名称 | 说明 |
|------|------|
| HUYA_COOKIE | 登录凭证（必须） |
| HUYA_ROOMS | 本地 send 模式必填，多个目标房间用英文逗号分隔；本地 diagnose 可省略 |

---

## 📅 定时任务
默认：**每天 北京时间 01:00** 执行
修改定时：编辑 `.github/workflows/auto.yml`
```yaml
on:
  schedule:
    # 每天 北京时间 01:00 运行
    - cron: '0 17 * * *'
```

---

## 📌 运行流程
1. 检查 Cookie，送礼模式另需有效房间号
2. 打开背包页面，读取虎粮数量
3. 自动分配数量到所有房间
4. 依次进入房间 → 悬停虎粮 → 自定义数量 → 赠送
5. 输出执行结果

---

## ⚠️ 重要提醒
- 请勿分享 Cookie，避免账号被盗
- Cookie 过期后重新获取更新即可
- 房间必须**正常开播**才能赠送礼物
- 本项目仅用于学习交流，遵守平台规则

---

## 🐛 常见问题
- **登录失败**：Cookie 过期或格式错误，重新获取
- **赠送失败**：房间未开播、礼物面板未加载
- **Action 报错**：检查 Secrets 是否正确配置

---

## 📄 许可证
MIT License
