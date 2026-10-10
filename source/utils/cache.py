import json
import os
from typing import List

import requests

from constants import CACHER_URL, EXCLUDED_TRACKERS
from torrent.torrent_item import TorrentItem
from utils.logger import setup_logger

logger = setup_logger(__name__)

def search_cache(media):
    logger.info("Searching for cached " + media.type + " results")
    url = CACHER_URL + "getResult/" + media.type + "/"

    cache_search = media.__dict__.copy()
    cache_search['title'] = cache_search['titles'][0]
    cache_search['language'] = cache_search['languages'][0]

    try:
        response = requests.get(url, json=cache_search, timeout=(3, 8))
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning('Cache lookup failed: %s', type(exc).__name__)
    return []

def cache_results(torrents: List[TorrentItem], media):
    if os.getenv("NODE_ENV") == "development":
        return

    logger.info("Started caching results")

    cache_items = []
    for torrent in torrents:
        if torrent.indexer in EXCLUDED_TRACKERS:
            continue

        try:
            cache_item = dict()

            cache_item['title'] = torrent.raw_title
            cache_item['trackers'] = "tracker:".join(torrent.trackers)
            cache_item['magnet'] = torrent.magnet
            cache_item['files'] = []
            cache_item['hash'] = torrent.info_hash
            cache_item['indexer'] = torrent.indexer
            cache_item['quality'] = torrent.parsed_data.resolution
            cache_item['qualitySpec'] = torrent.parsed_data.quality
            cache_item['seeders'] = torrent.seeders
            cache_item['size'] = torrent.size
            cache_item['language'] = ";".join(torrent.languages)
            cache_item['type'] = media.type
            cache_item['availability'] = torrent.availability

            if media.type == "movie":
                cache_item['year'] = media.year
            elif media.type == "series":
                cache_item['season'] = media.season
                cache_item['episode'] = media.episode
                cache_item['seasonfile'] = False

            cache_items.append(cache_item)
        except:
            logger.exception("An exception occured durring cache parsing")
            pass

    try:
        url = f"{CACHER_URL}pushResult/{media.type}"
        cache_data = json.dumps(cache_items, indent=4)
        response = requests.post(url, data=cache_data, timeout=(3, 12))
        response.raise_for_status()

        if response.status_code == 200:
            logger.info(f"Cached {str(len(cache_items))} {media.type} results")
        else:
            logger.error(f"Failed to cache {media.type} results: {str(response)}")
    except:
        logger.error("Failed to cache results")
        pass
