import json
from pathlib import Path
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
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
                    lines.append(f"{index}. {keyword} -> {reply}")
                yield event.plain_result("\n".join(lines))
                return

            if normalized.startswith("添加关键词") or normalized.startswith("新增关键词"):
                logger.info(f"[astrbot_keyword_stats] 尝试添加关键词, sender={getattr(event.message_obj, 'sender', None)}")
                if not self._is_admin_or_owner(event):
                    yield event.plain_result("只有群主或管理员才能执行此操作")
                    return
                payload = normalized.split(" ", 1)[1].strip() if " " in normalized else normalized[4:].strip()
                if "=" not in payload:
                    yield event.plain_result("格式错误，请使用：添加关键词 关键词=回复内容")
                    return
                keyword, reply = payload.split("=", 1)
                keyword = keyword.strip()
                reply = reply.strip()
                if not keyword or not reply:
                    yield event.plain_result("关键词和回复内容都不能为空")
                    return
                keywords = self.keywords
                keywords[keyword] = reply
                self._save_keywords()
                yield event.plain_result(f"已添加关键词：{keyword}")
                return

            if normalized.startswith("删除关键词") or normalized.startswith("移除关键词"):
                logger.info(f"[astrbot_keyword_stats] 尝试删除关键词, sender={getattr(event.message_obj, 'sender', None)}")
                if not self._is_admin_or_owner(event):
                    yield event.plain_result("只有群主或管理员才能执行此操作")
                    return
                keyword = normalized.split(" ", 1)[1].strip() if " " in normalized else normalized[4:].strip()
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
                    yield event.plain_result(reply)
                    if reply_once:
                        return
        except Exception as e:
            logger.error(f"[astrbot_keyword_stats] 处理群消息失败: {e}")

    async def terminate(self):
        self._save_stats()
        self._save_keywords()
