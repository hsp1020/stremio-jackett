import asyncio
import logging
import os
import re
import shutil
import time
import zipfile

import requests
import starlette.status as status
from aiocron import crontab
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse

from debrid.get_debrid_service import get_debrid_service
from jackett.jackett_result import JackettResult
from jackett.jackett_service import JackettService
from metdata.cinemeta import Cinemeta
from metdata.tmdb import TMDB
from torrent.torrent_service import TorrentService
from torrent.torrent_smart_container import TorrentSmartContainer
from utils.cache import search_cache
from utils.filter_results import (
    filter_items,
    start_rtn_prewarm,
    shutdown_rtn_prewarm,
)
from utils.logger import setup_logger
from utils.parse_config import parse_config
from utils.stremio_parser import parse_to_stremio_streams
from utils.string_encoding import decodeb64

# [v1.1] 환경 변수 로드 로그
print("[main.py] Loading environment variables...")
load_dotenv()

root_path = os.environ.get("ROOT_PATH", None)
if root_path and not root_path.startswith("/"):
    root_path = "/" + root_path

# [v1.1] 앱 초기화 시작
app = FastAPI(root_path=root_path)

VERSION = "4.2.6"
isDev = os.getenv("NODE_ENV") == "development"
COMMUNITY_VERSION = True if os.getenv("IS_COMMUNITY_VERSION") == "true" else False
SPONSOR_MESSAGE = os.getenv("SPONSOR_MESSAGE")
ADDON_ID = os.getenv("ADDON_ID") if os.getenv("ADDON_ID") is not None else "community.aymene69.jackett"


class LogFilterMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope, receive)
            path = request.url.path
            # Configuration URLs contain API keys; never log their encoded values.
            safe_path = re.sub(r"^/[^/]+(?=/(?:configure|stream/|manifest\.json))", "/<CONFIG>", path)
            safe_path = re.sub(r"^/playback/[^/]+/[^/]+", "/playback/<CONFIG>/<QUERY>", safe_path)
            logger.info(f"{request.method} - {safe_path}")
        return await self.app(scope, receive, send)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not isDev:
    app.add_middleware(LogFilterMiddleware)

templates = Jinja2Templates(directory="templates")

logger = setup_logger(__name__)


@app.get("/")
async def root():
    return RedirectResponse(url="/configure")


@app.get("/configure")
@app.get("/{config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"isCommunityVersion": COMMUNITY_VERSION, "sponsorMessage": SPONSOR_MESSAGE, "version": VERSION}
    )


@app.get("/static/{file_path:path}")
async def function(file_path: str):
    response = FileResponse(f"templates/{file_path}")
    return response


@app.get("/manifest.json")
@app.get("/{params}/manifest.json")
async def get_manifest():
    return {
        "id": ADDON_ID,
        "icon": "https://i.imgur.com/tVjqEJP.png",
        "version": VERSION,
        "catalogs": [],
        "resources": ["stream"],
        "types": ["movie", "series"],
        "name": "Jackett" + (" Community" if COMMUNITY_VERSION else "") + (" (Dev)" if isDev else ""),
        "description": "Elevate your Stremio experience with seamless access to Jackett torrent links, effortlessly "
                       "fetching torrents for your selected movies within the Stremio interface.",
        "behaviorHints": {
            "configurable": True,
            # "configurationRequired": True
        }
    }


formatter = logging.Formatter('[%(asctime)s] p%(process)s {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s',
                              '%m-%d %H:%M:%S')

logger.info("Started Jackett Addon")


