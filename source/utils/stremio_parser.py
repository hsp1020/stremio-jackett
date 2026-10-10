import json
from typing import List

from models.media import Media
from torrent.torrent_item import TorrentItem
from utils.logger import setup_logger
from utils.string_encoding import encodeb64

logger = setup_logger(__name__)

INSTANTLY_AVAILABLE = "[⚡]"
DOWNLOAD_REQUIRED = "[⬇️]"
DIRECT_TORRENT = "[🏴‍☠️]"

def get_emoji(language):
    emoji_dict = {
        "fr": "🇫🇷",
        "en": "🇬🇧",
        "es": "🇪🇸",
        "de": "🇩🇪",
        "it": "🇮🇹",
        "pt": "🇵🇹",
        "ru": "🇷🇺",
        "in": "🇮🇳",
        "nl": "🇳🇱",
        "hu": "🇭🇺",
        "la": "🇲🇽",
        "multi": "🌍"
    }
    return emoji_dict.get(language, "🇬🇧")

def filter_by_availability(item):
    if item["name"].startswith(INSTANTLY_AVAILABLE):
        return 0
    else:
        return 1

def filter_by_direct_torrent(item):
    if item["name"].startswith(DIRECT_TORRENT):
        return 1
    else:
        return 0

def parse_to_debrid_stream(torrent_item: TorrentItem, configb64, host, torrenting, debrid, results: list, media: Media):
    parsed_data = torrent_item.parsed_data

    size_in_gb = round(int(torrent_item.size) / 1024 / 1024 / 1024, 2)

    title = f"{torrent_item.raw_title}\n"
    if torrent_item.file_name is not None:
        title += f"{torrent_item.file_name}\n"

    title += f"👥 {torrent_item.seeders}   💾 {size_in_gb}GB   🔍 {torrent_item.indexer}\n"
    if parsed_data.codec:
        title += f"🎥 {parsed_data.codec.upper()}   "
    if parsed_data.audio:
        title += f"🎧 {', '.join(parsed_data.audio)}"
    if parsed_data.codec or parsed_data.audio:
        title += "\n"

    for language in torrent_item.languages:
        title += f"{get_emoji(language)}/"
    title = title[:-1]

    if debrid:
        if torrent_item.availability == True:
            name = f"{INSTANTLY_AVAILABLE}\n"
        else:
            name = f"{DOWNLOAD_REQUIRED}\n"

        name += f"{parsed_data.resolution or "Unknown"}" + (f" ({parsed_data.quality})" if parsed_data.quality else "")

        queryb64 = encodeb64(json.dumps(torrent_item.to_debrid_stream_query(media))).replace('=', '%3D')

        results.append({
            "name": name,
            "description": title,
            "url": f"{host}/playback/{configb64}/{queryb64}",
            "behaviorHints":{
                "bingeGroup": f"stremio-jackett-{torrent_item.info_hash}",
                "filename": torrent_item.file_name if torrent_item.file_name is not None else torrent_item.raw_title
            }
        })

    if torrenting and torrent_item.privacy == "public":
        name = f"{DIRECT_TORRENT}\n"
        if parsed_data.quality and parsed_data.quality != "Unknown" and \
                parsed_data.quality != "":
            name += f"({parsed_data.quality})"

        results.append({
            "name": name,
            "description": title,
            "infoHash": torrent_item.info_hash,
            "fileIdx": int(torrent_item.file_index) if torrent_item.file_index else None,
            "behaviorHints":{
                "bingeGroup": f"stremio-jackett-{torrent_item.info_hash}",
                "filename": torrent_item.file_name if torrent_item.file_name is not None else torrent_item.raw_title
            }
        })

def parse_to_stremio_streams(torrent_items: List[TorrentItem], config, media):
    stream_list = []
    configb64 = encodeb64(json.dumps(config).replace('=', '%3D'))
    debrid_enabled = config['debrid']

    # Formatting is CPU-only and short: preserve the selected item order
    # without starting one thread for every torrent.
    for torrent_item in torrent_items[:int(config['maxResults'])]:
        try:
            parse_to_debrid_stream(
                torrent_item, configb64, config['addonHost'],
                config['torrenting'], debrid_enabled, stream_list, media,
            )
        except Exception:
            logger.exception('Failed to format torrent: %s', torrent_item.raw_title)

    if len(stream_list) == 0:
        return []

    if config['debrid']:
        stream_list = sorted(stream_list, key=filter_by_availability)
        stream_list = sorted(stream_list, key=filter_by_direct_torrent)

    logger.debug(f"[StremioParser] Generated {len(stream_list)} streams")

    return stream_list
