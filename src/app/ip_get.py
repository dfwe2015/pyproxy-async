import asyncio

import aiohttp

from app.ip_factory import IPFactory
from app.main import Config, Logger
from lib.exceptions import EmptyResponseException, RetryException, MaxRetryException
from lib.helper import ShareInstance
from lib.redis_lib import Redis
from lib.func import retry
from lib.structs import SiteData, SiteResponseData


class SiteResponse:
    text: str = ''
    url: str = ''

    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url

    def json(self):
        import json
        return json.loads(self.text)

    def xpath(self, *args, **kwargs):
        # TODO 完善
        from lxml import etree
        from lxml.etree import _Element
        tree: _Element = etree.HTML(self.text)
        return tree.xpath(*args, **kwargs)


class IPGet(ShareInstance):
    _configs: dict = {}
    _parsers: dict = {}
    _test_model = False

    async def run(self):
        runner = self.crawl_task
        tasks = [runner()]
        await asyncio.ensure_future(asyncio.wait(tasks))

    async def crawl_task(self):
        while True:
            Logger.debug('[get] crawl task loop')
            await self.start_crawl()
            if Config.APP_ENV == Config.AppEnvType.TEST:
                break
            await asyncio.sleep(Config.DEFAULT_CRAWL_SITES_INTERVAL)

    async def start_crawl(self):
        for key, site in self._configs.items():
            assert isinstance(site, SiteData)
            if site.enabled:
                await self.crawl_site(site)

    @classmethod
    async def push_to_pool(cls, ips):
        from app.ip_checker import IPChecker
        if not isinstance(ips, list):
            ips = [ips]
        with await Redis.share() as redis:
            needs_ip = []
            for ip in ips:
                exists = await redis.zscore(Config.REDIS_KEY_IP_POOL, ip)
                if exists is not None:
                    continue
                await redis.zadd(Config.REDIS_KEY_IP_POOL, Config.DEFAULT_SCORE, ip)
                needs_ip.append(ip)
            if needs_ip:
                await IPChecker.push_to_pool(needs_ip)
            Logger.info('[get] send %d ip to ip pools' % len(needs_ip))
        return len(ips)

    async def crawl_site(self, site: SiteData, page_limit: int = 0):
        headers = {
            'User-Agent': self.get_user_agent()
        }
        headers.update(site.headers)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(Config.DEFAULT_REQUEST_TIME_OUT),
                                         headers=headers) as session:
            pages = site.pages if page_limit == 0 else site.pages[0:page_limit]
            for page in pages:
                try:
                    await self.crawl_single_page(session, page, site)
                except MaxRetryException as e:
                    Logger.warn('[get] Max retry skip, message: %s' % str(e))
                    continue
                finally:
                    if site.page_interval:
                        await asyncio.sleep(site.page_interval)

    @retry()
    async def crawl_single_page(self, session, page, site: SiteData):
        proxy = None
        if site.use_proxy is True:
            random_proxy = await IPFactory.get_random_ip(page.find('https') == 0)
            if random_proxy:
                proxy = random_proxy.to_http()
        try:
            async with session.get(page, proxy=proxy) as resp:
                text = await resp.text()
                if not text:
                    raise EmptyResponseException('empty text')
                site_resp = SiteResponse(text, url=page)
            await self.parse_site(site, site_resp)
        except Exception as e:
            Logger.error('[get] Get page %s error, message: %s' % (page, str(e)))
            raise RetryException() from e

    @classmethod
    async def test_crawl(cls, key: str, page_limit: int = 3):
        self = cls.share()
        self._test_model = True
        site = self._configs.get(key)
        await self.crawl_site(site=site, page_limit=page_limit)

    async def parse_site(self, site: SiteData, resp: SiteResponse):
        parser = self._parsers.get(site.key)
        if not parser:
            return
        try:
            result = parser(resp)
            if not self._test_model:
                await self.save_parse_result(result)
            else:
                self.show_result(resp, result)
        except Exception as e:
            Logger.error('[get] Parse error, message: %s' % str(e))

    async def save_parse_result(self, result):
        ips = []
        for item in result:
            if not isinstance(item, SiteResponseData):
                continue
            ips.append(item.to_str())
        if ips:
            Logger.info('[get] Get %d new ip' % len(ips))
            await self.push_to_pool(ips)

    def show_result(self, resp: SiteResponse, result):
        Logger.info('[get] Url: %s' % resp.url)
        for item in result:
            if not isinstance(item, SiteResponseData):
                continue
            Logger.info('[get] Get ip: %s' % item.to_str())

    @classmethod
    def config(cls, name):
        self = IPGet.share()

        def decorator(f):
            res = f()
            assert isinstance(res, SiteData), 'Config must be instance of SiteData'
            res.key = name
            self._configs[name] = res
            return f

        return decorator

    @classmethod
    def parse(cls, name):
        self = cls.share()

        def decorator(f):
            self._parsers[name] = f
            return f

        return decorator

    def get_user_agent(self) -> str:
        import random
        return 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%d.0.3770.80 Safari/537.36' % random.randint(
            70, 76)


if __name__ == '__main__':
    from lib.func import run_until_complete
    from sites import *
    from app.ip_get import IPGet, SiteResponse

    run_until_complete(IPGet.share().run())