@app.get("/{config}/stream/{stream_type}/{stream_id}")
async def get_results(config: str, stream_type: str, stream_id: str, request: Request):
    logger.info(f"==== NEW REQUEST ARRIVED: {stream_type} | ID: {stream_id} ====")
    jackett_service = None
    start = time.time()
    stream_id = stream_id.replace(".json", "")

    config = parse_config(config)
    logger.info(stream_type + " request")

    # 1. 메타데이터 제공자 설정
    if config['metadataProvider'] == "tmdb" and config['tmdbApi']:
        metadata_provider = TMDB(config)
        if not COMMUNITY_VERSION and config['jackett']:
            logger.info(f"Getting indexers' languages from Jackett for setting up TMDB")

            perf_indexers_start = time.perf_counter()

            jackett_service = JackettService(config)
            metadata_provider.indexers = jackett_service.get_indexers()

            logger.info(
                f"[PERF] Jackett indexer setup: "
                f"{time.perf_counter() - perf_indexers_start:.3f}s"
            )
    else:
        metadata_provider = Cinemeta(config)

    logger.info(f"Getting media from {config['metadataProvider']}")

    perf_metadata_start = time.perf_counter()

    media = metadata_provider.get_metadata(stream_id, stream_type)

    logger.info(
        f"[PERF] Metadata provider get_metadata: "
        f"{time.perf_counter() - perf_metadata_start:.3f}s"
    )

    logger.info("Got media and properties: " + str(media.titles))

    debrid_service = get_debrid_service(config)

    torrent_service = TorrentService()

    search_results = []
    # 2. 캐시 검색
    if COMMUNITY_VERSION or config['cache']:
        logger.info("Getting cached results")
        cached_results = search_cache(media)
        cached_results = [JackettResult().from_cached_item(torrent, media) for torrent in cached_results]
        logger.info("Got " + str(len(cached_results)) + " cached results")

        if len(cached_results) > 0:
            logger.info("Filtering cached results")
            search_results = filter_items(cached_results, media, config=config)
            logger.info("Filtered cached results")

    # 3. Jackett 검색 (캐시 부족 시)
    if not COMMUNITY_VERSION and config['jackett'] and len(search_results) < int(config['maxResults']):
        if len(search_results) > 0 and config['cache']:
            logger.info("Not enough cached results found (results: " + str(len(search_results)) + ")")
        elif config['cache']:
            logger.info("No cached results found")

        logger.info("Searching for results on Jackett")
        jackett_service = jackett_service if jackett_service else JackettService(config)

        rtn_prewarm = None

        def _start_request_rtn_prewarm(found_count):
            nonlocal rtn_prewarm

            if rtn_prewarm is None:
                rtn_prewarm = start_rtn_prewarm()

        jackett_search_results = jackett_service.search(
            media,
            prewarm_callback=_start_request_rtn_prewarm,
            prewarm_threshold=2000,
        )

        logger.info("Got " + str(len(jackett_search_results)) + " results from Jackett")

        # Jackett /dl/ magnet resolve를 RTN/filter와 병렬 실행

        logger.info("Filtering Jackett results")

        perf_filter_start = time.perf_counter()

        try:
            filtered_jackett_search_results = filter_items(
                jackett_search_results,
                media,
                config=config,
                prefetch_callback=torrent_service.start_web_prefetch,
                rtn_prewarm=rtn_prewarm,
            )
        finally:
            shutdown_rtn_prewarm(
                rtn_prewarm
            )

        logger.info(
            f"[PERF] filter_items TOTAL: "
            f"{time.perf_counter() - perf_filter_start:.3f}s / "
            f"{len(jackett_search_results)} -> "
            f"{len(filtered_jackett_search_results)}"
        )

        logger.info("Filtered Jackett results")

        search_results.extend(filtered_jackett_search_results)

    # 4. 결과 변환
    logger.debug("Converting result to TorrentItems (results: " + str(len(search_results)) + ")")

    perf_torrent_start = time.perf_counter()

    torrent_results = torrent_service.convert_and_process(search_results)

    logger.info(
        f"[PERF] TorrentService TOTAL: "
        f"{time.perf_counter() - perf_torrent_start:.3f}s / "
        f"{len(search_results)} -> {len(torrent_results)}"
    )

    logger.debug("Converted result to TorrentItems (results: " + str(len(torrent_results)) + ")")

    perf_dedup_start = time.perf_counter()

    torrent_smart_container = TorrentSmartContainer(
        torrent_results,
        media
    )

    logger.info(
        f"[PERF] SmartContainer dedup: "
        f"{time.perf_counter() - perf_dedup_start:.3f}s / "
        f"input={len(torrent_results)}"
    )

    # 5. Debrid 가용성 확인
    if config['debrid']:
        if config['service'] in ["torbox", "premiumize"]:
            logger.debug("Checking availability")
            hashes = torrent_smart_container.get_hashes()
            ip = request.client.host
            result = debrid_service.get_availability_bulk(hashes, ip)
            torrent_smart_container.update_availability(result, type(debrid_service), media)
            logger.debug("Checked availability (results: " + str(len(result.items())) + ")")

    # 6. 중복 제거된 결과 사용 (최종 정렬은 Aiostreams에서 처리)
    perf_getitems_start = time.perf_counter()

    results = torrent_smart_container.get_items()

    logger.info(
        f"[PERF] SmartContainer get_items: "
        f"{time.perf_counter() - perf_getitems_start:.3f}s / "
        f"results={len(results)}"
    )

    logger.debug(f"Got results (results: {len(results)})")

    logger.info("Processing results")

    perf_parser_start = time.perf_counter()

    # 7. 스트림 리스트 생성 (일반 토렌트/디브리드)
    stream_list = parse_to_stremio_streams(
        results,
        config,
        media
    )

    logger.info(
        f"[PERF] Stremio parser: "
        f"{time.perf_counter() - perf_parser_start:.3f}s / "
        f"items={len(results)} / streams={len(stream_list)}"
    )

    perf_external_start = time.perf_counter()

    # 8. EXTERNAL 아이템 추가
    for item in results:
        # info_hash도 없고 마그넷도 없으면 External로 간주 (단, 마그넷이 있으면 스트림 파서가 처리함)
        if not item.info_hash and not (item.magnet and "xt=urn:btih:" in item.magnet):
            target_url = getattr(item, 'detail_link', None) or item.link
            if target_url:
                stream_list.append({
                    "name": "External",
                    "description": f"{item.raw_title}\n 🔍 {item.indexer} (External)",
                    "externalUrl": target_url,
                    "behaviorHints": {
                        "notVideo": True,
                        "bingeGroup": f"jackett-ext-{item.raw_title}"
                    }
                })

    logger.info(
        f"[PERF] External append: "
        f"{time.perf_counter() - perf_external_start:.3f}s"
    )

    # 9. 통계 아이템 추가
    elapsed_time = time.time() - start
    result_count = len(stream_list)

    stats_item = {
        "name": "Result",
        "title": f"[ Found {result_count} items ] in {elapsed_time:.2f}s",
        "url": "https://github.com/aymene69/stremio-jackett"
    }
    stream_list.append(stats_item)

    logger.info(f"Processed results (results: {result_count})")
    logger.info(f"Total time: {elapsed_time:.2f}s")

    return {"streams": stream_list}


# [v1.1] HEAD, GET 통합 및 중복 제거
@app.api_route("/playback/{config}/{query}", methods=["GET", "HEAD"])
async def get_playback(config: str, query: str, request: Request):
    try:
        if not query:
            raise HTTPException(status_code=400, detail="Query required.")

        logger.info("[Playback] Request received")
        config = parse_config(config)

        logger.info("Decoding query")
        query = decodeb64(query)
        logger.debug("Playback query decoded")

        ip = request.client.host
        debrid_service = get_debrid_service(config)
        link = debrid_service.get_stream_link(query, ip)

        logger.debug("Playback link resolved")
        return RedirectResponse(url=link, status_code=status.HTTP_301_MOVED_PERMANENTLY)

    except Exception as e:
        logger.error(f"An error occurred in playback: {e}")
        raise HTTPException(status_code=500, detail="An error occurred while processing the request.")

# [v1.1] 여기에 있던 중복된 @app.head("/playback/...") 삭제됨
