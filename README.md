<div align="center">

# 🌟 KiraAI Reminder Plugin v1.0

**高可用、全功能、智能化的 KiraAI 定时提配生态插件**

![Version](https://img.shields.io/badge/version-v5.0-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![KiraAI](https://img.shields.io/badge/KiraAI-Plugin-orange.svg)

[特性](#-核心特性) • [安装](#-安装与配置) • [指令](#-人类直连快捷指令) • [AI 工具](#-大模型-llm-自动接管) • [开发](#-参与贡献)

</div>

---

## 📸 界面预览 (Web UI & 聊天交互)
<img width="1857" height="952" alt="image" src="https://github.com/user-attachments/assets/678f2578-3489-4c05-83de-76b3800464ba" />

---

## ✨ 核心特性

- 📅 **多维时间引擎**：支持 `精准定时`、`周期循环` (每天/周/月/年)、`间隔触发` (每N分钟)。
- 🎲 **拟真随机延时**：指定时间段内触发 N 次随机提醒，让 AI 带有“人性化”的不可预测感。
- 🌐 **独立 Web 看板微服务**：内置 FastAPI 驱动的高性能看板（默认 `8080`），可跨设备、跨局域网监控项目全体成员进度。
- ⚡ **无延迟极速指令**：内置类 CLI 命令解析器（如 `/r add`），绕过 LLM 思考过程，毫秒级响应您的增删改查。
- 🛡️ **防误删与越权保护**：
  - 全局超管（上帝视角）可指令级透视全域用户数据 `/r all`。
  - 重要提醒（⭐）被大模型试图删除时，强制下发 Token 令牌进行二次安全确认。
- 💾 **工业级高可用架构**：
  - 原子级排他并发锁，多线程高频读写绝不损坏 `.json` 数据文件。
  - 调度器长驻健康哨兵检查、宕机自启、投递异常指数退避重试（MaxRetries=3）。

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
                ├── schema.json
                ├── manifest.json
                └── index.html
```

### 2. 参数选配 (schema.json)
重启节点或在管理面加载本插件后，可配置以下进阶项：
- `admin_users`：超级管理员账号/QQ 数组录入。
- `web_port`：Web 大屏监控运行端口（默认为 `8080`，遇冲突时自动向 8089 顺延寻找可用端口）。

---

## 💻 人类直连快捷指令 (无需 AI 思考)

> 支持多种唤醒前缀：`/r`, `/待办`，或 `-r`

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
- `/r view @李雷`：跨会话精准聚合查看“李雷”的任务明细。

---

## 🤖 大模型 (LLM) 自动接管 (Function Calling)

作为智能体的“海马体”，AI 可通过以下 `Function Calling` 自主操纵系统：

- 🛠 `set_reminder`：注册含有 `action` 联想、`category` 等高级元标记的复合提醒。
- 🛠 `list_reminders`：探查时间环境以支撑模型作出决策。
- 🛠 `delete_reminder` / `confirm_delete_reminder`：处理带 `令牌二次认证` 的关键节点删除流。
- 🛠 `mark_reminder_important` / `unmark_reminder_important`：感知到高价值日程自动加注⭐。
- 🛠 `edit_reminder`：无缝重构现存提醒。

---

## 🤝 参与贡献

该生态插件属于不断演进中的版本，欢迎提出 Issue 或者提交 Pull Request (PR) 来增加新的特性！

<div align="center">Made with ❤️ by KiraAI Team & Community</div>
