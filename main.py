"""
KiraAI 提醒插件 v1.0 

v1.0 功能说明：
- 从独立工具 (data/tools/reminder.py) 迁移为标准插件 (data/plugins/)
- 消除 core.services.runtime 依赖（该模块在 v2.0.0 中不存在）
- 消除调用栈爬帧 hack（get_current_session_id）
- 通过 self.ctx.adapter_mgr 获取适配器，通过 event.sid 获取会话 ID
- 支持 PluginContext 注入，符合 v2.0.0 规范

功能特性：
- ✅ 定时提醒（支持 YYYY-MM-DD HH:MM 格式）
- ✅ 重复提醒（每天/每周/每月/每年）
- ✅ 间隔提醒（每隔N分钟）
- ✅ 随机时间提醒（可指定次数或随机次数）
- ✅ 提醒列表管理（查看/删除）
- ✅ 重要提醒标记（LLM 删除需用户二次确认）
- ✅ 数据持久化（JSON 存储）
"""

import json
import os
import tempfile
import time
import traceback
import uuid
import asyncio
import datetime
import random
from pathlib import Path
from typing import Optional, Dict, List, Any, AsyncGenerator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.plugin import BasePlugin, logger, register_tool, on, Priority
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent, KiraIMMessage, MessageChain
from core.chat.session import User, Group
from core.chat.message_elements import Notice, Text
from core.utils.path_utils import get_data_path
from core.adapter.adapter_utils import IMAdapter
from pydantic import BaseModel, Field, field_validator

class ReminderConfig(BaseModel):
    admin_users: List[str] = Field(default_factory=list, description="配置超管账号名或ID列表，拥有跨界管理权限")
    web_port: int = Field(default=8080, description="Web 大屏服务端口，若被占用将自动递增尝试")

    @field_validator("admin_users", mode="before")
    @classmethod
    def parse_admin_users(cls, v):
        if not v:
            return []
        
        res = []
        
        if isinstance(v, str):
            v_cleaned = v.replace("，", ",").replace("\n", ",")
            res.extend([x.strip() for x in v_cleaned.split(",") if x.strip()])
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    item_cleaned = item.replace("，", ",").replace("\n", ",")
                    res.extend([x.strip() for x in item_cleaned.split(",") if x.strip()])
                else:
                    res.append(str(item))
        else:
            res.append(str(v))
            
        return res

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

# ========== 全局常量 ==========
_CONFIRM_TTL = 300  # 确认令牌有效期（秒）
_MAX_PENDING_TOKENS = 100  # 待确认令牌最大缓存数
_FIRE_MAX_RETRIES = 3  # 提醒触发最大重试次数
_FIRE_RETRY_DELAY = 5  # 提醒触发重试间隔（秒）
_HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）


# ========== 工具函数（纯函数，无副作用）==========

def get_local_now() -> datetime.datetime:
    return datetime.datetime.now()


