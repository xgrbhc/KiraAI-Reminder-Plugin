

<div align="center">

# 🌟 KiraAI Reminder Plugin

**高可用、全功能、智能化的 KiraAI 定时提醒生态插件**

![Version](https://img.shields.io/badge/version-v2.2.1-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![KiraAI](https://img.shields.io/badge/KiraAI-Plugin-orange.svg)

[特性](#-核心特性) • [安装](#-安装与配置) • [指令](#-人类直连快捷指令) • [AI 工具](#-大模型-llm-自动接管) • [开发](#-参与贡献)

</div>

---

## 📸 界面预览 (Web UI & 聊天交互)
<img width="2104" height="1326" alt="image" src="https://github.com/user-attachments/assets/ca5d2863-6e03-4a2a-8846-42b3e64d2472" />

---

## ✨ 核心特性

- 📅 **多维时间引擎**：支持 `精准定时`、`周期循环` (每天/周/月/年)、`间隔触发` (每N分钟)。
- 🎲 **拟真随机延时**：指定时间段内触发 N 次随机提醒，让 AI 带有“人性化”的不可预测感。
- 🧭 **自主意图循环**：支持白名单会话中的每日复盘、低频随机检查、意图状态维护和到期跟进兜底。
- 📬 **提醒投递回执**：只有包含提醒的模型调用正常返回后，才清理一次性提醒；失败或结果不明时保留状态，供后续 LLM 或 WebUI 审慎补救。
- 🔐 **可信身份与统一 ACL**：区分用户、机器人、系统、Web 管理端和遗留记录；内部事件使用进程级可信信封，不再依赖 `system/unknown` 放行。
- 🌐 **主 WebUI 侧边栏看板**：基于 KiraAI `v2.23.0` 插件页面注册能力，入口为主 WebUI 左侧 `提醒 / Reminders`，统一走主 WebUI 认证与插件 API。
- ⚡ **无延迟极速指令**：内置类 CLI 命令解析器（如 `/r add`），绕过 LLM 思考过程，毫秒级响应您的增删改查。
- 🛡️ **防误删与越权保护**：
  - 全局超管（上帝视角）可指令级透视全域用户数据 `/r all`。
  - 重要提醒（⭐）被大模型试图删除时，强制下发 Token 令牌进行二次安全确认。
- 💾 **数据与调度保护**：JSON 文件采用同进程异步锁与原子替换；调度器健康检查会尝试重启。事件发布失败最多尝试 3 次，间隔固定为 5 秒。模型响应失败、超时或进程中断不会被误报为确认成功；仍需留意平台消息发送属于独立链路。

---

## 📦 安装与配置

### 1. 结构部署
将此项目放入您的主程序 `data/plugins/` 目录下，层级树应为：
```text
KiraAI/
 └── data/
      └── plugins/
           └── reminder_plugin/
                ├── main.py
                ├── config.py
                ├── models.py
                ├── storage.py
                ├── time_utils.py
                ├── reminder_service.py
                ├── scheduler.py
                ├── delivery.py
                ├── autonomy.py
                ├── identity.py
                ├── permissions.py
                ├── schema.json
                ├── manifest.json
                ├── requirements.txt
                ├── web/
                │    ├── index.html
                │    ├── app.js
                │    └── style.css
                └── tests/
                     ├── test_contracts.py
                     ├── test_reminder_service.py
                     ├── test_scheduler.py
                     ├── test_delivery.py
                     ├── test_autonomy.py
                     ├── test_identity_permissions.py
                     └── test_storage_migration.py
```

### 2. 参数选配 (schema.json)
重启节点或在管理面加载本插件后，可配置以下进阶项：
- `admin_users`：超级管理员账号/QQ 数组录入。
- `authorized_users`：额外允许在群聊中创建提醒的用户账号/ID 数组。
- `group_create_policy`：群聊提醒创建策略，可选 `admin_only`、`mentioned_user` 或 `all`（all 表示群聊所有成员均可创建，是否合理交由 LLM 行为准则把控）。
- `action_policy`：独立的自动动作权限策略，可选 `admin_only`、`admin_and_trusted_bot` 或 `all`；默认不会因为开启群聊 `all` 而同步开放高风险 action。
- `autonomy_enabled` / `allowed_sessions`：自主意图循环总开关与会话白名单。
- `advanced_config.autonomy_mode`：可选 `off`、`observe`、`plan_only`、`act_with_confirm`、`trusted_admin`；权限在工具执行层硬校验。
- `advanced_config.autonomy_allowed_tools`：自主事件工具白名单。非 `trusted_admin` 模式会继续限制为插件自身安全工具。
- `advanced_config.usage_prompt`：注入 LLM 请求的插件使用提示词，用于指导模型何时调用提醒工具。

升级到 v2.2.0 时，旧提醒会幂等迁移到 identity schema v2。首次迁移前会在插件数据目录生成 `reminders.pre-v2.2.backup.json`；无法恢复所有者的群聊旧记录仅管理员可见和管理。

如果 `reminders.json`、`autonomous_state.json` 或新增的 `delivery_state.json` 已存在但无法读取、JSON 不完整或顶层不是对象，插件会报错并保留原文件，不再把它当作空数据写回。遇到此错误时，先关闭 KiraAI，备份异常文件，再检查权限或从可信备份恢复；不要直接删除或清空原文件。文件确实不存在时仍按首次使用处理。

### 3. WebUI 入口

本插件从 `v2.0.0` 起使用 KiraAI `v2.23.0` 新增的插件 WebUI 页面注册能力；当前投递回执还依赖 `v2.24.2` 起在所有模型失败时触发的 provider 异常事件，因此 `manifest.json` 现设为：

```json
"core_version": ">=2.24.2"
```

投递回执只使用主项目已有的 `on.exception` 与 `on.llm_response` 钩子，不需要修改 KiraAI 核心。全部模型调用失败时，插件根据 `source=provider`、`stage=agent_loop` 的异常事件将该次投递标为“未确认”；随后收到的合成响应不能再将它标为成功。若进程中断或未收到回调，等待超时后同样保留为“未确认”。该状态不代表 QQ 已发送，也不代表用户已看到提醒。

安装并重启 KiraAI 后，可在主 WebUI 左侧侧边栏进入：`提醒 / Reminders`。

`web/index.html` 从同目录加载 `app.js` 和 `style.css`；`app.js` 仍使用主 WebUI 注入的 `window.PluginPageContext` 调用插件 API。页面资源不需要额外注册静态路由。

对应页面与接口路径：

- 页面入口：`/plugin-page/reminder_plugin/dashboard`
- 插件页面服务：`/page/plugin/reminder_plugin/dashboard`
- 插件 API：`/api/plugin/reminder_plugin/...`

旧版独立入口 `http://127.0.0.1:18080/web`、独立 `uvicorn` 服务和 `web_port` 配置已移除。

### 4. 依赖
插件分发或在干净环境安装时，请一并安装 `requirements.txt` 中的依赖：

- `APScheduler>=3.10,<4`

---

## 💻 人类直连快捷指令 (无需 AI 思考)

> 支持多种唤醒前缀：`/r`, `/待办`，或 `-r`
>
> 群聊快捷命令始终要求艾特或回复当前机器人，以避免同群多个机器人同时响应；此规则不受 `group_create_policy=all` 影响。

| 操作类型 | 指令示例 | 说明 / 功能 |
| :--- | :--- | :--- |
| **可用帮助** | `/r help` | 打印可用控制流及别名清单 |
| **我的待办** | `/r` | 查阅当前账户名下的待办序列及序号 |
| **快速添加** | `/r add 2026-03-25 14:00 开会` | 极简格式注册单次/定点任务 |
| **任务删除** | `/r rm 1` | 删除列表内序号为 `1` 的提醒 |
| **冻结/唤醒** | `/r pause 1` 或 `/r resume 1` | 冻结不触发 / 重新激活倒计时 |
| **查阅元信息** | `/r view 1` | 穿透查看任务的包含调度、分类等高级隐藏属性 |

💂 **系统超管特权网**：
- `/r all`：调取并按用户分组排列系统基座内所有人的待办事项。
- `/r view @`：跨会话精准聚合查看“”的任务明细。

---

## 🤖 大模型 (LLM) 自动接管 (Function Calling)

作为智能体的“海马体”，AI 可通过以下 `Function Calling` 自主操纵系统：

- 🛠 `set_reminder`：注册含有 `action` 联想、`category` 等高级元标记的复合提醒。
- 🛠 `list_reminders`：探查时间环境以支撑模型作出决策。
- 🛠 `delete_reminder` / `confirm_delete_reminder`：处理带 `令牌二次认证` 的关键节点删除流。
- 🛠 `mark_reminder_important` / `unmark_reminder_important`：感知到高价值日程自动加注⭐。
- 🛠 `edit_reminder`：无缝重构现存提醒。
- 🛠 `list_autonomous_intents` / `create_autonomous_intent`：读取或创建当前会话的自主意图。
- 🛠 `update_autonomous_intent` / `close_autonomous_intent`：维护、暂停或关闭自主意图。
- 🛠 `schedule_intent_followup`：以 bot-owned reminder 安排下一次低频自主跟进。
- 🛠 `list_delivery_issues` / `review_delivery_issue`：查看并处理当前主体有权访问的未确认投递；只允许明确失败且无 `action` 的记录由 LLM 自动重试。

---

## 🤝 参与贡献

该生态插件属于不断演进中的版本，欢迎提出 Issue 或者提交 Pull Request (PR) 来增加新的特性！

<div align="center">Made with ❤️ by xgrbhc & Community</div>
