"""注册网易云与QQ音乐搜索工具，按配置发送卡片或返回歌曲文字信息。
机器人签名、独立签名和本地实验卡片使用各自发送路径。"""

import json
from . import settings as settings_config
import secrets
import time
import traceback
from typing import Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import (
    BaseMessageComponent,
    ComponentType,
    Json,
)

from astrbot.api.star import Star


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


NETEASE_SEARCH_API = "https://music.163.com/api/search/get/web"
NETEASE_SONG_URL = "https://music.163.com/#/song?id={song_id}"

NETEASE_DETAIL_API = "https://music.163.com/api/song/detail/"
NETEASE_PLAYER_API = "https://music.163.com/api/song/enhance/player/url"


QQMUSIC_SEARCH_API = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
QQMUSIC_SONG_URL = "https://y.qq.com/n/ryqq/songDetail/{song_mid}"


class MusicCard(BaseMessageComponent):
    type: ComponentType = ComponentType.Music
    music_type: str = "custom"
    id: Optional[int] = None
    url: str = ""
    audio: str = ""
    title: str = ""
    content: str = ""
    image: str = ""

    def toDict(self):
        """生成QQ音乐消息字段，显式保留内层音乐类型。"""
        data = {"type": self.music_type}
        if self.id is not None:
            data["id"] = self.id
        if self.url:
            data["url"] = self.url
        if self.audio:
            data["audio"] = self.audio
        if self.title:
            data["title"] = self.title
        if self.content:
            data["content"] = self.content
        if self.image:
            data["image"] = self.image
        return {"type": "music", "data": data}

    async def to_dict(self):
        return self.toDict()