def parse_time_string(time_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")


def generate_multiple_random_times(
    start_time: datetime.datetime, end_time: datetime.datetime, count: int
) -> List[datetime.datetime]:
    if count <= 0:
        return []
    time_diff = int((end_time - start_time).total_seconds())
    if time_diff <= 0:
        return [start_time] * count
    min_interval = 60
    max_possible = time_diff // min_interval
    actual = min(count, max(1, max_possible))
    if actual < count:
        logger.warning(f"[Reminder] 时间范围不足以容纳 {count} 个提醒，已调整为 {actual} 个")
    seg = time_diff // actual
    times = []
    for i in range(actual):
        seg_start = start_time + datetime.timedelta(seconds=i * seg)
        offset = random.randint(0, max(seg - 1, 0))  # 防止 seg=0 时 randint 报错
        times.append(seg_start + datetime.timedelta(seconds=offset))
    times.sort()
    return times


def determine_random_count(
    random_count=None, random_count_min=None, random_count_max=None
) -> int:
    if random_count and random_count > 0:
        return random_count
    if random_count_min is not None and random_count_max is not None:
        if random_count_min > random_count_max:
            random_count_min, random_count_max = random_count_max, random_count_min
        return random.randint(random_count_min, random_count_max)
    return 1


# ========== 存储层 ==========

class ReminderStorage:
    """带并发锁和原子写入的提醒数据存储"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _unsafe_load(self) -> Dict[str, List[Dict]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[Reminder] 加载数据失败: {e}")
            return {}

    def _unsafe_save(self, data: Dict[str, List[Dict]]):
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), suffix=".tmp"
            )
            try:
                os.write(fd, content.encode("utf-8"))
                os.close(fd)
                if self.path.exists():
                    self.path.unlink()
                os.rename(tmp_path, str(self.path))
            except Exception:
                os.close(fd) if not os.get_inheritable(fd) else None
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error(f"[Reminder] 保存数据失败: {e}")

    async def load(self) -> Dict[str, List[Dict]]:
        async with self._lock:
            return self._unsafe_load()

    async def save(self, data: Dict[str, List[Dict]]):
        async with self._lock:
            self._unsafe_save(data)

    @asynccontextmanager
    async def modify(self) -> AsyncGenerator[Dict[str, List[Dict]], None]:
        """提供原子读-写事务"""
        async with self._lock:
            data = self._unsafe_load()
            yield data
            self._unsafe_save(data)


# ========== 插件主类 ==========

class ReminderPlugin(BasePlugin):
    """
    提醒插件 v4.1 — 稳定性优化版。
    已增量升级 Phase 4: 多级权限隔离与极简管理员全览视图。
    """

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.config = ReminderConfig(**cfg)
        data_dir = get_data_path() / "plugin_data" / "reminder_plugin"
        self._storage = ReminderStorage(data_dir / "reminders.json")
        self._scheduler: Optional[AsyncIOScheduler] = None
        # 待确认删除缓存: token -> {session_id, job_ids, content, expires_at}
        self._pending: Dict[str, Any] = {}
        self._health_task: Optional[asyncio.Task] = None
        self._fire_semaphore = asyncio.Semaphore(3)  # 最多同时 3 个提醒触发写入并发
        
        self._uvicorn_server: Optional[Any] = None
        self._fastapi_task: Optional[asyncio.Task] = None

    async def initialize(self):
        await self._migrate_legacy_data()
        
        self._scheduler = AsyncIOScheduler()
        self._scheduler.start()
        await self._restore_jobs()
        # 启动健康检查后台协程
        self._health_task = asyncio.get_event_loop().create_task(self._health_check_loop())
        logger.info("[Reminder] 插件初始化完成，调度器与健康检查已启动")
        
        if HAS_FASTAPI:
            self._init_fastapi()

    async def terminate(self):
        # 取消健康检查
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            self._health_task = None
        # 关闭调度器
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        # 清空待确认缓存
        self._pending.clear()
        
        if HAS_FASTAPI and self._uvicorn_server:
            logger.info("[Reminder] 正在关闭前端 Web Dashboard 微服务...")
            self._uvicorn_server.should_exit = True
            if self._fastapi_task and not self._fastapi_task.done():
                try:
                    import asyncio
                    await asyncio.wait_for(self._fastapi_task, timeout=2.0)
                except Exception:
                    self._fastapi_task.cancel()
        
        logger.info("[Reminder] 插件已终止")

    # ──────── 内部调度辅助 ────────

    def _init_fastapi(self):
        """挂载独立的 FastAPI 高性能子节点"""
        app = FastAPI(title="KiraAI Reminder Web Dashboard")
        
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/api/sessions")
        async def api_get_sessions():
            try:
                data = await self._storage.load()
                # 仅发送包含数据的基站，并提取基站内的活跃跨界用户树 (User Tree)
                sessions = []
                for k, v in data.items():
                    if len(v) > 0:
                        creators = {}
                        for r in v:
                            uid = r.get("creator_id", "unknown")
                            uname = r.get("creator_name", "未知")
                            # 合并并以新名称为主
                            if uid not in creators:
                                creators[uid] = uname
                            elif creators[uid] in ("未知", "legacy_user") and uname not in ("未知", "legacy_user"):
                                creators[uid] = uname
                        
                        users_list = [{"id": uid, "name": uname} for uid, uname in creators.items()]
                        sessions.append({"id": k, "count": len(v), "users": users_list})
                        
                return JSONResponse({"status": "ok", "data": sessions})
            except Exception as e:
                return JSONResponse({"status": "error", "msg": str(e)}, status_code=500)

        @app.get("/api/reminders/{session_id}")
        async def api_get_reminders(session_id: str):
            try:
                # 兼容前端传递 uri encoded 字符
                from urllib.parse import unquote
                sid = unquote(session_id)
                data = await self._storage.load()
                reminders = data.get(sid, [])
                return JSONResponse({"status": "ok", "data": reminders})
            except Exception as e:
                return JSONResponse({"status": "error", "msg": str(e)}, status_code=500)

        @app.post("/api/reminders/{action}")
        async def api_action_reminders(action: str, payload: dict):
            sid = payload.get("session_id")
            job_id = payload.get("job_id")
            uid = payload.get("user_id", "web_admin_superuser")
            force = payload.get("force", True) # Web端默认赋予强行覆盖权限
            
            if not sid or not job_id:
                raise HTTPException(status_code=400, detail="缺少必要参数")

            # 模拟事件结构以复用现有的最强原生鉴权体系
            class DummyWebEvent:
                class DummySender:
                    def __init__(self, _uid):
                        self.user_id = _uid
                        self.nickname = "Web UI 用户"
                class DummyMsg:
                    def __init__(self, _uid):
                        self.sender = DummyWebEvent.DummySender(_uid)
                def __init__(self, _sid, _uid):
                    self.sid = _sid
                    self.message = DummyWebEvent.DummyMsg(_uid)

            fake_event = DummyWebEvent(sid, uid)

            if action == "delete":
                res = await self.delete_reminder(fake_event, job_id=job_id, force=force)
                return {"status": "ok" if ("❌" not in res and "拒绝" not in res) else "error", "msg": res}
            elif action == "pause":
                res = await self.pause_reminder(fake_event, job_id=job_id)
                return {"status": "ok" if ("❌" not in res and "拒绝" not in res) else "error", "msg": res}
            elif action == "resume":
                res = await self.resume_reminder(fake_event, job_id=job_id)
                return {"status": "ok" if ("❌" not in res and "拒绝" not in res) else "error", "msg": res}
            else:
                raise HTTPException(status_code=400, detail="无效动作")

        @app.get("/web")
        async def api_dashboard_ui():
            try:
                html_path = Path(__file__).parent / "index.html"
                content = html_path.read_text(encoding="utf-8")
                return HTMLResponse(content)
            except Exception as e:
                return HTMLResponse(f"<h2 style='color:red;'>[KiraAI] UI 加载失败: {e}</h2>", status_code=500)

        # 端口自动探活：从配置端口开始，最多尝试 10 个端口
        import socket as _socket
        base_port = self.config.web_port
        bound_port = None
        for _p in range(base_port, base_port + 10):
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                if _s.connect_ex(("127.0.0.1", _p)) != 0:
                    bound_port = _p
                    break
        if bound_port is None:
            logger.error(f"[Reminder] 端口 {base_port}~{base_port+9} 均被占用，Web 大屏启动失败！")
            return
        
        if bound_port != base_port:
            logger.warning(f"[Reminder] 端口 {base_port} 已被占用，自动切换至 {bound_port}")

        config = uvicorn.Config(app, host="0.0.0.0", port=bound_port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        
        async def _run_uvicorn():
            try:
                await self._uvicorn_server.serve()
            except asyncio.CancelledError:
                pass
                
        self._fastapi_task = asyncio.get_event_loop().create_task(_run_uvicorn())
        
        # 打印本机所有局域网 IP 入口（含实际绑定端口）
        try:
            import socket
            hostname = socket.gethostname()
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET)
            ips = []
            for item in addr_info:
                ip = item[4][0]
                if ip not in ips and not ip.startswith("127."):
                    ips.append(ip)
            
            ip_logs = " | ".join([f"http://{ip}:{bound_port}/web" for ip in ips])
            logger.info(f"[Reminder] ✅ Web 大屏已启动！本机访问：http://127.0.0.1:{bound_port}/web")
            if ip_logs:
                logger.info(f"[Reminder] 🌐 局域网跨设备访问：{ip_logs}")
        except Exception:
            logger.info(f"[Reminder] ✅ Web 大屏已启动！访问：http://127.0.0.1:{bound_port}/web")

    async def _migrate_legacy_data(self):
        """升级旧版本数据结构，注入 creator_id、creator_name 等"""
        async with self._storage.modify() as data:
            migrated = 0
            for sid, reminders in data.items():
                for r in reminders:
                    if "creator_id" not in r:
                        r["creator_id"] = "legacy_user"
                        r["creator_name"] = "未知"
                        r["session_type"] = "gm" if "gm:" in sid else "dm"
                        migrated += 1
            if migrated > 0:
                logger.info(f"[Reminder] 成功迁移/升维了 {migrated} 条旧版提醒数据")

    def _get_creator_info(self, event) -> Dict[str, str]:
        """从事件中提取发信人真实身份
        
        注意：当传入 KiraMessageBatchEvent 时，使用 messages[-1]（最后一条消息的发送人），
        与框架内部 is_group_message()、self_id 等属性的取值保持一致，
        确保在多消息批处理场景下创建人归属始终正确。
        """
        creator_id = "unknown"
        creator_name = "未知"
        try:
            if hasattr(event, "message") and hasattr(event.message, "sender"):
                # KiraMessageEvent（单条消息）
                creator_id = str(event.message.sender.user_id)
                creator_name = event.message.sender.nickname
            elif hasattr(event, "messages") and event.messages:
                # KiraMessageBatchEvent（批量消息）：取 messages[-1]（触发 LLM 的最后一条）
                last_msg = event.messages[-1]
                if hasattr(last_msg, "sender"):
                    creator_id = str(last_msg.sender.user_id)
                    creator_name = last_msg.sender.nickname
        except Exception as e:
            logger.debug(f"[Reminder] _get_creator_info 提取失败: {e}")
        logger.debug(f"[Reminder] creator resolved → id={creator_id} name={creator_name}")
        return {"creator_id": creator_id, "creator_name": creator_name}

    def _check_permission(self, event, target_reminder: Dict) -> bool:
        """鉴权网：判定是否有权修改或删除"""
        creator_id = target_reminder.get("creator_id")
        if creator_id in ("legacy_user", "unknown"):
            return True  # 遗留数据豁免
        
        current_id = self._get_creator_info(event)["creator_id"]
        # 自己创建的无条件放行
        if current_id == creator_id:
            return True
        
        # 判断如果是纯私聊场景（防止 session_id 包含其账号的伪造场景，简单做个鉴别）
        sid = getattr(event, "sid", "")
        if ":dm:" in sid and current_id in sid:
             return True
             
        # 支持扩展超管放行逻辑，例如 if current_id in SUPERUSERS: return True
        return self._is_admin_user(event)

    def _is_admin_user(self, event) -> bool:
        """全局统御视角的管理员身份判定"""
        creator_info = self._get_creator_info(event)
        current_id = creator_info.get("creator_id", "")
        # 网页大屏身份特批放行与系统级超管名单验证
        if current_id == "web_admin_superuser" or current_id in self.config.admin_users:
            return True
        return False

    async def _health_check_loop(self):
        """定期检查调度器状态，异常时自动重启"""
        try:
            while True:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
                if self._scheduler and not self._scheduler.running:
                    logger.error("[Reminder] 调度器已停止，尝试重启...")
                    try:
                        self._scheduler.start()
                        await self._restore_jobs()
                        logger.info("[Reminder] 调度器重启成功")
                    except Exception as e:
                        logger.error(f"[Reminder] 调度器重启失败: {e}")
        except asyncio.CancelledError:
            logger.debug("[Reminder] 健康检查任务已取消")
        except Exception as e:
            logger.error(f"[Reminder] 健康检查异常: {e}")

    async def _restore_jobs(self):
        """启动时从持久化存储恢复提醒任务，同时清理过期的一次性提醒"""
        async with self._storage.modify() as data:
            now = get_local_now()
            restored = 0
            expired = 0
            for sid, reminders in list(data.items()):
                kept = []
                for r in reminders:
                    try:
                        trigger_time = parse_time_string(r["time"])
                        repeat = r.get("repeat", "none")
                        if repeat != "none":
                            # 重复提醒始终恢复
                            if not r.get("paused"):
                                self._add_job(sid, r)
                            kept.append(r)
                            restored += 1
                        elif trigger_time > now:
                            # 未过期的一次性提醒
                            if not r.get("paused"):
                                self._add_job(sid, r)
                            kept.append(r)
                            restored += 1
                        else:
                            # 过期的一次性提醒 → 丢弃
                            expired += 1
                    except Exception as e:
                        logger.warning(f"[Reminder] 恢复任务失败: {e}")
                        kept.append(r)  # 解析失败的保留，不丢数据
                data[sid] = kept
            
            if expired:
                logger.info(f"[Reminder] 清理了 {expired} 条过期一次性提醒")
            if restored:
                logger.info(f"[Reminder] 已恢复 {restored} 个提醒任务")

    def _add_job(self, sid: str, r: Dict):
        """向调度器添加一个任务"""
        if not self._scheduler:
            return
            
        trigger_time = parse_time_string(r["time"])
        repeat = r.get("repeat", "none")
        job_id = r.get("job_id", "")

        if repeat == "none":
            trigger = DateTrigger(run_date=trigger_time)
        elif repeat == "daily":
            trigger = CronTrigger(hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "weekly":
            trigger = CronTrigger(day_of_week=trigger_time.weekday(),
                                  hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "monthly":
            trigger = CronTrigger(day=trigger_time.day,
                                  hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "yearly":
            trigger = CronTrigger(month=trigger_time.month, day=trigger_time.day,
                                  hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "interval":
            interval_minutes = r.get("interval_minutes", 30)
            # 加固：若基准时间已过，将 start_date 调整为当前时间+1个间隔，避免意外立即触发
            now = get_local_now()
            safe_start = trigger_time if trigger_time > now else now + datetime.timedelta(minutes=interval_minutes)
            trigger = IntervalTrigger(minutes=interval_minutes, start_date=safe_start)
        else:
            trigger = DateTrigger(run_date=trigger_time)

        try:
            self._scheduler.add_job(
                self._fire_reminder,
                trigger=trigger,
                id=job_id,
                args=[sid, r],
                replace_existing=True,
                misfire_grace_time=300,
            )
        except Exception as e:
            logger.warning(f"[Reminder] 添加调度任务失败 job_id={job_id}: {e}")

    async def _fire_reminder(self, sid: str, r: Dict):
        """提醒触发时发送消息到目标会话，带重试机制与并发写入保护"""
        async with self._fire_semaphore:  # 最多同时 3 个提醒执行，避免竞争写入
            content = r.get("content", "")
            job_id = r.get("job_id", "")
            repeat = r.get("repeat", "none")
            category = r.get("category", "")
            action = r.get("action", "")
            logger.info(f"[Reminder] 触发提醒 sid={sid} content={content}")
            from core.chat.message_elements import Text
            
            cat_str = f"[{category}] " if category else ""
            msg = f"\u23f0 {cat_str}提醒：{content}"
            if action:
                msg += f"\n\U0001f449 自动动作指令：{action}\n(请优先执行上述动作建议并回复结果)"
                
            chain = MessageChain([Text(msg)])
            sent = False
            for attempt in range(1, _FIRE_MAX_RETRIES + 1):
                try:
                    await self.ctx.publish_notice(session=sid, chain=chain)
                    sent = True
                    break
                except Exception as e:
                    logger.warning(
                        f"[Reminder] 发送提醒失败 (尝试 {attempt}/{_FIRE_MAX_RETRIES}): {e}"
                    )
                    if attempt < _FIRE_MAX_RETRIES:
                        await asyncio.sleep(_FIRE_RETRY_DELAY)
            if not sent:
                logger.error(f"[Reminder] 提醒发送彻底失败 sid={sid} job_id={job_id}")
                try:
                    async with self._storage.modify() as data:
                        for rv in data.get(sid, []):
                            if rv.get("job_id") == job_id:
                                rv["failed_at"] = get_local_now().strftime("%Y-%m-%d %H:%M")
                                break
                except Exception:
                    pass
            # 一次性提醒触发后清理存储记录
            if repeat == "none":
                try:
                    async with self._storage.modify() as data:
                        if sid in data:
                            data[sid] = [rv for rv in data[sid] if rv.get("job_id") != job_id]
                except Exception as e:
                    logger.warning(f"[Reminder] 清理已触发提醒失败: {e}")

    @on.im_message(priority=Priority.HIGH)
    async def handle_quick_command(self, event: KiraMessageEvent):
        """绕过 LLM 的极速指令响应拦截器"""
        if not event.message.chain or event.is_stopped:
            return

        # 提取纯文本
        text = " ".join([m.text.strip() for m in event.message.chain if isinstance(m, Text)]).strip()
        if not text:
            return

        sid = event.session.sid
        
        # --- 读取当前会话已有提醒列表用于下发的卡片绘制或序号映射 ---
        try:
            data = await self._storage.load()
            reminders = data.get(sid, [])
        except Exception:
            reminders = []

        # 0. 极简帮助菜单
        if text in ["/r help", "/remind help", "-r help", "/待办 帮助", "/提醒 帮助", "/r h"]:
            help_str = (
                "[ 待办控制台 ]\n"
                "/r : 查看列表\n"
                "/r add 时间 内容\n"
                "/r rm <序号> : 删除\n"
                "/r pause <序号> : 暂停\n"
                "/r resume <序号> : 恢复\n"
                "/r view <序号> : 详情\n"
                "*(别名: add为a/+, rm为-, view为v/i)*\n\n"
                "[ 全景视图（超管） ]\n"
                "/r all : 跨人员查看所有事项\n"
                "/r view @xxx @某人 : 聚焦特定成员"
            )
            await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(help_str)]))
            event.discard(force=True)
            event.stop()
            return

        # 1. 独立极简卡片格式化 (Mac/CLI 高级质感)
        list_triggers = ["/r", "/remind", "/提", "/待办", "-r list"]
        if text in list_triggers or text in ["/r all", "/待办全部"]:
            is_global_view = text in ["/r all", "/待办全部"]
            
            if is_global_view and not self._is_admin_user(event):
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("❌ 拒绝执行：您没有全局查看权限。")]))
                event.discard(force=True)
                event.stop()
                return

            # 数据过滤隔离
            current_uid = self._get_creator_info(event)["creator_id"]
            filtered_reminders = []
            for r in reminders:
                if is_global_view or r.get("creator_id") in (current_uid, "legacy_user", "unknown"):
                    filtered_reminders.append(r)

            if not filtered_reminders:
                view_type = "全系群落" if is_global_view else "您的私有"
                result_str = f"[ 待办列表 ({view_type}) ]\n空空如也"
            else:
                title = "[ 全景穿梭视图 (上帝模式) ]" if is_global_view else "[ 个人私密待办列表 ]"
                lines = [title]
                
                # 如果是上帝模式，最好按人物聚类
                if is_global_view:
                    from itertools import groupby
                    sorted_rems = sorted(filtered_reminders, key=lambda x: x.get('creator_name', '未知'))
                    for creator, group in groupby(sorted_rems, key=lambda x: x.get('creator_name', '未知')):
                        lines.append(f"\n👥 {creator}")
                        for r in group:
                            idx = reminders.index(r) + 1
                            lines.append(f"  {idx}. {r.get('content', '')[:20]}.. ({r.get('time', '')})")
                else:
                    for r in filtered_reminders:
                        i = reminders.index(r) + 1  # 保持原有索引用于极短删除映射
                        status = " (已暂停)" if r.get("paused") else ""
                        cate_str = f"[{r['category']}] " if r.get("category") else ""
                        
                        rep_map = {"none": "单次", "daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年", "interval": f"每{r.get('interval_minutes', 30)}分"}
                        rep_str = f" · {rep_map.get(r.get('repeat', 'none'), '单次')}"
                        imp_str = " ⭐" if r.get("important") else ""
                        
                        lines.append("")
                        lines.append(f"{i}. {r.get('time', '未知')}{rep_str}")
                        
                        raw_content = r.get('content', '')
                        disp_content = raw_content[:40] + "..." if len(raw_content) > 40 else raw_content
                        lines.append(f"   {cate_str}{disp_content}{imp_str}{status}")
                        
                result_str = "\n".join(lines)
            
            await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
            event.discard(force=True)
            event.stop()
            return

        # 1.5 查阅特定人员清单
        if text.startswith("/待办查 ") or text.startswith("/r view @"):
            if not self._is_admin_user(event):
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("❌ 拒绝执行：这是管理员专属探查工具。")]))
                event.discard(force=True)
                event.stop()
                return
                
            match_name = text.split(" ", 1)[1].strip().lstrip("@")
            targets = [r for r in reminders if match_name.lower() in r.get("creator_name", "").lower()]
            if not targets:
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(f"未找到与 [{match_name}] 相关的日程。")]))
                event.discard(force=True)
                event.stop()
                return
                
            lines = [f"🔍 [ 聚焦透视: {match_name} ]"]
            for r in targets:
                i = reminders.index(r) + 1
                lines.append(f"\n{i}. {r.get('time', '')} - {r.get('content', '')[:30]}...")
            
            await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("\n".join(lines))]))
            event.discard(force=True)
            event.stop()
            return

        # 2. 规范化指令前缀，解析子命令与参数
        parts = text.split()
        if len(parts) >= 2:
            action_cmd = parts[0]
            action_arg = parts[1]
            
            # 若第一段是体系主词 (如 /r, /待办)，则将第二段视作子命令
            if action_cmd in list_triggers and len(parts) >= 3:
                # 兼容中文及英文短拼
                sub_cmd_map = {
                    "删除": "/rm", "rm": "/rm", "d": "/rm", "-": "/rm",
                    "暂停": "/pause", "pause": "/pause", "p": "/pause",
                    "恢复": "/resume", "resume": "/resume", "re": "/resume",
                    "查看": "/view", "view": "/view", "v": "/view", "i": "/view", "info": "/view"
                }
                if parts[1] in sub_cmd_map:
                    action_cmd = sub_cmd_map[parts[1]]
                    action_arg = parts[2]

            # 3. 解析操作目标：支持用极短序号代替长 job_id
            job_id_target = action_arg
            if action_arg.isdigit():
                idx = int(action_arg) - 1
                if 0 <= idx < len(reminders):
                    job_id_target = reminders[idx].get("job_id")
                else:
                    msg = f"找不到序号: {action_arg}"
                    if action_cmd in ["/rm", "-rm", "/删除", "/pause", "-pause", "/暂停", "/resume", "-resume", "/恢复", "/view", "-view", "/查看", "/v", "/i"]:
                        await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(msg)]))
                        event.discard(force=True)
                        event.stop()
                        return

            # 执行删除指令
            if action_cmd in ["/rm", "-rm", "/删除"]:
                result_str = await self.delete_reminder(event, job_id=job_id_target)
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return

            # 执行暂停指令
            if action_cmd in ["/pause", "-pause", "/暂停"]:
                result_str = await self.pause_reminder(event, job_id=job_id_target)
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return
                
            # 执行恢复指令
            if action_cmd in ["/resume", "-resume", "/恢复"]:
                result_str = await self.resume_reminder(event, job_id=job_id_target)
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return

            # 执行查看详情指令
            if action_cmd in ["/view", "-view", "/查看", "/v", "-v", "/i"]:
                target_r = next((r for r in reminders if r.get("job_id") == job_id_target), None)
                if not target_r:
                    result_str = f"找不到任务: {job_id_target}"
                else:
                    rep_map = {"none": "单次", "daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年", "interval": f"每{target_r.get('interval_minutes', 30)}分"}
                    rep_str = rep_map.get(target_r.get('repeat', 'none'), '单次')
                    status = "已暂停" if target_r.get("paused") else "活动中"
                    cate = target_r.get('category', '无分类')
                    imp = "是" if target_r.get('important') else "否"
                    full_content = target_r.get('content', '')
                    result_str = (
                        f"[ 任务详情 ]\n"
                        f"时间: {target_r.get('time', '未知')}\n"
                        f"频率: {rep_str}\n"
                        f"状态: {status}\n"
                        f"分类: {cate}\n"
                        f"重要: {imp}\n"
                        f"创建: {target_r.get('created_at', '未知')}\n"
                        f"创建人: {target_r.get('creator_name', '未知')}\n"
                        f"内容:\n{full_content}"
                    )
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return

        # 4. 处理快捷创建指令 (/r add, /添加待办, 等)
        cmd_is_add = False
        payload = ""
        if text.startswith("/r add "):
            cmd_is_add = True
            payload = text[len("/r add "):].strip()
        elif text.startswith("/r a "):
            cmd_is_add = True
            payload = text[len("/r a "):].strip()
        elif text.startswith("/r + "):
            cmd_is_add = True
            payload = text[len("/r + "):].strip()
        elif text.startswith("/添加待办 ") or text.startswith("/待办 添加 "):
            cmd_is_add = True
            prefix = "/添加待办 " if text.startswith("/添加待办 ") else "/待办 添加 "
            payload = text[len(prefix):].strip()

        if cmd_is_add:
            parts = payload.split(" ", 2)
            if len(parts) >= 3:
                time_str = f"{parts[0]} {parts[1]}"
                content = parts[2]
                try:
                    if "-" in time_str and ":" in time_str:
                        res = await self.set_reminder(event, content=content, time=time_str)
                        success_msg = f"已添加: {content}\n[{time_str}]"
                        await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(success_msg)]))
                    else:
                        await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("时间格式需为 YYYY-MM-DD HH:MM")]))
                except Exception as e:
                    await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(f"出错了: {e}")]))
            else:
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("格式: /r add 2026-03-20 08:30 内容")]))
            
            event.discard(force=True)
            event.stop()
            return

    def _get_sid(self, event: KiraMessageBatchEvent) -> str:
        """获取会话 ID 并校验格式（adapter:type:id）"""
        sid = getattr(event, "sid", "default")
        if sid and sid != "default" and len(sid.split(":")) < 3:
            logger.warning(f"[Reminder] session_id 格式异常: {sid}")
        return sid

    def _cleanup_tokens(self):
        """清理过期令牌并限制最大缓存数量"""
        now = time.time()
        expired = [k for k, v in self._pending.items() if v["expires_at"] < now]
        for k in expired:
            del self._pending[k]
        # 超出上限时按过期时间淘汰最旧的
        if len(self._pending) > _MAX_PENDING_TOKENS:
            sorted_keys = sorted(
                self._pending, key=lambda k: self._pending[k]["expires_at"]
            )
            for k in sorted_keys[: len(self._pending) - _MAX_PENDING_TOKENS]:
                del self._pending[k]

    def _create_token(self, sid: str, job_ids: List[str], content: str) -> str:
        token = uuid.uuid4().hex[:12]
        self._pending[token] = {"session_id": sid, "job_ids": job_ids,
                                "content": content, "expires_at": time.time() + _CONFIRM_TTL}
        return token

    # ──────── 工具方法 ────────

    @register_tool(
        name="set_reminder",
        description=(
            "设置提醒。支持一次性/重复/间隔/随机时间提醒。"
            "time 必须为 'YYYY-MM-DD HH:MM' 格式。成功后返回 job_id 供后续管理使用。"
        ),
        params={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "提醒内容"},
                "time": {"type": "string", "description": "提醒时间，格式 YYYY-MM-DD HH:MM"},
                "repeat": {"type": "string",
                           "enum": ["none", "daily", "weekly", "monthly", "yearly", "interval"],
                           "description": "重复类型，默认 none"},
                "interval_minutes": {"type": "integer",
                                     "description": "间隔提醒的分钟数（repeat=interval 时必填）"},
                "category": {"type": "string", "description": "提醒分类（如工作/学习等）"},
                "action": {"type": "string", "description": "触发时期望执行的动作指令，可借此调用其他工具"},
                "time_range_end": {"type": "string",
                                   "description": "随机提醒结束时间，设置后在 time~time_range_end 内随机触发"},
                "random_count": {"type": "integer", "description": "随机提醒次数（固定值）"},
                "random_count_min": {"type": "integer", "description": "随机次数最小值"},
                "random_count_max": {"type": "integer", "description": "随机次数最大值"},
            },
            "required": ["content", "time"],
        }
    )
    async def set_reminder(self, event: KiraMessageBatchEvent, content: str, time: str,
                           repeat: str = "none", interval_minutes: Optional[int] = None,
                           category: Optional[str] = None, action: Optional[str] = None,
                           time_range_end: Optional[str] = None,
                           random_count: Optional[int] = None,
                           random_count_min: Optional[int] = None,
                           random_count_max: Optional[int] = None, **kwargs) -> str:
        try:
            if repeat not in ("none", "daily", "weekly", "monthly", "yearly", "interval"):
                return "❌ repeat 参数无效"
            if repeat == "interval":
                if not interval_minutes:
                    return "❌ 间隔提醒需要指定 interval_minutes"
                if not (1 <= interval_minutes <= 1440):
                    return "❌ 间隔时间必须在 1~1440 分钟之间"
            try:
                start_time = parse_time_string(time)
            except ValueError:
                return "❌ 时间格式错误，请使用 YYYY-MM-DD HH:MM 格式"
            sid = self._get_sid(event)
            batch_ts = get_local_now().strftime("%Y%m%d%H%M%S%f")
            creator_info = self._get_creator_info(event)
            sess_type = "gm" if ":gm:" in sid else "dm"
            
            # --- 时间前置校验（避免不必要的锁占用） ---
            if time_range_end:
                try:
                    end_time = parse_time_string(time_range_end)
                except ValueError:
                    return "❌ 结束时间格式错误"
                if end_time <= start_time:
                    return "❌ 结束时间必须晚于开始时间"
                final_count = determine_random_count(random_count, random_count_min, random_count_max)
                trigger_times = generate_multiple_random_times(start_time, end_time, final_count)
                trigger_times = [t for t in trigger_times if t > get_local_now()]
                if not trigger_times:
                    return "❌ 所有随机时间点都已过去"
            elif repeat == "none" and start_time < get_local_now():
                return "❌ 不能设置过去的时间"

            async with self._storage.modify() as data:
                data.setdefault(sid, [])
                
                if time_range_end:
                    for i, t in enumerate(trigger_times):
                        job_id = f"reminder_{sid}_{batch_ts}_{i}"
                        r: Dict[str, Any] = {
                            "content": content, "time": t.strftime("%Y-%m-%d %H:%M"),
                            "repeat": "none", "job_id": job_id,
                            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M"),
                            "is_random": True, "random_batch_id": batch_ts,
                            "random_index": i + 1, "random_total": len(trigger_times),
                            "time_range": {"start": time, "end": time_range_end},
                            "creator_id": creator_info["creator_id"],
                            "creator_name": creator_info["creator_name"],
                            "session_type": sess_type
                        }
                        if category: r["category"] = category
                        if action: r["action"] = action
                        data[sid].append(r)
                        self._add_job(sid, r)
                    times_str = "\n".join(f"  {i+1}. {t.strftime('%Y-%m-%d %H:%M')}"
                                           for i, t in enumerate(trigger_times))
                    return (f"已添加随机待办: {content} (共{len(trigger_times)}次)\n"
                            f"时间列表:\n{times_str}")
                            
                # --- 非随机多签分支 ---
                job_id = f"reminder_{sid}_{batch_ts}"
                r = {"content": content, "time": start_time.strftime("%Y-%m-%d %H:%M"),
                     "repeat": repeat, "job_id": job_id,
                     "created_at": get_local_now().strftime("%Y-%m-%d %H:%M"),
                     "creator_id": creator_info["creator_id"],
                     "creator_name": creator_info["creator_name"],
                     "session_type": sess_type}
                if repeat == "interval":
                    r["interval_minutes"] = interval_minutes
                if category: r["category"] = category
                if action: r["action"] = action
                data[sid].append(r)
                self._add_job(sid, r)
                
            repeat_text = {"none": "", "daily": " (每天)", "weekly": " (每周)",
                           "monthly": " (每月)", "yearly": " (每年)",
                           "interval": f" (每{interval_minutes}分钟)"}.get(repeat, "")
            return (f"已添加: {content}\n"
                    f"[{start_time.strftime('%Y-%m-%d %H:%M')}{repeat_text}]")
        except Exception as e:
            logger.error(f"[Reminder] 设置提醒失败: {e}")
            return f"设置出错: {e}"

    @register_tool(
        name="list_reminders",
        description="列出当前会话的所有提醒，包括 job_id、重要标记、重复类型等。删除/标记前必须先调用此工具。",
        params={"type": "object", "properties": {}, "required": []}
    )
    async def list_reminders(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            data = await self._storage.load()
            reminders = data.get(sid, [])
            is_admin = self._is_admin_user(event)
            current_uid = self._get_creator_info(event)["creator_id"]
            
            filtered = [r for r in reminders if is_admin or r.get("creator_id") in (current_uid, "legacy_user", "unknown")]

            if not filtered:
                return "当前没有任何待办"
            lines = ["📋 您的私有列表 (超管可透视)：\n"] if is_admin else ["📋 您的私有列表：\n"]
            for r in filtered:
                i = reminders.index(r) + 1
                imp = " ⭐重要" if r.get("important") else ""
                paused = " ⏸️已暂停" if r.get("paused") else ""
                cat = f" [{r['category']}]" if r.get("category") else ""
                action = f"\n   🤖动作:{r['action']}" if r.get("action") else ""
                rep = {"none": "", "daily": " [每天]", "weekly": " [每周]",
                       "monthly": " [每月]", "yearly": " [每年]",
                       "interval": f" [每{r.get('interval_minutes',30)}分钟]"}.get(r.get("repeat","none"), "")
                rand = ""
                if r.get("is_random"):
                    tr = r.get("time_range", {})
                    idx, total = r.get("random_index"), r.get("random_total")
                    rand = (f"\n   范围: {tr.get('start','?')} ~ {tr.get('end','?')}"
                            f" [随机 {idx}/{total}]" if total else "")
                lines.append(f"{i}. {r['content']}{cat}{imp}{paused}\n   时间: {r['time']}{rep}{rand}{action}"
                             f"\n   job_id: {r.get('job_id','unknown')}"
                             f"\n   创建人: {r.get('creator_name','未知')}")
            self._cleanup_tokens()
            pending = sum(1 for v in self._pending.values() if v.get("session_id") == sid)
            if pending:
                lines.append(f"\n⏳ 有 {pending} 个重要提醒的删除请求等待确认")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 列出提醒失败: {e}"

    @register_tool(
        name="delete_reminder",
        description="根据 job_id 删除提醒。重要提醒需二次确认。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要删除的提醒 ID"},
                "delete_batch": {"type": "boolean", "description": "是否删除整个随机批次，默认 false"},
            },
            "required": ["job_id"],
        }
    )
    async def delete_reminder(self, event: KiraMessageBatchEvent, job_id: str = "",
                              delete_batch: bool = False, **kwargs) -> str:
        try:
            self._cleanup_tokens()
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                if not reminders:
                    return "当前没有待办"
                reminder = next((r for r in reminders if r.get("job_id") == job_id), None)
                if reminder is None:
                    return f"找不到任务: {job_id}"
                    
                if not self._check_permission(event, reminder):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {reminder.get('creator_name', '未知')})"
                    
                batch_id = reminder.get("random_batch_id")
                targets = ([r for r in reminders if r.get("random_batch_id") == batch_id]
                           if delete_batch and batch_id else [reminder])
                if any(r.get("important") for r in targets):
                    job_ids = [r["job_id"] for r in targets if "job_id" in r]
                    token = self._create_token(sid, job_ids, reminder["content"])
                    return (f"注意: 「{reminder['content']}」是重要提醒\n"
                            f"请确认删除令牌: {token}")
                            
                # 直接删除
                for r in targets:
                    if "job_id" in r:
                        try:
                            if self._scheduler: self._scheduler.remove_job(r["job_id"])
                        except Exception:
                            pass
                            
                if delete_batch and batch_id:
                    data[sid] = [r for r in reminders if r.get("random_batch_id") != batch_id]
                    return f"已批量删除: {reminder['content']} (共{len(targets)}项)"
                
                data[sid] = [r for r in reminders if r.get("job_id") != job_id]
                return f"已删除: {reminder['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="confirm_delete_reminder",
        description="用户明确同意后，使用 delete_reminder 返回的 confirm_token 完成重要提醒删除。",
        params={
            "type": "object",
            "properties": {
                "confirm_token": {"type": "string", "description": "delete_reminder 返回的确认令牌"},
            },
            "required": ["confirm_token"],
        }
    )
    async def confirm_delete_reminder(self, event: KiraMessageBatchEvent,
                                      confirm_token: str = "", **kwargs) -> str:
        try:
            self._cleanup_tokens()
            pending = self._pending.get(confirm_token)
            if not pending:
                return "令牌无效或已过期"
            sid, job_ids, content = pending["session_id"], pending["job_ids"], pending["content"]
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                before = len(reminders)
                data[sid] = [r for r in reminders if r.get("job_id") not in job_ids]
                deleted = before - len(data[sid])
                for jid in job_ids:
                    try:
                        if self._scheduler: self._scheduler.remove_job(jid)
                    except Exception:
                        pass
                del self._pending[confirm_token]
                suffix = f" (共{deleted}项)" if deleted > 1 else ""
                return f"已删除: {content}{suffix}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="mark_reminder_important",
        description="将指定 job_id 的提醒标记为重要，删除时需二次确认。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def mark_reminder_important(self, event: KiraMessageBatchEvent,
                                      job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if r.get("important"):
                    return f"已设为重要: {r['content']}"
                r["important"] = True
                return f"设为重要: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="unmark_reminder_important",
        description="取消指定 job_id 提醒的重要标记。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def unmark_reminder_important(self, event: KiraMessageBatchEvent,
                                        job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if not r.get("important"):
                    return f"并非重要提醒: {r['content']}"
                r["important"] = False
                return f"取消重要标记: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="pause_reminder",
        description="暂停提醒，暂停后的提醒不会触发，但可以随时恢复。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "要暂停的提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def pause_reminder(self, event: KiraMessageBatchEvent, job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if r.get("paused"):
                    return f"已经暂停: {r['content']}"
                r["paused"] = True
                try:
                    if self._scheduler: self._scheduler.remove_job(job_id)
                except Exception:
                    pass
                return f"已暂停: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="resume_reminder",
        description="恢复被暂停的提醒。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "要恢复的提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def resume_reminder(self, event: KiraMessageBatchEvent, job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if not r.get("paused"):
                    return f"已经处于活动状态: {r['content']}"
                
                # 检查是否尝试恢复已经过期的一次性任务
                if r.get("repeat", "none") == "none":
                    trigger_time = parse_time_string(r["time"])
                    if trigger_time <= get_local_now():
                        return f"已过期，无法恢复: {r['content']}"
                
                r["paused"] = False
                self._add_job(sid, r)
                return f"已恢复: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="edit_reminder",
        description="修改已设置的提醒。仅需提供想修改的字段，未提供的字段保持原样。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要修改的提醒 ID"},
                "content": {"type": "string", "description": "新提醒内容（可选）"},
                "time": {"type": "string", "description": "新提醒时间，格式 YYYY-MM-DD HH:MM（可选）"},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly", "yearly", "interval"], "description": "新重复类型（可选）"},
                "interval_minutes": {"type": "integer", "description": "新间隔分钟数（可选）"},
                "category": {"type": "string", "description": "新提醒分类（可选）"},
                "action": {"type": "string", "description": "新自动动作指令（可选）"},
            },
            "required": ["job_id"],
        }
    )
    async def edit_reminder(self, event: KiraMessageBatchEvent, job_id: str = "",
                            content: Optional[str] = None, time: Optional[str] = None,
                            repeat: Optional[str] = None, interval_minutes: Optional[int] = None,
                            category: Optional[str] = None, action: Optional[str] = None, **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            
            # 提前校验时间以免持有锁时做无用功
            new_time_str = time
            new_start_time = None
            if new_time_str is not None:
                try:
                    new_start_time = parse_time_string(new_time_str)
                except ValueError:
                    return "时间格式需为 YYYY-MM-DD HH:MM"

            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r_index = next((i for i, rv in enumerate(reminders) if rv.get("job_id") == job_id), -1)
                if r_index == -1:
                    return f"找不到任务: {job_id}"
                
                r = reminders[r_index]
                if not self._check_permission(event, r):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                    
                if r.get("is_random"):
                    return "不支持修改随机批次提醒"
                    
                new_repeat = repeat if repeat is not None else r.get("repeat", "none")
                if new_repeat not in ("none", "daily", "weekly", "monthly", "yearly", "interval"):
                    return "重复参数无效"
                
                if new_repeat == "interval":
                    new_interval = interval_minutes if interval_minutes is not None else r.get("interval_minutes")
                    if not new_interval:
                        return "缺少间隔时间参数"
                    if not (1 <= new_interval <= 1440):
                        return "间隔时间应在 1~1440 分钟"
                    r["interval_minutes"] = new_interval
                
                if new_time_str is None:
                    new_time_str = r.get("time")
                    new_start_time = parse_time_string(new_time_str)
                    
                if new_repeat == "none" and new_start_time < get_local_now() and time is not None:
                    return "无法设置过去的时间"

                if content is not None: r["content"] = content
                if time is not None: r["time"] = new_time_str
                if repeat is not None: r["repeat"] = new_repeat
                if category is not None: r["category"] = category
                if action is not None: r["action"] = action
                
                # 先移除旧任务
                try:
                    if self._scheduler: self._scheduler.remove_job(job_id)
                except Exception:
                    pass
                    
                # 非暂停状态则重新加入调度器
                if not r.get("paused"):
                    self._add_job(sid, r)
                    
                return f"已更新: {r['content']}\n[{r['time']}]"
        except Exception as e:
            logger.error(f"[Reminder] 编辑提醒失败: {e}")
            return f"修改出错: {e}"
