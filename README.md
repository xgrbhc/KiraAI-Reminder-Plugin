

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
  - 聊天管理员按适配器授权，可通过 `/r all` 查看当前会话中其他用户的提醒；已认证 WebUI 保留部署管理权限。
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
                ├── migration.py
                ├── time_utils.py
                ├── reminder_service.py
                ├── scheduler.py
                ├── delivery.py
                ├── autonomy.py
                ├── identity.py
                ├── message_sources.py
                ├── confirmation.py
                ├── confirmation_routes.py
                ├── permissions.py
                ├── schema.json
                ├── manifest.json
                ├── requirements.txt
                ├── web/
                │    ├── index.html
                │    ├── app.js
                │    ├── i18n.js
                │    └── style.css
                └── tests/
                     ├── test_contracts.py
                     ├── test_reminder_service.py
                     ├── test_scheduler.py
                     ├── test_delivery.py
                     ├── test_autonomy.py
                     ├── test_identity_permissions.py
                     ├── test_adapter_identity.py
                     ├── test_message_sources.py
                     ├── test_confirmation.py
                     └── test_storage_migration.py
```

### 2. 参数选配 (schema.json)
重启节点或在管理面加载本插件后，可配置以下进阶项：
- `admin_users`：聊天管理员列表，一行一个 `适配器名称:用户ID`，例如 `QQ:123456`；仅在对应适配器内具有管理员权限。
- `authorized_users`：额外允许在群聊中创建提醒的用户列表，同样使用 `适配器名称:用户ID`。
- `legacy_acl_adapter`：旧裸 ID 权限的归属适配器，例如 `QQ`。仅配置一个适配器时首次加载会备份并固定归属；配置多个适配器（包括停用的）时须手动指定，或把旧列表改成带适配器的格式。空值不授予裸 ID 全局权限。
- `group_create_policy`：群聊提醒创建策略，可选 `admin_only`、`mentioned_user` 或 `all`（all 表示群聊所有成员均可创建，是否合理交由 LLM 行为准则把控）。
- `action_policy`：独立的自动动作权限策略，可选 `admin_only`、`admin_and_trusted_bot` 或 `all`；默认不会因为开启群聊 `all` 而同步开放高风险 action。
- `autonomy_enabled` / `allowed_sessions`：自主意图循环总开关与会话白名单。
- `advanced_config.autonomy_mode`：可选 `off`、`observe`、`plan_only`、`act_with_confirm`、`trusted_admin`；权限在工具执行层硬校验。
- `advanced_config.autonomy_allowed_tools`：自主事件工具白名单。非 `trusted_admin` 模式会继续限制为插件自身安全工具。
- `advanced_config.usage_prompt`：注入 LLM 请求的插件使用提示词，用于指导模型何时调用提醒工具。

默认使用说明已同步按需来源查询和单次真实确认规则，代码备用默认值与 schema 一致。升级不会覆盖保存的 `usage_prompt`；若要采用新版，可在插件高级配置中粘贴 [已确认改稿第 2.4 节](docs/PROMPT_CHANGE_PROPOSAL.md) 的完整文本，再按当前部署方式保存／重载。不需要卸载插件或删除配置。

未确认投递摘要只在本轮最新用户输入末尾提供，不进入静态系统提示词或保存的用户消息历史；最多 5 条，内容使用有界 JSON 数据表示。正常 Tool 结果仍会进入会话历史，不承诺零 Token 成本或固定缓存命中率。实施及验证边界见 [缓存与提示词检查](docs/CACHE_PROMPT_REVIEW.md)。

升级到 v2.2.0 时，旧提醒会幂等迁移到 identity schema v2。首次迁移前会在插件数据目录生成 `reminders.pre-v2.2.backup.json`；无法恢复所有者的群聊旧记录仅管理员可见和管理。

当前身份隔离改进使用 identity schema v3：用户身份按“适配器名称＋用户 ID”区分，同号不自动合并，适配器名称区分大小写且须与会话 ID 前缀一致。旧提醒和投递记录中的提醒快照会在加载时同步迁移，保留原有所有者、内容和调度信息。首次迁移前分别生成 `reminders.pre-identity-v3.backup.json` 和 `delivery_state.pre-identity-v3.backup.json`（对应文件有待迁移记录时）；旧备份不覆盖。自动绑定旧权限时，配置目录会生成 `reminder_plugin.pre-adapter-acl.backup.json`。

迁移任一文件失败会停止插件初始化，不启动调度器；修复读取或写入问题后重新加载可幂等完成迁移。本轮不修改主项目或运行时 LLM 缓存；按需来源 Tool 与轻量待确认请求已实现。完整边界及后续验收见 `docs/GROUP_IDENTITY_PLAN.md`、`docs/CONFIRMATION_PLAN.md`。

多人或不同来源上下文合批时，LLM 可按需调用 `list_message_sources` 获取临时来源标记，再通过 `set_reminder(source_ref=...)` 为对应用户创建无 `action` 的普通个人提醒。标记只对当前批次有效，不授予权限；查询只读当前消息，不改写历史。单一用户且相同来源上下文仍可直接创建。

批次中的潜在用户来源须全部通过当前创建策略才自动创建；混有无权限用户时，所选用户本身获准可建立待确认请求，缺失身份或内部事件混入时仍不自动创建。多人查看、修改、删除和 `action` 使用 `source_ref` 建立待确认请求，由 LLM 自然询问一次；唯一普通请求回复“确认”或“同意”即可，多项时附请求编号。群聊确认需 @ 或回复当前机器人。群聊查询内容可能被展示给群成员，需要私密管理时使用认证 WebUI。

重要删除回复“确认删除”，唯一请求无需编号；取消已有重要标记也需一次确认，避免先取消标记再绕过删除保护。普通私聊的创建、查询、编辑、暂停／恢复保持原体验；WebUI 保留原页面确认和重要删除令牌，不需再到聊天确认。重要跟进不能被自主关闭／替换顺带清理，正常到期回执清理语义不变。

待确认请求保存固定参数，5 分钟有效；确认执行时重新校验身份、权限和目标变化，模型不能替用户确认或替换参数。同一请求仅消费一次；停用、重载或重启失效。执行仅限真实确认消息所在批次，模型未在本轮完成时可在有效期内重新确认；失败／结果不明不自动重复提交。多人自主意图工具仍保守拒绝，聊天确认不放开结果不明／含 `action` 的投递重试和忽略限制。

如果 `reminders.json`、`autonomous_state.json` 或新增的 `delivery_state.json` 已存在但无法读取、JSON 不完整或顶层不是对象，插件会报错并保留原文件，不再把它当作空数据写回。遇到此错误时，先关闭 KiraAI，备份异常文件，再检查权限或从可信备份恢复；不要直接删除或清空原文件。文件确实不存在时仍按首次使用处理。

### 3. WebUI 入口

本插件从 `v2.0.0` 起使用 KiraAI `v2.23.0` 新增的插件 WebUI 页面注册能力；当前投递回执还依赖 `v2.24.2` 起在所有模型失败时触发的 provider 异常事件，因此 `manifest.json` 现设为：

```json
"core_version": ">=2.24.2"
```

投递回执只使用主项目已有的 `on.exception` 与 `on.llm_response` 钩子，不需要修改 KiraAI 核心。全部模型调用失败时，插件根据 `source=provider`、`stage=agent_loop` 的异常事件将该次投递标为“未确认”；随后收到的合成响应不能再将它标为成功。若进程中断或未收到回调，等待超时后同样保留为“未确认”。该状态不代表 QQ 已发送，也不代表用户已看到提醒。

安装并重启 KiraAI 后，可在主 WebUI 左侧侧边栏进入：`提醒 / Reminders`。

`web/index.html` 从同目录加载 `i18n.js`、`app.js` 和 `style.css`；`app.js` 仍使用主 WebUI 注入的 `window.PluginPageContext` 调用插件 API。页面资源不需要额外注册静态路由。

重试投递、忽略此次异常和删除提醒使用页面内确认框，兼容主 WebUI 的 iframe 沙箱。重要提醒仍须完成后端令牌确认。提交时禁用重复操作；切换会话后清空旧列表，须重新加载后才能操作。读取失败不会被当作“没有提醒”；提交结果不明时需先刷新核对，不自动重复提交。

前端检查记录与剩余改进项见 [前端检查记录](docs/FRONTEND_REVIEW.md)。新增提示文案的中英文在插件内维护，不修改主项目翻译文件。

同会话操作后的后台刷新会保留当前列表，避免加载占位导致页面跳到顶部；刷新期间仍暂停写操作。切换会话或读取失败时清空旧数据，确认框焦点恢复不会主动滚动页面。

操作通知最多同时显示 3 条，约 4 秒后自动消失，也可点击右侧 × 关闭。标题、正文和类型完全相同的通知合并并重新计时；成功提示不会挤掉错误提示。关闭通知只隐藏消息，不撤销操作或删除提醒。

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

- 🛠 `list_message_sources`：按需读取当前批次的昵称、消息片段及临时来源标记；无提醒需求时不必调用，返回量有界。
- 🛠 `list_pending_reminder_requests` / `confirm_reminder_request`：按需查询最小请求标记，并在真实用户确认后执行保存的操作；无确认需求时不必查询，不每轮附加身份表。
- 🛠 `set_reminder`：注册含有 `action` 联想、`category` 等高级元标记的复合提醒；多人普通创建需有效 `source_ref` 并通过批次权限检查。
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

插件回归测试（在插件目录执行）：

```bash
python -m pytest tests/ -q
node --test tests/test_dashboard.js
```

前端测试使用 Node 内置测试框架，无需安装 npm 依赖。可用 `python tests/dashboard_server.py --port 0` 启动隔离浏览器测试页面；它只在本机监听，使用内存测试数据和模拟接口，不连接 Kira 或 LLM。按 Ctrl+C 停止测试服务器。

隔离页面可附加 `?sid=Test%3Adm%3Aalice&list_size=30&read_delay=600`，生成长列表并模拟读取延迟，以检查后台刷新时的滚动位置；这些参数仅用于测试服务器。

该生态插件属于不断演进中的版本，欢迎提出 Issue 或者提交 Pull Request (PR) 来增加新的特性！

<div align="center">Made with ❤️ by xgrbhc & Community</div>
