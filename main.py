import json
import hashlib
import base64
import shutil
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
import inspect

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Image
from astrbot.api.star import Context, Star, StarTools


DEFAULT_KEYWORDS = {
    "你好": "你好呀~",
    "在吗": "我在，有什么事？",
    "帮助": "可用命令：群统计、今日统计、昨日统计、本周统计、月统计、今日发言、查发言、导出活跃榜、清空统计、关键词列表；管理员可用：添加关键词 词=回复、删除关键词 词"
}

DEFAULT_CONFIG = {
    "match_mode": "contains",
    "reply_once_per_message": True,
    "group_whitelist": []
}


class KeywordStatsPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).parent
        self.config_path = self.plugin_dir / "config.json"
        self.data_dir = Path(StarTools.get_data_dir("astrbot_keyword_stats"))
        self.data_dir.mkdir(exist_ok=True)
        self.stats_path = self.data_dir / "stats_data.json"
        self.keywords_path = self.data_dir / "keywords.json"
        self.media_dir = self.data_dir / "keyword_media"
        self.media_dir.mkdir(exist_ok=True)

        file_config = self._load_json(self.config_path, DEFAULT_CONFIG)
        self.config = self._merge_config(file_config, self.config)
        self.keywords = self._load_json(self.keywords_path, DEFAULT_KEYWORDS)
        self.stats = self._load_json(self.stats_path, {})

    def _load_json(self, path: Path, default):
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[astrbot_keyword_stats] 读取文件失败: {path} - {e}")
        return default

    def _save_stats(self):
        try:
            with self.stats_path.open("w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[astrbot_keyword_stats] 保存统计失败: {e}")

    def _save_keywords(self):
        try:
            with self.keywords_path.open("w", encoding="utf-8") as f:
                json.dump(self.keywords, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[astrbot_keyword_stats] 保存关键词失败: {e}")

    def _merge_config(self, file_config: dict, runtime_config: dict) -> dict:
        result = dict(DEFAULT_CONFIG)
        result.update(file_config or {})
        result.update(runtime_config or {})

        result["match_mode"] = str(result.get("match_mode", "contains")).strip().lower()
        if result["match_mode"] not in {"contains", "exact"}:
            result["match_mode"] = "contains"

        result["reply_once_per_message"] = bool(result.get("reply_once_per_message", True))
        result["group_whitelist"] = [str(item) for item in result.get("group_whitelist", []) or []]
        return result

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _yesterday(self) -> str:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    def _week_dates(self) -> list[str]:
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    def _month_dates(self) -> list[str]:
        today = datetime.now()
        first_day = today.replace(day=1)
        dates = []
        current = first_day
        while current.month == first_day.month:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        return dates

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            # Some adapters expose a get_group_id() method.
            if hasattr(event, "get_group_id") and callable(event.get_group_id):
                val = event.get_group_id()
                if val:
                    return str(val).strip()
        except Exception:
            pass

        candidates = [
            getattr(event, "group_id", None),
            getattr(event.message_obj, "group_id", None),
            getattr(event.message_obj, "group", None),
        ]
        for val in candidates:
            if val:
                return str(val).strip()
        return ""

    def _is_group_whitelisted(self, group_id: str) -> bool:
        whitelist = self.config.get("group_whitelist", []) or []
        if not whitelist:
            return True
        if not group_id:
            return False
        return str(group_id) in {str(item) for item in whitelist}

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            sender = getattr(event.message_obj, "sender", None)
            return str(getattr(sender, "user_id", "unknown"))

    def _is_admin_or_owner(self, event: AstrMessageEvent) -> bool:
        sender = getattr(event.message_obj, "sender", None)
        candidates = [
            getattr(sender, "role", None),
            getattr(event.message_obj, "sender_role", None),
            getattr(event, "sender_role", None),
            getattr(event, "role", None),
        ]
        for role in candidates:
            if str(role or "").lower() in {"admin", "owner"}:
                return True

        # Some adapters expose permission flags instead of a string role.
        bool_flags = [
            getattr(sender, "is_admin", None),
            getattr(sender, "is_owner", None),
            getattr(event.message_obj, "is_admin", None),
            getattr(event.message_obj, "is_owner", None),
        ]
        if any(flag is True for flag in bool_flags):
            return True

        logger.info(
            "[astrbot_keyword_stats] 无法确认管理员身份, sender_role=%s event_role=%s sender=%s",
            getattr(event.message_obj, "sender_role", None),
            getattr(event, "role", None),
            sender,
        )
        return False

    def _ensure_group_stats(self, group_id: str):
        if group_id not in self.stats:
            self.stats[group_id] = {
                "total_messages": 0,
                "users": {},
                "user_names": {},
                "daily": {},
            }

    def _get_display_name(self, event: AstrMessageEvent) -> str:
        sender = getattr(event.message_obj, "sender", None)
        candidates = [
            getattr(sender, "card", None),
            getattr(sender, "nickname", None),
            getattr(sender, "remark", None),
            getattr(event.message_obj, "sender_card", None),
            getattr(event.message_obj, "sender_nickname", None),
        ]
        for name in candidates:
            text = str(name or "").strip()
            if text:
                return text
        return self._get_user_id(event)

    def _record_group_message(self, group_id: str, user_id: str, display_name: str):
        self._ensure_group_stats(group_id)
        group_stats = self.stats[group_id]
        group_stats["total_messages"] += 1
        group_stats["users"][user_id] = group_stats["users"].get(user_id, 0) + 1
        group_stats.setdefault("user_names", {})[user_id] = display_name

        today = self._today()
        if today not in group_stats["daily"]:
            group_stats["daily"][today] = {
                "total_messages": 0,
                "users": {},
            }
        group_stats["daily"][today]["total_messages"] += 1
        group_stats["daily"][today]["users"][user_id] = (
            group_stats["daily"][today]["users"].get(user_id, 0) + 1
        )
        self._save_stats()

    def _format_user_label(self, user_id: str, user_names: dict | None = None) -> str:
        user_names = user_names or {}
        name = str(user_names.get(user_id, "") or "").strip()
        if name and name != user_id:
            return f"{name}（{user_id}）"
        return user_id

    def _format_ranking(self, user_stats: dict, user_names: dict | None = None, top_n: int = 10) -> str:
        if not user_stats:
            return "暂无数据"
        user_names = user_names or {}
        sorted_users = sorted(user_stats.items(), key=lambda item: item[1], reverse=True)[:top_n]
        return "\n".join(
            f"{index}. {self._format_user_label(user_id, user_names)}: {count} 条"
            for index, (user_id, count) in enumerate(sorted_users, start=1)
        )

    def _extract_mentioned_user_id(self, event: AstrMessageEvent, message_str: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        for attr in ["message", "messages", "raw_message", "segments"]:
            segments = getattr(message_obj, attr, None)
            if isinstance(segments, list):
                for seg in segments:
                    if isinstance(seg, dict):
                        seg_type = seg.get("type")
                        seg_data = seg.get("data", {})
                        if seg_type == "at" and isinstance(seg_data, dict):
                            qq = seg_data.get("qq") or seg_data.get("id") or seg_data.get("user_id")
                            if qq:
                                return str(qq)

        if "@" in message_str:
            import re
            match = re.search(r"@(\d{5,})", message_str)
            if match:
                return match.group(1)
        return ""

    def _find_user_id(self, group_id: str, query: str) -> str:
        query = str(query or "").strip()
        if not query:
            return ""
        self._ensure_group_stats(group_id)
        group_stats = self.stats[group_id]
        users = group_stats.get("users", {})
        user_names = group_stats.get("user_names", {})

        if query in users:
            return query

        normalized = query.lstrip("@").strip()
        if normalized in users:
            return normalized

        for user_id, name in user_names.items():
            if str(name).strip() == normalized:
                return user_id
        return ""

    def _build_single_user_stats_message(self, group_id: str, user_id: str) -> str:
        self._ensure_group_stats(group_id)
        group_stats = self.stats[group_id]
        user_names = group_stats.get("user_names", {})
        total_count = group_stats.get("users", {}).get(user_id, 0)
        today = self._today()
        yesterday = self._yesterday()
        today_count = group_stats.get("daily", {}).get(today, {"users": {}}).get("users", {}).get(user_id, 0)
        yesterday_count = group_stats.get("daily", {}).get(yesterday, {"users": {}}).get("users", {}).get(user_id, 0)

        week_count = 0
        for day in self._week_dates():
            week_count += group_stats.get("daily", {}).get(day, {"users": {}}).get("users", {}).get(user_id, 0)

        return (
            f"成员发言统计\n"
            f"成员: {self._format_user_label(user_id, user_names)}\n"
            f"累计发言: {total_count} 条\n"
            f"今日发言: {today_count} 条\n"
            f"昨日发言: {yesterday_count} 条\n"
            f"本周发言: {week_count} 条"
        )

    def _get_day_stats(self, group_id: str, day: str) -> dict:
        self._ensure_group_stats(group_id)
        return self.stats[group_id].get("daily", {}).get(day, {"total_messages": 0, "users": {}})

    def _get_week_stats(self, group_id: str) -> dict:
        self._ensure_group_stats(group_id)
        total_messages = 0
        users = {}
        daily = self.stats[group_id].get("daily", {})
        for day in self._week_dates():
            day_stats = daily.get(day, {"total_messages": 0, "users": {}})
            total_messages += day_stats.get("total_messages", 0)
            for user_id, count in day_stats.get("users", {}).items():
                users[user_id] = users.get(user_id, 0) + count
        return {
            "total_messages": total_messages,
            "users": users,
            "user_names": self.stats[group_id].get("user_names", {}),
        }

    def _get_month_stats(self, group_id: str) -> dict:
        self._ensure_group_stats(group_id)
        total_messages = 0
        users = {}
        daily = self.stats[group_id].get("daily", {})
        for day in self._month_dates():
            day_stats = daily.get(day, {"total_messages": 0, "users": {}})
            total_messages += day_stats.get("total_messages", 0)
            for user_id, count in day_stats.get("users", {}).items():
                users[user_id] = users.get(user_id, 0) + count
        return {
            "total_messages": total_messages,
            "users": users,
            "user_names": self.stats[group_id].get("user_names", {}),
        }

    def _export_ranking_text(self, title: str, user_stats: dict, user_names: dict | None = None) -> str:
        return f"{title}\n" + self._format_ranking(user_stats, user_names, top_n=99999)

    # ==================== 引用消息 / 消息段工具 ====================

    def _debug_event_for_reply(self, event: AstrMessageEvent):
        """将事件结构安全地打到日志，用于排查引用消息位置。

        会打印 message_obj 的关键字段、消息段结构、raw_message 内容。
        对隐私字段（sender/user_id/nickname/card）只打印 type/key。
        """
        mo = event.message_obj
        lines = ["[astrbot_keyword_stats] 引用诊断 === 开始"]

        # ---- event 顶层 ----
        lines.append(f"  event type: {type(event).__name__}")
        for attr in ('platform_meta', 'unified_msg_origin', 'message_id', 'group_id',
                     'role', 'sender_role', 'message_str'):
            val = getattr(event, attr, None)
            if val is not None:
                val_str = str(val)[:120] if not isinstance(val, (dict, list)) else f"<{type(val).__name__}>"
                lines.append(f"  event.{attr}: {val_str}")

        # ---- event 上是否有 bot / client / adapter ----
        for attr in ('bot', 'client', 'adapter', 'platform_adapter'):
            val = getattr(event, attr, None)
            if val is not None:
                lines.append(f"  event.{attr}: {type(val).__name__}")

        # ---- message_obj ----
        lines.append(f"  message_obj type: {type(mo).__name__}")
        # 列出所有公开字段
        safe_fields = []
        for name in sorted(dir(mo)):
            if name.startswith('_'):
                continue
            try:
                val = getattr(mo, name, None)
                if val is None:
                    continue
                if name in ('sender',):
                    safe_fields.append(f"{name}=<{type(val).__name__}>")
                elif isinstance(val, (str, int, float, bool)):
                    safe_fields.append(f"{name}={repr(val)[:80]}")
                elif isinstance(val, (list, dict)):
                    safe_fields.append(f"{name}=<{type(val).__name__} len={len(val)}>")
                elif callable(val):
                    safe_fields.append(f"{name}=<callable>")
                else:
                    safe_fields.append(f"{name}=<{type(val).__name__}>")
            except Exception:
                safe_fields.append(f"{name}=<err>")
        lines.append(f"  message_obj fields: {', '.join(safe_fields)}")

        # ---- 消息段 ----
        for attr in ('message', 'messages', 'raw_message', 'segments', 'message_chain'):
            segs = getattr(mo, attr, None)
            if segs is None:
                continue
            lines.append(f"  message_obj.{attr}:")
            if isinstance(segs, str):
                # raw_message 可能是 JSON 字符串！
                truncated = segs[:500]
                lines.append(f"    str(len={len(segs)}): {truncated}")
                # 尝试解析 JSON
                try:
                    parsed = json.loads(segs)
                    self._debug_dump_structure(parsed, lines, indent=4, depth=0)
                except (json.JSONDecodeError, TypeError):
                    lines.append("    (不是 JSON)")
            elif isinstance(segs, list):
                for i, seg in enumerate(segs):
                    self._debug_dump_segment(seg, lines, indent=4, idx=i)
            elif isinstance(segs, dict):
                self._debug_dump_structure(segs, lines, indent=4, depth=0)
            else:
                lines.append(f"    <{type(segs).__name__}>: {str(segs)[:200]}")

        # ---- 检查 message_obj.raw_message (如果是对象) ----
        raw = getattr(mo, 'raw_message', None)
        if raw is not None and not isinstance(raw, (str, list)):
            lines.append(f"  message_obj.raw_message (object): {type(raw).__name__}")
            for name in sorted(dir(raw)):
                if name.startswith('_') or callable(getattr(raw, name, None)):
                    continue
                try:
                    val = getattr(raw, name, None)
                    if val is not None:
                        lines.append(f"    .{name}: {type(val).__name__} = {str(val)[:120]}")
                except Exception:
                    pass

        # ---- 递归检查所有属性里是否有嵌套 reply ----
        self._debug_search_reply_recursive(mo, lines)

        lines.append("[astrbot_keyword_stats] 引用诊断 === 结束")
        logger.info("\n".join(lines))

    def _debug_dump_segment(self, seg, lines: list, indent: int, idx: int):
        """安全地打印单个消息段。"""
        prefix = " " * indent
        if isinstance(seg, dict):
            seg_type = seg.get('type', '?')
            seg_data = seg.get('data', {})
            if isinstance(seg_data, dict):
                data_keys = list(seg_data.keys())
            else:
                data_keys = [f"<{type(seg_data).__name__}>"]
            lines.append(f"{prefix}[{idx}] type={seg_type} data_keys={data_keys}")
            # 如果是 reply/quote 段，打印 data 的少量细节
            if seg_type in ('reply', 'quote', 'reference'):
                for k, v in seg_data.items():
                    v_str = str(v)[:150] if not isinstance(v, (dict, list)) else f"<{type(v).__name__}>"
                    lines.append(f"{prefix}      data.{k}: {v_str}")
        elif hasattr(seg, 'type'):
            seg_type = getattr(seg, 'type', '?')
            lines.append(f"{prefix}[{idx}] type={seg_type} <{type(seg).__name__}>")
        else:
            lines.append(f"{prefix}[{idx}] <{type(seg).__name__}>: {str(seg)[:100]}")

    def _debug_dump_structure(self, obj, lines: list, indent: int, depth: int):
        """递归安全打印 dict/list 结构。"""
        if depth > 4:
            return
        prefix = " " * indent
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    lines.append(f"{prefix}{k}: <{type(v).__name__} len={len(v)}>")
                    self._debug_dump_structure(v, lines, indent + 2, depth + 1)
                elif isinstance(v, str) and len(v) > 120:
                    lines.append(f"{prefix}{k}: str({len(v)})")
                else:
                    lines.append(f"{prefix}{k}: {str(v)[:150]}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}[{i}]: <{type(item).__name__} len={len(item)}>")
                    self._debug_dump_structure(item, lines, indent + 2, depth + 1)
                elif isinstance(item, str) and len(item) > 120:
                    lines.append(f"{prefix}[{i}]: str({len(item)})")
                else:
                    lines.append(f"{prefix}[{i}]: {str(item)[:150]}")

    def _debug_search_reply_recursive(self, obj, lines: list, depth: int = 0, path: str = "message_obj"):
        """递归搜索对象树中是否有 reply/quote 相关的 dict/属性。"""
        if depth > 5 or obj is None:
            return
        if isinstance(obj, (str, int, float, bool)):
            return
        if isinstance(obj, dict):
            if obj.get('type') in ('reply', 'quote', 'reference'):
                lines.append(f"  [递归发现] {path} 中有 type={obj.get('type')} dict: keys={list(obj.keys())[:10]}")
                data = obj.get('data', {})
                if isinstance(data, dict):
                    for k, v in data.items():
                        lines.append(f"    data.{k}: {str(v)[:200]}")
            for k, v in obj.items():
                self._debug_search_reply_recursive(v, lines, depth + 1, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                self._debug_search_reply_recursive(item, lines, depth + 1, f"{path}[{i}]")
        elif hasattr(obj, 'type'):
            seg_type = getattr(obj, 'type', '')
            if seg_type in ('reply', 'quote', 'reference'):
                lines.append(f"  [递归发现] {path} 中有 type={seg_type} 对象 <{type(obj).__name__}>")
                for name in sorted(dir(obj)):
                    if name.startswith('_'):
                        continue
                    try:
                        val = getattr(obj, name, '')
                        lines.append(f"    .{name}: {str(val)[:200]}")
                    except Exception:
                        pass
            # 也遍历子属性
            for name in sorted(dir(obj)):
                if name.startswith('_') or name in ('message_obj',):
                    continue
                try:
                    val = getattr(obj, name, None)
                    if val is not None and not isinstance(val, (str, int, float, bool)) and not callable(val):
                        self._debug_search_reply_recursive(val, lines, depth + 1, f"{path}.{name}")
                except Exception:
                    pass

    async def _extract_reply_message(self, event: AstrMessageEvent):
        """从事件中提取被引用的消息对象（异步，支持 API 回退拉取）。

        不同 adapter 的引用字段差异很大；这里遍历常见属性和消息段，
        递归搜索嵌套结构，解析 raw_message JSON，并尝试通过 bot API 拉取。
        返回可能是 dict、对象或 None。
        """
        message_obj = event.message_obj

        # ---- 辅助：从 data 中判断是否有消息内容 ----
        def _data_has_content(data) -> bool:
            if not isinstance(data, dict):
                return hasattr(data, 'message') or hasattr(data, 'segments') or hasattr(data, 'raw_message')
            content_keys = {'message', 'messages', 'segments', 'raw_message', 'content', 'text'}
            return bool(set(data.keys()) & content_keys)

        # ---- 辅助：递归搜索对象树中找到 type="reply"/"quote"/"reference" 的节点 ----
        def _recursive_find_reply(obj, depth=0):
            if depth > 6 or obj is None:
                return None
            if isinstance(obj, (str, int, float, bool)):
                return None
            if isinstance(obj, dict):
                if obj.get('type') in ('reply', 'quote', 'reference'):
                    data = obj.get('data', {})
                    if _data_has_content(data):
                        return data
                    # 只有 id → 返回 dict 供外层记录 id
                    if isinstance(data, dict) and (data.get('id') or data.get('message_id')):
                        return obj
                    if data:
                        return data
                for v in obj.values():
                    r = _recursive_find_reply(v, depth + 1)
                    if r is not None:
                        return r
            elif isinstance(obj, list):
                for item in obj:
                    r = _recursive_find_reply(item, depth + 1)
                    if r is not None:
                        return r
            elif hasattr(obj, 'type') and getattr(obj, 'type', '') in ('reply', 'quote', 'reference'):
                data = getattr(obj, 'data', {})
                if _data_has_content(data):
                    return data
                if isinstance(data, dict) and (data.get('id') or data.get('message_id')):
                    return obj
                if data:
                    return data
            return None

        # ---- 辅助：从一段 data（可能是只有 id 的 reply 段）提取 reply_id ----
        def _extract_reply_id(data) -> str | None:
            if isinstance(data, dict):
                return data.get('message_id') or data.get('id') or data.get('msg_id') or data.get('seq')
            if hasattr(data, 'message_id'):
                return getattr(data, 'message_id', None) or getattr(data, 'id', None)
            return None

        # ======== 第 1 层：message_obj 上的直接属性 ========
        for attr in ('reply', 'quote', 'source', 'reply_message', 'quote_message',
                     'referenced_message', 'reference', 'reply_obj', 'quote_obj',
                     'message_reference', 'message_quote'):
            val = getattr(message_obj, attr, None)
            if val is not None and val is not message_obj:
                return val

        # ======== 第 2 层：消息段列表中查找 reply/quote 段 ========
        reply_id = None
        reply_node = None  # 可能是一个仅含 id 的 reply dict/对象
        for attr in ('message', 'messages', 'raw_message', 'segments', 'message_chain'):
            segments = getattr(message_obj, attr, None)

            # raw_message 可能是 JSON 字符串
            if isinstance(segments, str) and attr == 'raw_message':
                try:
                    segments = json.loads(segments)
                except (json.JSONDecodeError, TypeError):
                    pass

            if not isinstance(segments, list):
                continue

            for seg in segments:
                if isinstance(seg, dict):
                    seg_type = seg.get('type', '')
                elif hasattr(seg, 'type'):
                    raw_type = getattr(seg, 'type', '')
                    # 兼容 ComponentType 枚举 (e.g. ComponentType.Reply)
                    seg_type = getattr(raw_type, 'value', str(raw_type)).lower()
                    # strip "componenttype." prefix if present
                    if '.' in seg_type:
                        seg_type = seg_type.rsplit('.', 1)[-1]
                else:
                    continue

                if seg_type not in ('reply', 'quote', 'reference'):
                    continue

                data = seg.get('data', {}) if isinstance(seg, dict) else getattr(seg, 'data', {})
                if data and _data_has_content(data):
                    return data

                rid = _extract_reply_id(data) if data else None
                if rid:
                    reply_id = rid
                    reply_node = seg
                    break
            if reply_id:
                break

        # ======== 第 2.5 层：递归搜索 message_obj 所有嵌套结构 ========
        if reply_id is None:
            recursive_result = _recursive_find_reply(message_obj)
            if recursive_result is not None:
                if isinstance(recursive_result, dict) and _data_has_content(recursive_result):
                    return recursive_result
                rid = _extract_reply_id(
                    recursive_result.get('data', {}) if isinstance(recursive_result, dict)
                    else getattr(recursive_result, 'data', {})
                )
                if rid:
                    reply_id = rid
                    reply_node = recursive_result

        # ======== 第 3 层：用 reply_id 尝试 API 拉取 ========
        if reply_id:
            logger.info(
                "[astrbot_keyword_stats] 找到引用 id=%s，尝试通过 API 拉取原消息",
                reply_id,
            )
            fetched = await self._try_fetch_message_by_id(event, reply_id)
            if fetched is not None:
                return fetched
            # API 也失败了，返回 reply_node 让外层至少知道有引用
            if reply_node is not None:
                logger.info(
                    "[astrbot_keyword_stats] API 拉取失败，reply_node 可用字段: %s",
                    list(reply_node.keys()) if isinstance(reply_node, dict)
                    else [a for a in dir(reply_node) if not a.startswith('_')],
                )

        # ======== 第 4 层：顶层 event 上的属性 ========
        for attr in ('reply', 'quote', 'reply_message', 'quote_message',
                     'referenced_message', 'reference'):
            val = getattr(event, attr, None)
            if val is not None and val is not message_obj:
                return val

        # ======== 第 5 层：QQ Official / aiocqhttp raw.records ========
        # raw 可能在多个位置，逐个尝试
        raw_candidates = []
        # 路径 A: message_obj.raw（qq_official 直挂）
        raw_a = getattr(message_obj, 'raw', None)
        if isinstance(raw_a, dict):
            raw_candidates.append(('message_obj.raw', raw_a))
        # 路径 B: message_obj.raw_message.raw（aiocqhttp 嵌套在 Event 对象里）
        raw_msg = getattr(message_obj, 'raw_message', None)
        if raw_msg is not None:
            raw_b = getattr(raw_msg, 'raw', None)
            if isinstance(raw_b, dict):
                raw_candidates.append(('message_obj.raw_message.raw', raw_b))
            elif isinstance(raw_msg, dict):
                raw_b = raw_msg.get('raw')
                if isinstance(raw_b, dict):
                    raw_candidates.append(('message_obj.raw_message["raw"]', raw_b))

        for src, raw in raw_candidates:
            raw_elements = raw.get('elements', [])
            records = raw.get('records', [])
            if not raw_elements or not records:
                continue

            source_msg_id = None
            source_msg_seq = None
            for elem in raw_elements:
                if not isinstance(elem, dict):
                    continue
                reply_elem = elem.get('replyElement')
                if isinstance(reply_elem, dict):
                    source_msg_id = reply_elem.get('sourceMsgIdInRecords')
                    source_msg_seq = reply_elem.get('replayMsgSeq')
                    if source_msg_id:
                        break

            matched = None
            if source_msg_id:
                for rec in records:
                    if isinstance(rec, dict) and str(rec.get('msgId', '')) == str(source_msg_id):
                        matched = rec
                        break
            if matched is None and source_msg_seq:
                for rec in records:
                    if isinstance(rec, dict) and str(rec.get('msgSeq', '')) == str(source_msg_seq):
                        matched = rec
                        break

            if matched is not None:
                logger.info(
                    "[astrbot_keyword_stats] 从 %s.records 读取引用消息成功 msgId=%s",
                    src, source_msg_id,
                )
                return matched

            logger.info(
                "[astrbot_keyword_stats] %s.records 未匹配: sourceMsgId=%s sourceMsgSeq=%s "
                "records_msgIds=[%s]",
                src, source_msg_id, source_msg_seq,
                ', '.join(str(r.get('msgId', '?')) for r in records if isinstance(r, dict)),
            )

        return None

    async def _try_fetch_message_by_id(self, event: AstrMessageEvent, msg_id: str):
        """尝试通过 adapter bot/client API 根据消息 id 拉取原消息。

        依次尝试: event.bot, event.client, event.message_obj.bot,
        event.message_obj.client, event.adapter, event.platform_adapter。
        """
        # 收集所有可能的 bot/client 入口
        bot_candidates = []
        for src, obj in [
            ('event.bot', getattr(event, 'bot', None)),
            ('event.client', getattr(event, 'client', None)),
            ('event.adapter', getattr(event, 'adapter', None)),
            ('event.platform_adapter', getattr(event, 'platform_adapter', None)),
        ]:
            if obj is not None:
                bot_candidates.append((src, obj))

        mo = event.message_obj
        for src, obj in [
            ('event.message_obj.bot', getattr(mo, 'bot', None)),
            ('event.message_obj.client', getattr(mo, 'client', None)),
        ]:
            if obj is not None:
                bot_candidates.append((src, obj))

        for src, bot in bot_candidates:
            logger.info("[astrbot_keyword_stats] get_msg 尝试入口: %s (%s)", src, type(bot).__name__)

            # 路径 1: call_action (OneBot v11)
            call_action = getattr(bot, 'call_action', None)
            if callable(call_action):
                try:
                    result = call_action("get_msg", message_id=int(msg_id))
                    if inspect.isawaitable(result):
                        result = await result
                    if isinstance(result, dict) and (result.get('message') or result.get('raw_message')):
                        logger.info("[astrbot_keyword_stats] get_msg 成功 (via %s.call_action) id=%s", src, msg_id)
                        return result
                except Exception as e:
                    logger.info("[astrbot_keyword_stats] %s.call_action('get_msg') 失败: %s", src, e)

            # 路径 2: 直接 get_msg / get_message
            for method_name in ('get_msg', 'get_message'):
                fn = getattr(bot, method_name, None)
                if not callable(fn):
                    continue
                try:
                    result = fn(message_id=int(msg_id))
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        logger.info("[astrbot_keyword_stats] get_msg 成功 (via %s.%s) id=%s", src, method_name, msg_id)
                        return result
                except Exception as e:
                    logger.info("[astrbot_keyword_stats] %s.%s() 失败: %s", src, method_name, e)

            # 路径 3: Satori 风格 bot.api / bot.message
            for api_attr in ('api', 'message'):
                api = getattr(bot, api_attr, None)
                if api is None:
                    continue
                for method_name in ('get_message', 'get_msg', 'get'):
                    fn = getattr(api, method_name, None)
                    if not callable(fn):
                        continue
                    try:
                        result = fn(message_id=msg_id)
                        if inspect.isawaitable(result):
                            result = await result
                        if result is not None:
                            logger.info("[astrbot_keyword_stats] get_msg 成功 (via %s.%s.%s) id=%s", src, api_attr, method_name, msg_id)
                            return result
                    except Exception as e:
                        pass

        logger.info("[astrbot_keyword_stats] 所有 get_msg 入口均失败，msg_id=%s，尝试过: %s",
                     msg_id, [s for s, _ in bot_candidates])
        return None

    async def _try_fetch_image_via_bot(self, event: AstrMessageEvent,
                                        seg_data: dict) -> str | bytes | None:
        """通过 OneBot / adapter bot API 拉取图片数据。

        尝试 call_action("get_image") 和 get_image()。
        返回: base64 字符串、bytes、文件路径、URL 或 None。
        """
        # 收集候选 file id
        candidates = []
        for key in ('file', 'file_id', 'fileId', 'fileUuid', 'uuid', 'id', 'md5'):
            val = seg_data.get(key)
            if val and isinstance(val, str) and not val.startswith(('http://', 'https://', '/')):
                candidates.append(val)
                break  # 只需一个

        if not candidates:
            return None

        # 收集 bot 入口
        bot_sources = []
        for src, obj in [
            ('event.bot', getattr(event, 'bot', None)),
            ('event.client', getattr(event, 'client', None)),
            ('event.adapter', getattr(event, 'adapter', None)),
        ]:
            if obj is not None:
                bot_sources.append((src, obj))

        file_id = candidates[0]
        for src, bot in bot_sources:
            # 路径 1: call_action("get_image", file=...)
            call_action = getattr(bot, 'call_action', None)
            if callable(call_action):
                try:
                    result = call_action("get_image", file=file_id)
                    if inspect.isawaitable(result):
                        result = await result
                    # 返回可能是 {file: "base64://..."} 或 {url: "..."} 或 {file: "/path/..."}
                    if isinstance(result, dict):
                        file_data = result.get('file') or result.get('data') or result.get('url')
                        if file_data:
                            # base64:// 前缀
                            if isinstance(file_data, str) and file_data.startswith('base64://'):
                                return file_data[len('base64://'):]
                            if isinstance(file_data, str) and file_data.startswith(('http://', 'https://')):
                                return file_data
                            return file_data
                        path_data = result.get('path')
                        if path_data:
                            return path_data
                except Exception as e:
                    logger.info(
                        "[astrbot_keyword_stats] %s.call_action('get_image') 失败: %s",
                        src, e,
                    )

            # 路径 2: 直接 get_image 方法
            get_image = getattr(bot, 'get_image', None)
            if callable(get_image):
                try:
                    result = get_image(file=file_id)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        if isinstance(result, bytes):
                            return result
                        if isinstance(result, str):
                            return result
                        if isinstance(result, dict):
                            return (result.get('file') or result.get('data')
                                   or result.get('url') or result.get('path'))
                except Exception as e:
                    logger.info(
                        "[astrbot_keyword_stats] %s.get_image() 失败: %s",
                        src, e,
                    )

        logger.info(
            "[astrbot_keyword_stats] 所有 bot 入口 get_image 均失败 file_id=%s",
            file_id,
        )
        return None

    def _parse_qq_official_elements(self, elements: list, segments: list):
        """解析 QQ Official adapter 的 elements 列表，提取文本和图片段。

        elementType 1 → textElement.content
        elementType 2 → picElement（优先 sourcePath/filePath/url）
        elementType 7 → replyElement（跳过，或提取 sourceMsgText 作为 fallback）
        """
        for elem in elements:
            if not isinstance(elem, dict):
                continue

            # 文本
            text_elem = elem.get('textElement')
            if isinstance(text_elem, dict):
                text = text_elem.get('content', '')
                if text:
                    segments.append({"type": "text", "data": {"text": text}})

            # 图片
            pic_elem = elem.get('picElement')
            if isinstance(pic_elem, dict):
                # 诊断：记录 picElement 的完整 keys 和关键字段
                logger.info(
                    "[astrbot_keyword_stats] 图片诊断 picElement keys=%s "
                    "sourcePath=%s filePath=%s url=%s originImageUrl=%s "
                    "thumbUrl=%s picUrl=%s fileUrl=%s downloadUrl=%s "
                    "fileUuid=%s md5=%s fileName=%s",
                    list(pic_elem.keys()),
                    pic_elem.get('sourcePath', '')[:120],
                    pic_elem.get('filePath', '')[:120],
                    pic_elem.get('url', '')[:120],
                    pic_elem.get('originImageUrl', '')[:120],
                    pic_elem.get('thumbUrl', '')[:120],
                    pic_elem.get('picUrl', '')[:120],
                    pic_elem.get('fileUrl', '')[:120],
                    pic_elem.get('downloadUrl', '')[:120],
                    pic_elem.get('fileUuid', ''),
                    pic_elem.get('md5', ''),
                    pic_elem.get('fileName', ''),
                )
                img_data = {}
                # QQ Official picElement → AstrBot Image 兼容字段
                # AstrBot 的 Image() 必填参数是 file=，接受 URL 或本地路径
                # 注意：sourcePath/filePath 是宿主 QQ 客户端的路径，容器内不可访达
                url_val = (pic_elem.get('url') or pic_elem.get('originImageUrl')
                          or pic_elem.get('picUrl') or pic_elem.get('fileUrl')
                          or pic_elem.get('downloadUrl') or pic_elem.get('thumbUrl'))
                if url_val:
                    img_data['file'] = url_val  # AstrBot Image 用 file= 接受 URL
                    img_data['url'] = url_val   # 也保留 url 字段

                # 本地路径不直接作为 file 主干，只保留为 raw 字段
                source_path = pic_elem.get('sourcePath')
                file_path = pic_elem.get('filePath')
                if source_path:
                    img_data['sourcePath'] = source_path
                if file_path:
                    img_data['filePath'] = file_path

                # 保留原始字段供调试
                for key in ('fileName', 'fileUuid', 'uuid', 'md5', 'fileSize',
                           'picWidth', 'picHeight'):
                    val = pic_elem.get(key)
                    if val:
                        img_data[key] = val
                if img_data:
                    segments.append({"type": "image", "data": img_data})

            # replyElement.sourceMsgText 作为纯文本回退
            reply_elem = elem.get('replyElement')
            if isinstance(reply_elem, dict):
                source_text = reply_elem.get('sourceMsgText')
                if source_text and not segments:
                    segments.append({"type": "text", "data": {"text": source_text}})

    def _extract_segments_from_message(self, msg) -> list:
        """从一条消息（dict 或对象）中提取可持久化的消息段列表。

        返回 list[dict]，每个 dict 形如：
            {"type": "text", "data": {"text": "..."}}
            {"type": "image", "data": {"url": "...", "file": "...", ...}}
        """
        segments = []

        # ---- 先拿到消息段列表 ----
        raw_segments = None
        if isinstance(msg, dict):
            # QQ Official: msg 带 elements 字段（来自 raw.records）
            qq_elements = msg.get('elements')
            if isinstance(qq_elements, list):
                self._parse_qq_official_elements(qq_elements, segments)
                if segments:
                    return segments

            for key in ('message', 'messages', 'segments', 'raw_message'):
                candidate = msg.get(key)
                if isinstance(candidate, list):
                    raw_segments = candidate
                    break
                elif isinstance(candidate, str) and candidate.strip():
                    segments.append({"type": "text", "data": {"text": candidate}})
                    return segments
            # 也可能 msg 本身就是单个消息段
            if raw_segments is None and msg.get('type'):
                raw_segments = [msg]
        elif isinstance(msg, str):
            if msg.strip():
                segments.append({"type": "text", "data": {"text": msg}})
            return segments
        elif hasattr(msg, 'message'):
            raw_segments = getattr(msg, 'message', None)
            if isinstance(raw_segments, str):
                if raw_segments.strip():
                    segments.append({"type": "text", "data": {"text": raw_segments}})
                return segments
        elif msg is not None:
            # 尝试当作单个段对象
            if hasattr(msg, 'type'):
                raw_segments = [msg]

        if not isinstance(raw_segments, list):
            return segments

        # ---- 逐个段解析 ----
        for seg in raw_segments:
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {}) or {}
            elif hasattr(seg, 'type'):
                seg_type = getattr(seg, 'type', '')
                seg_data = getattr(seg, 'data', {}) or {}
                if not isinstance(seg_data, dict):
                    seg_data = {}
            else:
                continue

            if seg_type == 'text':
                text = seg_data.get('text', '')
                if text:
                    segments.append({"type": "text", "data": {"text": text}})
            elif seg_type == 'image':
                image_data = {}
                for key in ('url', 'file', 'path', 'base64', 'summary'):
                    val = seg_data.get(key)
                    if val:
                        image_data[key] = val
                if image_data:
                    segments.append({"type": "image", "data": image_data})

        return segments

    def _extract_own_image_segments(self, event: AstrMessageEvent) -> list:
        """从当前这条消息自身提取图片段（忽略命令文本）。

        用于"发送图片并在同一条消息里写 添加关键词 关键词"的场景：
        某些平台（如 QQ 官方接口）引用历史图片拿不到下载地址，但当前消息
        自带的图片通常带有可下载 url。优先从 AstrBot 规范化组件读取，
        再回退到 QQ 官方 raw.elements。
        """
        message_obj = getattr(event, 'message_obj', None)
        if message_obj is None:
            return []

        # ---- 路径 A: AstrBot 规范化组件 / OneBot dict 段 ----
        for attr in ('message', 'messages', 'segments', 'raw_message'):
            raw_segments = getattr(message_obj, attr, None)
            if isinstance(raw_segments, str) and attr == 'raw_message':
                try:
                    raw_segments = json.loads(raw_segments)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(raw_segments, list):
                continue
            found = []
            for seg in raw_segments:
                img = self._image_segment_from_component(seg)
                if img:
                    found.append(img)
            if found:
                return found

        # ---- 路径 B: QQ 官方 raw.elements（picElement 带 url）----
        raw = getattr(message_obj, 'raw', None)
        if isinstance(raw, dict):
            elements = raw.get('elements')
            if isinstance(elements, list):
                tmp = []
                self._parse_qq_official_elements(elements, tmp)
                found = [s for s in tmp if s.get('type') == 'image']
                if found:
                    return found

        return []

    def _image_segment_from_component(self, seg) -> dict | None:
        """把单个消息段（dict 或 AstrBot 组件对象）规范化为图片段，非图片返回 None。"""
        if isinstance(seg, dict):
            if str(seg.get('type', '')).lower() != 'image':
                return None
            seg_data = seg.get('data', {}) or {}
            image_data = {}
            for key in ('url', 'file', 'path', 'base64'):
                val = seg_data.get(key)
                if val:
                    image_data[key] = val
            return {"type": "image", "data": image_data} if image_data else None

        # AstrBot 组件对象（Image）：type 可能是 "Image" 或枚举，url/file 为直接属性
        seg_type = str(getattr(seg, 'type', '')).lower()
        if 'image' not in seg_type:
            return None
        image_data = {}
        for key in ('url', 'file', 'path', 'base64'):
            val = getattr(seg, key, None)
            if val and isinstance(val, str):
                image_data[key] = val
        return {"type": "image", "data": image_data} if image_data else None

    def _is_empty_reply(self, reply) -> bool:
        """判断一条回复值是否为空。"""
        if reply is None:
            return True
        if isinstance(reply, str):
            return not reply.strip()
        if isinstance(reply, dict):
            if reply.get('type') == 'segments':
                return len(reply.get('segments', [])) == 0
        return False

    def _format_reply_summary(self, reply) -> str:
        """将回复值格式化为列表展示用的摘要字符串。

        字符串直接返回；图文段返回 "[图文消息]" 或 "[图片消息]"。
        """
        if isinstance(reply, str):
            return reply
        if isinstance(reply, dict) and reply.get('type') == 'segments':
            segs = reply.get('segments', [])
            has_text = any(s.get('type') == 'text' for s in segs)
            has_image = any(s.get('type') == 'image' for s in segs)
            if has_text and has_image:
                return "[图文消息]"
            if has_image:
                return "[图片消息]"
            if has_text:
                texts = [s.get('data', {}).get('text', '') for s in segs if s.get('type') == 'text']
                joined = ' '.join(texts).strip()
                return joined[:50] + ("…" if len(joined) > 50 else "")
            return "[空消息]"
        return str(reply)[:50]

    # ==================== 图片持久化 ====================

    async def _persist_reply_segments(self, segments: list, keyword: str,
                                       event: AstrMessageEvent) -> tuple:
        """对 segments 中的图片段做持久化落地。

        复制/下载/API拉取图片到 self.media_dir，替换 seg_data 为持久化路径。
        返回 (persisted_segments, failed_count)。
        """
        persisted = []
        img_index = 0
        failed = 0
        for seg in segments:
            if seg.get('type') != 'image':
                persisted.append(seg)
                continue
            seg_data = seg.get('data', {})
            new_data = await self._persist_image_segment(seg_data, keyword, img_index, event)
            if new_data:
                persisted.append({"type": "image", "data": new_data})
                logger.info(
                    "[astrbot_keyword_stats] 图片持久化成功 keyword=%s index=%d file=%s",
                    keyword, img_index, new_data.get('file', ''),
                )
            else:
                failed += 1
                logger.warning(
                    "[astrbot_keyword_stats] 图片持久化失败 keyword=%s index=%d",
                    keyword, img_index,
                )
                # 不保留不可用的图片段
            img_index += 1
        return persisted, failed

    async def _persist_image_segment(self, seg_data: dict, keyword: str, index: int,
                                     event: AstrMessageEvent) -> dict | None:
        """将单个图片段持久化到 media_dir。

        依次尝试: 本地文件复制 > HTTP 下载 > base64 解码 > bot API 拉取。
        返回新的 seg_data dict（包含持久化 file 路径），失败返回 None。
        """
        # 生成安全文件名
        safe_kw = "".join(c if c.isalnum() or c in '_-' else '_' for c in keyword)[:30]
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        # 用 data 内容的 hash 避免重复
        data_str = str(sorted(seg_data.items()))
        h = hashlib.md5(data_str.encode()).hexdigest()[:8]
        stem = f"{safe_kw}_{ts}_{index}_{h}"

        # ---- 方式 1: 本地文件复制 ----
        src_path = seg_data.get('sourcePath') or seg_data.get('filePath') or seg_data.get('file')
        if src_path and Path(src_path).is_file():
            suffix = Path(src_path).suffix or '.png'
            dst = self.media_dir / f"{stem}{suffix}"
            try:
                shutil.copy2(src_path, dst)
                return {"file": str(dst)}
            except Exception as e:
                logger.warning(
                    "[astrbot_keyword_stats] 复制本地图片失败: %s → %s: %s",
                    src_path, dst, e,
                )

        # ---- 方式 2: HTTP URL 下载 ----
        url = seg_data.get('url') or seg_data.get('file')
        if url and isinstance(url, str) and url.startswith(('http://', 'https://')):
            suffix = '.png'
            for ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
                if ext in url.lower():
                    suffix = ext
                    break
            dst = self.media_dir / f"{stem}{suffix}"
            try:
                urllib.request.urlretrieve(url, dst)
                if dst.stat().st_size > 0:
                    return {"file": str(dst)}
                dst.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(
                    "[astrbot_keyword_stats] 下载图片失败: %s → %s: %s",
                    url, dst, e,
                )

        # ---- 方式 3: base64 解码 ----
        b64 = seg_data.get('base64')
        if b64:
            # 去掉可能的 data:image/...;base64, 前缀
            if ',' in b64:
                b64 = b64.split(',', 1)[1]
            suffix = '.png'
            dst = self.media_dir / f"{stem}{suffix}"
            try:
                data = base64.b64decode(b64)
                dst.write_bytes(data)
                return {"file": str(dst)}
            except Exception as e:
                logger.warning(
                    "[astrbot_keyword_stats] base64 解码失败: %s: %s",
                    stem, e,
                )

        # ---- 方式 4: bot API 拉取（OneBot get_image） ----
        fetched = await self._try_fetch_image_via_bot(event, seg_data)
        if fetched:
            # fetched 可能是文件路径或 base64 数据
            suffix = '.png'
            dst = self.media_dir / f"{stem}{suffix}"
            try:
                if isinstance(fetched, bytes):
                    dst.write_bytes(fetched)
                    return {"file": str(dst)}
                elif isinstance(fetched, str):
                    if fetched.startswith(('http://', 'https://')):
                        urllib.request.urlretrieve(fetched, dst)
                        if dst.stat().st_size > 0:
                            return {"file": str(dst)}
                        dst.unlink(missing_ok=True)
                    elif Path(fetched).is_file():
                        shutil.copy2(fetched, dst)
                        return {"file": str(dst)}
                    else:
                        # 可能是 base64
                        try:
                            data = base64.b64decode(fetched)
                            dst.write_bytes(data)
                            return {"file": str(dst)}
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(
                    "[astrbot_keyword_stats] bot API 图片落地失败: %s: %s",
                    stem, e,
                )

        return None

    # ==================== 消息回复构建 ====================

    def _build_reply_result(self, reply):
        """根据回复内容构建可用于 yield 的结果。

        返回:
            str → 纯文本，yield event.plain_result(str)
            list[Plain|Image] → 含图片的消息组件链
        """
        if isinstance(reply, str):
            return reply

        if isinstance(reply, dict) and reply.get('type') == 'segments':
            segs = reply.get('segments', [])
            if not segs:
                return ""

            texts = []
            images = []
            for seg in segs:
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {})
                if seg_type == 'text':
                    text = seg_data.get('text', '')
                    if text:
                        texts.append(text)
                elif seg_type == 'image':
                    images.append(seg_data)

            # 纯文本：直接返回合并字符串，兼容旧数据
            if texts and not images:
                return '\n'.join(texts)
            if not texts and not images:
                return ""

            # 含图片：构建组件链
            chain = []
            for seg in segs:
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {})
                if seg_type == 'text':
                    text = seg_data.get('text', '')
                    if text:
                        chain.append(Plain(text))
                elif seg_type == 'image':
                    # 诊断：记录当前 saved seg_data
                    logger.info(
                        "[astrbot_keyword_stats] 图片诊断 构造: seg_data keys=%s",
                        list(seg_data.keys()),
                    )
                    img_constructed = False

                    # AstrBot Image() 必填参数是 file=，接受 URL 或本地路径
                    # 方式 1: file 字段（URL 或本地路径）
                    file_val = seg_data.get('file')
                    if file_val and not img_constructed:
                        # HTTP URL → AstrBot 会自行下载
                        if isinstance(file_val, str) and file_val.startswith(('http://', 'https://')):
                            try:
                                chain.append(Image(file=file_val))
                                img_constructed = True
                            except Exception as e:
                                logger.warning(
                                    "[astrbot_keyword_stats] Image(file=URL) 失败: %s", e
                                )
                        # 本地路径 → 必须真实存在
                        elif Path(file_val).is_file():
                            try:
                                chain.append(Image(file=file_val))
                                img_constructed = True
                            except Exception as e:
                                logger.warning(
                                    "[astrbot_keyword_stats] Image(file=本地) 失败: %s", e
                                )
                        else:
                            logger.info(
                                "[astrbot_keyword_stats] 图片 file 路径不存在: %s",
                                file_val[:120],
                            )

                    # 方式 2: url 字段（兼容旧数据）
                    url_val = seg_data.get('url')
                    if url_val and not img_constructed:
                        if isinstance(url_val, str) and url_val.startswith(('http://', 'https://')):
                            try:
                                chain.append(Image(file=url_val))
                                img_constructed = True
                            except Exception as e:
                                logger.warning(
                                    "[astrbot_keyword_stats] Image(file=url) 失败: %s", e
                                )

                    if not img_constructed:
                        logger.warning(
                            "[astrbot_keyword_stats] 图片构造失败: file=%s url=%s sourcePath=%s",
                            str(seg_data.get('file', ''))[:120],
                            str(seg_data.get('url', ''))[:120],
                            str(seg_data.get('sourcePath', ''))[:120],
                        )
                        chain.append(Plain("[图片发送失败：图片地址不可用，请重新引用图片添加]"))
            if chain:
                return chain
            return ""

        return str(reply)

    # ==================== 消息处理入口 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            message_str = (event.message_str or "").strip()
            if not message_str:
                return

            group_id = self._get_group_id(event)
            user_id = self._get_user_id(event)
            display_name = self._get_display_name(event)
            is_whitelisted_group = self._is_group_whitelisted(group_id)
            if group_id and is_whitelisted_group:
                self._record_group_message(group_id, user_id, display_name)

            if not group_id:
                if is_whitelisted_group:
                    yield event.plain_result("无法获取当前群号，暂不能使用本插件")
                return

            normalized = message_str.replace("/", "").strip()

            whitelist_commands = {
                "群统计", "统计", "今日群统计", "今日统计", "今日发言", "查我发言", "我的发言",
                "昨日群统计", "昨日统计", "每周统计", "本周统计", "周统计", "月统计", "本月统计",
                "导出活跃榜", "导出发言榜", "活跃榜导出", "清空群统计", "清空统计", "重置统计",
                "关键词列表", "查看关键词", "关键字列表", "查看关键字"
            }
            if normalized.startswith(("查发言", "添加关键词", "新增关键词", "删除关键词", "移除关键词")):
                whitelist_command_hit = True
            else:
                whitelist_command_hit = normalized in whitelist_commands

            if not is_whitelisted_group and whitelist_command_hit:
                yield event.plain_result("当前群未加入统计白名单")
                return

            if not is_whitelisted_group:
                return

            if normalized in {"群统计", "统计"}:
                self._ensure_group_stats(group_id)
                data = self.stats[group_id]
                msg = (
                    f"群统计\n"
                    f"总消息数: {data.get('total_messages', 0)}\n"
                    f"活跃成员数: {len(data.get('users', {}))}\n"
                    f"发言榜:\n{self._format_ranking(data.get('users', {}), data.get('user_names', {}))}"
                )
                yield event.plain_result(msg)
                return

            if normalized in {"今日群统计", "今日统计"}:
                today = self._today()
                daily = self._get_day_stats(group_id, today)
                msg = (
                    f"今日群统计 ({today})\n"
                    f"今日消息数: {daily.get('total_messages', 0)}\n"
                    f"今日活跃成员数: {len(daily.get('users', {}))}\n"
                    f"今日发言榜:\n{self._format_ranking(daily.get('users', {}), self.stats[group_id].get('user_names', {}))}"
                )
                yield event.plain_result(msg)
                return

            if normalized in {"今日发言", "查我发言", "我的发言"}:
                yield event.plain_result(self._build_single_user_stats_message(group_id, user_id))
                return

            if normalized in {"昨日群统计", "昨日统计"}:
                day = self._yesterday()
                daily = self._get_day_stats(group_id, day)
                msg = (
                    f"昨日群统计 ({day})\n"
                    f"昨日消息数: {daily.get('total_messages', 0)}\n"
                    f"昨日活跃成员数: {len(daily.get('users', {}))}\n"
                    f"昨日发言榜:\n{self._format_ranking(daily.get('users', {}), self.stats[group_id].get('user_names', {}))}"
                )
                yield event.plain_result(msg)
                return

            if normalized in {"每周统计", "本周统计", "周统计"}:
                week_stats = self._get_week_stats(group_id)
                msg = (
                    f"本周群统计\n"
                    f"统计范围: {self._week_dates()[0]} 至 {self._week_dates()[-1]}\n"
                    f"本周消息数: {week_stats.get('total_messages', 0)}\n"
                    f"本周活跃成员数: {len(week_stats.get('users', {}))}\n"
                    f"本周发言榜:\n{self._format_ranking(week_stats.get('users', {}), week_stats.get('user_names', {}))}"
                )
                yield event.plain_result(msg)
                return

            if normalized in {"月统计", "本月统计"}:
                month_stats = self._get_month_stats(group_id)
                month_dates = self._month_dates()
                msg = (
                    f"本月群统计\n"
                    f"统计范围: {month_dates[0]} 至 {month_dates[-1]}\n"
                    f"本月消息数: {month_stats.get('total_messages', 0)}\n"
                    f"本月活跃成员数: {len(month_stats.get('users', {}))}\n"
                    f"本月发言榜:\n{self._format_ranking(month_stats.get('users', {}), month_stats.get('user_names', {}))}"
                )
                yield event.plain_result(msg)
                return

            if normalized in {"导出活跃榜", "导出发言榜", "活跃榜导出"}:
                data = self.stats[group_id]
                msg = self._export_ranking_text("群活跃榜导出", data.get('users', {}), data.get('user_names', {}))
                yield event.plain_result(msg)
                return

            if normalized.startswith("查发言"):
                query = normalized[3:].strip()
                target_user_id = self._extract_mentioned_user_id(event, message_str)
                if not target_user_id:
                    target_user_id = self._find_user_id(group_id, query)
                if not target_user_id:
                    yield event.plain_result("请使用：查发言 @某人，或 查发言 QQ号，或 查发言 群昵称")
                    return
                yield event.plain_result(self._build_single_user_stats_message(group_id, target_user_id))
                return

            if normalized in {"清空群统计", "清空统计", "重置统计"}:
                if not self._is_admin_or_owner(event):
                    yield event.plain_result("只有群主或管理员才能执行此操作")
                    return
                self.stats[group_id] = {
                    "total_messages": 0,
                    "users": {},
                    "user_names": {},
                    "daily": {},
                }
                self._save_stats()
                yield event.plain_result("当前群统计已清空")
                return

            if normalized in {"关键词列表", "查看关键词", "关键字列表", "查看关键字"}:
                keywords = self.keywords
                if not keywords:
                    yield event.plain_result("当前没有已配置的关键词")
                    return
                lines = ["关键词列表:"]
                for index, (keyword, reply) in enumerate(keywords.items(), start=1):
                    lines.append(f"{index}. {keyword} -> {self._format_reply_summary(reply)}")
                yield event.plain_result("\n".join(lines))
                return

            if normalized.startswith("添加关键词") or normalized.startswith("新增关键词"):
                logger.info(f"[astrbot_keyword_stats] 尝试添加关键词, sender={getattr(event.message_obj, 'sender', None)}")
                if not self._is_admin_or_owner(event):
                    yield event.plain_result("只有群主或管理员才能执行此操作")
                    return
                payload = normalized.split(" ", 1)[1].strip() if " " in normalized else normalized[5:].strip()
                if "=" in payload:
                    # 旧格式：添加关键词 词=回复内容
                    keyword, reply_text = payload.split("=", 1)
                    keyword = keyword.strip()
                    reply_text = reply_text.strip()
                    if not keyword or not reply_text:
                        yield event.plain_result("关键词和回复内容都不能为空")
                        return
                    keywords = self.keywords
                    keywords[keyword] = reply_text
                    self._save_keywords()
                    yield event.plain_result(f"已添加关键词：{keyword}")
                    return

                # 新格式：添加关键词 词 → 尝试从引用消息提取回复
                keyword = payload.strip()
                if not keyword:
                    yield event.plain_result(
                        "格式错误，请使用：添加关键词 关键词=回复内容，"
                        "或引用一条消息（或直接附带图片）后发送：添加关键词 关键词"
                    )
                    return

                reply_msg = await self._extract_reply_message(event)
                segments = self._extract_segments_from_message(reply_msg) if reply_msg is not None else []

                # 回退：从当前消息自身提取图片段
                # （QQ 官方接口等平台引用图片拿不到下载地址时，可改为"发图片+命令文字"一条消息）
                if not segments:
                    own_images = self._extract_own_image_segments(event)
                    if own_images:
                        logger.info(
                            "[astrbot_keyword_stats] 引用未取到内容，改用当前消息自带图片 %d 段",
                            len(own_images),
                        )
                        segments = own_images

                if not segments:
                    self._debug_event_for_reply(event)
                    yield event.plain_result(
                        "未读取到可用的图片或引用内容，请尝试以下方式：\n"
                        "① 使用格式：添加关键词 关键词=回复内容\n"
                        "② 引用一条包含文本/图片的消息后发送：添加关键词 关键词\n"
                        "③ 直接发送图片并在同一条消息里写：添加关键词 关键词\n"
                        "（请将 AstrBot 日志中 [astrbot_keyword_stats] 引用诊断 发给开发者）"
                    )
                    return

                # 图片段持久化落地到插件数据目录
                had_image = any(s.get('type') == 'image' for s in segments)
                segments, img_failed = await self._persist_reply_segments(segments, keyword, event)

                # 检查是否所有段都被丢弃（全是图片且全部失败）
                if had_image and img_failed > 0:
                    if not segments:
                        # 全部是图片且全部持久化失败
                        yield event.plain_result(
                            "图片保存失败：当前平台未提供可下载的图片地址，"
                            "无法保存为关键词回复。\n"
                            "请尝试使用 = 格式添加文本回复：添加关键词 关键词=回复内容"
                        )
                        return
                    else:
                        logger.warning(
                            "[astrbot_keyword_stats] %d 张图片持久化失败，已丢弃", img_failed,
                        )

                if not segments:
                    yield event.plain_result(
                        "未读取到可用的引用消息内容，请重新引用包含文本或图片的消息"
                    )
                    return

                reply_value = {"type": "segments", "segments": segments}
                keywords = self.keywords
                keywords[keyword] = reply_value
                self._save_keywords()
                yield event.plain_result(f"已添加关键词：{keyword}（{self._format_reply_summary(reply_value)}）")
                return

            if normalized.startswith("删除关键词") or normalized.startswith("移除关键词"):
                logger.info(f"[astrbot_keyword_stats] 尝试删除关键词, sender={getattr(event.message_obj, 'sender', None)}")
                if not self._is_admin_or_owner(event):
                    yield event.plain_result("只有群主或管理员才能执行此操作")
                    return
                keyword = normalized.split(" ", 1)[1].strip() if " " in normalized else normalized[5:].strip()
                if not keyword:
                    yield event.plain_result("格式错误，请使用：删除关键词 关键词")
                    return
                keywords = self.keywords
                if keyword not in keywords:
                    yield event.plain_result(f"未找到关键词：{keyword}")
                    return
                del keywords[keyword]
                self._save_keywords()
                yield event.plain_result(f"已删除关键词：{keyword}")
                return

            keywords: dict = self.keywords
            match_mode = str(self.config.get("match_mode", "contains")).lower()
            reply_once = bool(self.config.get("reply_once_per_message", True))

            for keyword, reply in keywords.items():
                matched = message_str == keyword if match_mode == "exact" else keyword in message_str
                if matched:
                    logger.info(
                        "[astrbot_keyword_stats] 关键词命中: %s reply_type=%s",
                        keyword, type(reply).__name__,
                    )
                    result = self._build_reply_result(reply)
                    if isinstance(result, str):
                        if result:
                            yield event.plain_result(result)
                    elif isinstance(result, list) and result:
                        # 含图片的组件链
                        chain_result = getattr(event, 'chain_result', None)
                        if callable(chain_result):
                            try:
                                yield chain_result(result)
                            except Exception:
                                logger.exception(
                                    "[astrbot_keyword_stats] event.chain_result 失败"
                                )
                                # 回退：提取文本部分
                                fallback = [getattr(c, 'text', '') for c in result]
                                fallback = [t for t in fallback if t]
                                if fallback:
                                    yield event.plain_result('\n'.join(fallback))
                                else:
                                    yield event.plain_result("[图文消息发送失败，请查看日志]")
                        else:
                            # 无 chain_result API：回退纯文本
                            fallback = [getattr(c, 'text', '') for c in result]
                            fallback = [t for t in fallback if t]
                            yield event.plain_result(
                                '\n'.join(fallback) if fallback
                                else "[图片消息，当前 AstrBot 版本暂不支持通过关键词回复发送图片]"
                            )
                    if reply_once:
                        return
        except Exception as e:
            logger.exception("[astrbot_keyword_stats] 处理群消息失败")

    async def terminate(self):
        self._save_stats()
        self._save_keywords()