class MusicRecommenderPlugin(Star):
    def __init__(self, context, config: Optional[AstrBotConfig] = None):
        """迁移配置并读取搜索与卡片参数；保存配置后需重载以重新读取。"""
        super().__init__(context)
        self.config = config or {}
        settings_config.migrate(self.config)


        self.music_source = settings_config.value(self.config, "music_source", "netease")
        self.send_mode = settings_config.value(self.config, "send_mode", "card")
        self.default_limit = int(settings_config.value(self.config, "default_limit", 3))
        self.card_count = int(settings_config.value(self.config, "card_count", 1))
        self.enable_tool = bool(settings_config.value(self.config, "enable_tool", True))


        self.show_album = bool(settings_config.value(self.config, "show_album", True))
        self.show_duration = bool(settings_config.value(self.config, "show_duration", True))
        self.show_link = bool(settings_config.value(self.config, "show_link", True))


        self.search_timeout = int(settings_config.value(self.config, "search_timeout", 10))
        self.max_results_hard_limit = int(settings_config.value(self.config, "max_results_hard_limit", 8))


        self.ark_token = str(settings_config.value(self.config, "ark_token", "") or "").strip()
        self.ark_bizsrc = str(settings_config.value(self.config, "ark_bizsrc", "music_card") or "").strip()
        self.ark_appid = int(settings_config.value(self.config, "ark_appid", 100495085))


        self.sign_api_url = str(settings_config.value(self.config, "sign_api_url", "") or "").strip()
        self.sign_api_key = str(settings_config.value(self.config, "sign_api_key", "") or "").strip()
        self.sign_api_format = str(settings_config.value(self.config, "sign_api_format", "") or "").strip()

    async def initialize(self):
        source_label = "网易云音乐" if self.music_source == "netease" else "QQ音乐"
        mode_label = {
            "card": "音乐卡片（依赖签名服务）",
            "ark": "音乐卡片（本地构造，不走签名服务）",
            "card_api": "音乐卡片（第三方签名服务）",
            "link": "链接文本",
        }.get(self.send_mode, self.send_mode)
        if self.send_mode == "card_api" and not (self.sign_api_url and self.sign_api_key):
            logger.warning(
                "[MusicRecommender] card_api 模式缺 sign_api_url / sign_api_key，"
                "会降级成文字说明")
        logger.info(
            f"[MusicRecommender] 音乐推荐插件加载成功：音源={source_label}，发送方式={mode_label}"
        )
        if not self.enable_tool:
            logger.info("[MusicRecommender] 音乐搜索工具已在配置中禁用")

    async def terminate(self):
        logger.info("[MusicRecommender] 插件已卸载")

    @filter.llm_tool(name="search_music")
    async def search_music(self, event: AstrMessageEvent, keyword: str, limit: str = ""):
        """我在需要查找或推荐歌曲时，用此工具搜索网易云或QQ音乐。
        我可以用歌名、歌手或主题作为关键词，并按需要指定数量。
        我根据工具返回的歌曲信息、卡片发送数量或失败说明，继续向对方介绍结果。
        
        Args:
            keyword(string): 我用于搜歌的关键词。
            limit(string): 我需要的结果数量；留空使用配置默认值。"""
        if not self.enable_tool:
            yield "音乐搜索功能当前已禁用，请在插件设置中开启。"
            return


        event = self._unwrap_event(event)

        keyword = str(keyword or "").strip()
        if not keyword:
            yield "搜索失败：keyword 不能为空"
            return


        if limit:
            try:
                limit_int = int(limit)
            except (TypeError, ValueError):
                limit_int = self.default_limit
        else:
            limit_int = self.default_limit
        limit_int = max(1, min(limit_int, self.max_results_hard_limit))

        try:
            if self.music_source == "qqmusic":
                songs = await self._search_qqmusic(keyword, limit=limit_int)
            else:
                songs = await self._search_netease(keyword, limit=limit_int)
        except Exception as e:
            logger.error(f"[MusicRecommender] 搜索异常: {e}\n{traceback.format_exc()}")
            yield f"搜索失败：{e}"
            return

        if not songs:
            yield f"没找到与'{keyword}'相关的歌曲，试试换个关键词？"
            return


        if self.send_mode in ("card", "ark", "card_api"):
            sender = {"card": self._send_music_card,
                      "ark": self._send_music_ark,
                      "card_api": self._send_music_ark_api}[self.send_mode]
            card_sent = 0
            for s in songs[: self.card_count]:
                try:
                    await sender(event, s)
                    card_sent += 1
                except Exception as e:
                    logger.warning(f"[MusicRecommender] 发送卡片失败: {e}")


            names = [s["song_name"] for s in songs[: self.card_count]]
            if card_sent > 0:
                yield (
                    f"已向用户发送 {card_sent} 张音乐卡片：{', '.join(names)}。"
                    f"请用简洁的语言说明一下推荐理由"
                )
            else:

                yield "音乐卡片发送失败，改为文字说明。" + self._format_songs_text(songs)
            return


        yield self._format_songs_text(songs)

    def _format_songs_text(self, songs: list[dict]) -> str:
        lines = [f"找到 {len(songs)} 首相关歌曲：\n"]
        for i, s in enumerate(songs, 1):
            parts = [f"{i}. {s['song_name']} - {s['artist']}"]
            if self.show_album and s.get("album"):
                parts.append(f"   专辑：{s['album']}")
            if self.show_duration and s.get("duration"):
                parts.append(f"   时长：{s['duration']}")
            if self.show_link and s.get("url"):
                parts.append(f"   链接：{s['url']}")
            lines.append("\n".join(parts))
        return "\n".join(lines)

    async def _send_music_card(self, event: AstrMessageEvent, song: dict) -> None:
        if self.music_source == "netease":
            song_id = song.get("song_id")
            if song_id is None:
                logger.warning(f"[MusicRecommender] 网易云歌曲缺少 song_id: {song}")
                return
            card = MusicCard(music_type="163", id=int(song_id))
        else:

            card = MusicCard(
                music_type="qq",
                url=song.get("url", ""),
                title=song.get("song_name", ""),
                content=song.get("artist", ""),
                image=song.get("cover", ""),
            )
        await event.send(MessageChain([card]))


    async def _send_music_ark_api(self, event: AstrMessageEvent, song: dict) -> None:
        """向配置的独立服务请求签名卡片，校验必要返回字段后发送JSON消息。"""
        if not (self.sign_api_url and self.sign_api_key):
            raise RuntimeError("card_api 缺 sign_api_url / sign_api_key")


        audio = str(song.get("audio") or song.get("audio_url") or "").strip()
        cover = str(song.get("cover") or song.get("cover_url") or "").strip()
        jump = str(song.get("url") or "").strip()
        if self.music_source == "netease" and song.get("song_id"):
            extra_cover, extra_audio = await self._netease_ark_extras(
                str(song["song_id"]))
            cover = cover or extra_cover
            audio = audio or extra_audio


        audio = audio or jump
        cover = cover or jump
        if not audio or not cover:
            raise RuntimeError(f"缺音频或封面，跳过卡片：{song.get('song_name')}")


        fmt = self.sign_api_format or ("netease" if self.music_source == "netease"
                                       else "qq")
        params = {
            "key": self.sign_api_key,
            "url": audio,
            "song": str(song.get("song_name") or ""),
            "singer": str(song.get("artist") or ""),
            "cover": cover,
            "jump": jump or audio,
            "format": fmt,
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.search_timeout)
            ) as session:
                async with session.get(self.sign_api_url, params=params) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"签名服务 HTTP {resp.status}")
                    result = await resp.json(content_type=None)
        except Exception as e:
            raise RuntimeError(f"签名服务请求失败: {e}")


        data = result.get("data") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("code") != 200
                or not isinstance(data, dict)
                or not {"app", "meta", "prompt", "view"}.issubset(data)):
            raise RuntimeError(f"签名服务返回形状不对：{str(result)[:200]}")

        await event.send(MessageChain([Json(data=json.dumps(data, ensure_ascii=False))]))


    async def _send_music_ark(self, event: AstrMessageEvent, song: dict) -> None:
        ark = await self._build_music_ark(song)
        await event.send(MessageChain([Json(data=ark)]))

    async def _build_music_ark(self, song: dict) -> dict:
        """按歌曲信息构造本地实验卡片；令牌与音源地址不保证QQ接受。"""
        title = str(song.get("song_name") or "").strip() or "未知歌曲"
        singer = str(song.get("artist") or "").strip() or "未知歌手"
        jump_url = str(song.get("url") or "").strip()
        cover = str(song.get("cover") or "").strip()
        audio = ""
        tag = "网易云音乐" if self.music_source == "netease" else "QQ音乐"

        if self.music_source == "netease" and song.get("song_id"):
            extra_cover, extra_audio = await self._netease_ark_extras(
                str(song["song_id"]))
            cover = cover or extra_cover
            audio = extra_audio

        now = int(time.time())
        token = self.ark_token or secrets.token_hex(16)
        return {
            "app": "com.tencent.music.lua",
            "bizsrc": self.ark_bizsrc,
            "desc": "音乐",
            "view": "music",
            "ver": "0.0.0.1",
            "prompt": f"[分享]{title}",
            "meta": {
                "music": {
                    "appid": self.ark_appid,
                    "action": "",
                    "android_pkg_name": "",
                    "app_type": 1,
                    "ctime": now,
                    "desc": singer,
                    "jumpUrl": jump_url,
                    "musicUrl": audio or jump_url,
                    "preview": cover or jump_url,
                    "sourceMsgId": "0",
                    "source_icon": "",
                    "source_url": "",
                    "tag": tag,
                    "title": title,
                }
            },
            "config": {
                "autosize": True,
                "ctime": now,
                "forward": True,
                "token": token,
                "type": "normal",
            },
        }

    async def _netease_ark_extras(self, song_id: str) -> tuple[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://music.163.com/",
            "Accept": "application/json",
        }
        cover = ""
        audio = ""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.search_timeout)
            ) as session:
                async with session.get(
                    NETEASE_DETAIL_API, params={"ids": f"[{song_id}]"},
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        detail = await resp.json(content_type=None)
                        songs = detail.get("songs") if isinstance(detail, dict) else None
                        if isinstance(songs, list) and songs:
                            album = songs[0].get("album") or {}
                            if isinstance(album, dict):
                                cover = str(album.get("picUrl") or "").strip()
                async with session.post(
                    NETEASE_PLAYER_API,
                    params={"id": song_id, "ids": f"[{song_id}]", "br": 128000},
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        data = payload.get("data") if isinstance(payload, dict) else None
                        if isinstance(data, list) and data:
                            audio = str((data[0] or {}).get("url") or "").strip()
        except Exception as e:
            logger.warning(f"[MusicRecommender] 取封面/音频失败（不影响卡片）: {e}")
        return cover, audio

    @staticmethod
    def _unwrap_event(event):
        try:
            from astrbot.core.agent.run_context import ContextWrapper

            if isinstance(event, ContextWrapper):
                return event.context.event
        except ImportError:
            pass
        return event

    async def _search_netease(self, keyword: str, limit: int = 5) -> list[dict]:
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://music.163.com/",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.search_timeout)
        ) as session:
            async with session.get(
                NETEASE_SEARCH_API, params=params, headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[MusicRecommender] 网易云 API 返回 {resp.status}")
                    return []
                data = await resp.json(content_type=None)

        songs = (data.get("result") or {}).get("songs") or []
        result = []
        for s in songs:
            song_id = s.get("id")
            song_name = s.get("name", "")
            artists = [a.get("name", "") for a in (s.get("artists") or [])]
            artist_str = " / ".join([a for a in artists if a])
            album = (s.get("album") or {}).get("name", "")
            duration_ms = s.get("duration", 0)
            duration_sec = duration_ms // 1000 if duration_ms else 0
            result.append({
                "song_id": song_id,
                "song_name": song_name,
                "artist": artist_str,
                "album": album,
                "duration": (
                    f"{duration_sec // 60}:{duration_sec % 60:02d}"
                    if duration_sec else ""
                ),
                "url": NETEASE_SONG_URL.format(song_id=song_id) if song_id else "",
            })
        return result

    async def _search_qqmusic(self, keyword: str, limit: int = 5) -> list[dict]:
        params = {
            "w": keyword,
            "p": 1,
            "n": limit,
            "format": "json",
            "cr": 1,
            "g_tk": 5381,
            "loginUin": 0,
            "hostUin": 0,
            "platform": "yqq",
            "needNewCode": 0,
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://y.qq.com/",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.search_timeout)
            ) as session:
                async with session.get(
                    QQMUSIC_SEARCH_API, params=params, headers=headers
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[MusicRecommender] QQ音乐 API 返回 {resp.status}")
                        return []
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[MusicRecommender] QQ音乐搜索失败: {e}")
            return []

        song_list = ((data.get("data") or {}).get("song") or {}).get("list") or []
        result = []
        for s in song_list:
            song_mid = s.get("songmid") or s.get("songMid") or ""
            song_name = s.get("songname") or s.get("songName") or ""
            singers = s.get("singer") or []
            artist_str = " / ".join(
                [singer.get("name", "") for singer in singers if singer.get("name")]
            )
            if isinstance(s.get("album"), dict):
                album = s.get("album", {}).get("name", "")
                album_mid = s.get("album", {}).get("mid", "")
            else:
                album = s.get("albumname") or ""
                album_mid = s.get("albummid") or ""
            duration_sec = s.get("interval", 0) or 0
            cover = (
                f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
                if album_mid
                else ""
            )
            result.append({
                "song_mid": song_mid,
                "song_name": song_name,
                "artist": artist_str,
                "album": album,
                "duration": (
                    f"{duration_sec // 60}:{duration_sec % 60:02d}"
                    if duration_sec else ""
                ),
                "url": QQMUSIC_SONG_URL.format(song_mid=song_mid) if song_mid else "",
                "cover": cover,
            })
        return result
