"""用公开插件入口提供搜歌工具和指令；不接管模型上下文或全局发送。"""
from astrbot.api import AstrBotConfig,logger
from astrbot.api.event import AstrMessageEvent,MessageChain,filter
from astrbot.api.star import Context,Star,register
from astrbot.api import message_components as MC
from .music_core import Settings,MusicClient,MusicService

class MusicCard(MC.Music):
    """音乐载荷在插件自己的消息组件内编码，不修改SDK类。"""
    payload: dict

    def toDict(self):
        return {'type':'music','data':self.payload}

    async def to_dict(self):
        return self.toDict()

@register('astrbot_plugin_music_recommender','Akavilos','音乐搜索与卡片分享','1.1.0')
class MusicRecommenderPlugin(Star):
    def __init__(self,context:Context,config:AstrBotConfig):
        super().__init__(context)
        self.settings=Settings.from_mapping(config)
        if config.get('settings_layout_version',0)<1:
            legacy={**config,**{k:v for group in self.settings.grouped().values() for k,v in group.items() if k not in config}}
            self.settings=Settings.from_mapping({k:v for k,v in legacy.items() if not isinstance(v,dict)})
        config.update(self.settings.grouped());config['settings_layout_version']=2
        config.save_config()
        self.client=MusicClient(self.settings)
        self.service=MusicService(self.settings,self.client,logger)

    async def initialize(self):
        await self.client.open()
        logger.info('[音乐推荐] 搜歌工具与 /搜歌、/音乐 已加载')

    async def terminate(self):
        await self.client.close()

    async def recommend(self,event,keyword,limit=None):
        # 模型工具入口可能传入包装上下文，指令入口传入消息事件。
        if not hasattr(event,'unified_msg_origin'):event=event.context.event
        async def send(card):
            component=MusicCard(payload=card.data) if card.kind=='music' else MC.Json(data=card.data['data'])
            return await event.send(MessageChain(chain=[component]))
        return await self.service.recommend(keyword,limit,send)

    @filter.llm_tool(name='search_music')
    async def search_music(self,event:AstrMessageEvent,keyword:str,limit:int=0):
        """我需要查找或推荐歌曲时搜索歌名、歌手或关键词；返回真实歌曲与发送结果。

        Args:
            keyword(string): 我用于搜歌的歌名、歌手或关键词。
            limit(number): 我需要的候选数量；0表示沿用默认值。
        """
        return await self.recommend(event,keyword,limit or None)

    @filter.command('搜歌',alias={'音乐'})
    async def search_command(self,event:AstrMessageEvent):
        keyword=event.message_str.partition(' ')[2].strip()
        text=await self.recommend(event,keyword) if keyword else '用法：/搜歌 歌名、歌手或关键词；也可使用 /音乐。'
        event.stop_event()
        yield event.plain_result(text)
