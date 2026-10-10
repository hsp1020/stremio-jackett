import json
import time
from urllib.parse import unquote

import requests

from constants import NO_CACHE_VIDEO_URL
from debrid.base_debrid import BaseDebrid
from utils.general import get_info_hash_from_magnet
from utils.general import is_video_file
from utils.general import season_episode_in_filename
from utils.logger import setup_logger

logger = setup_logger(__name__)


class RealDebrid(BaseDebrid):
    def __init__(self, config):
        super().__init__(config)
        self.base_url = "https://api.real-debrid.com"
        self.headers = {"Authorization": f"Bearer {self.config['debridKey']}"}

    def add_magnet(self, magnet, ip=None):
        url = f"{self.base_url}/rest/1.0/torrents/addMagnet"
        data = {"magnet": magnet}
        return self.get_json_response(url, method='post', headers=self.headers, data=data)

    def add_torrent(self, torrent_file):
        url = f"{self.base_url}/rest/1.0/torrents/addTorrent"
        return self.get_json_response(url, method='put', headers=self.headers, data=torrent_file)

    def delete_torrent(self, id):
        url = f"{self.base_url}/rest/1.0/torrents/delete/{id}"
        return self.get_json_response(url, method='delete', headers=self.headers)

    def get_torrent_info(self, torrent_id):
        logger.info(f"Getting torrent info for: {torrent_id}")
        url = f"{self.base_url}/rest/1.0/torrents/info/{torrent_id}"
        torrent_info = self.get_json_response(url, headers=self.headers)

        if not torrent_info or 'files' not in torrent_info:
            return None

        return torrent_info

    def select_files(self, torrent_id, file_id):
        logger.info(f"Selecting file(s): {file_id}")
        url = f"{self.base_url}/rest/1.0/torrents/selectFiles/{torrent_id}"
        data = {"files": str(file_id)}
        requests.post(url, headers=self.headers, data=data, timeout=(5, 20))

    def unrestrict_link(self, link):
        url = f"{self.base_url}/rest/1.0/unrestrict/link"
        data = {"link": link}
        return self.get_json_response(url, method='post', headers=self.headers, data=data)

    def is_already_added(self, magnet):
        hash = magnet.split("urn:btih:")[1].split("&")[0].lower()
        url = f"{self.base_url}/rest/1.0/torrents"
        torrents = self.get_json_response(url, headers=self.headers)
        for torrent in torrents:
            if torrent['hash'].lower() == hash:
                return torrent['id']
        return False

    def wait_for_link(self, torrent_id, timeout=30, interval=5):
        start_time = time.time()
        while time.time() - start_time < timeout:
            torrent_info = self.get_torrent_info(torrent_id)
            if torrent_info and 'links' in torrent_info and len(torrent_info['links']) > 0:
                return torrent_info['links']
            time.sleep(interval)

        return None

    def get_availability_bulk(self, hashes_or_magnets, ip=None):
        if len(hashes_or_magnets) == 0:
            logger.info("No hashes to be sent to Real-Debrid.")
            return dict()

        url = f"{self.base_url}/torrents/"
        ids = []
        for element in self.get_json_response(url)["data"]["hash"]:
            if element["hash"] in hashes_or_magnets:
                ids.append(element["id"])
        return self.get_json_response(url, headers=self.headers)

    def get_stream_link(self, query_string, ip=None):
        query = json.loads(query_string)

        magnet = query['magnet']
        stream_type = query['type']
        file_index = int(query['file_index']) if query.get('file_index') is not None else None
        file_name = query.get('file_name')
        season = query['season']
        episode = query['episode']
        torrent_download = unquote(query["torrent_download"]) if query["torrent_download"] is not None else None
        info_hash = get_info_hash_from_magnet(magnet)
        logger.info(f"RealDebrid get stream link for {stream_type} with hash: {info_hash}")

        cached_torrent_ids = self.__get_cached_torrent_ids(info_hash)
        logger.info(f"Found {len(cached_torrent_ids)} cached torrents with hash: {info_hash}")

        torrent_info = None
        if len(cached_torrent_ids) > 0:
            if stream_type == "movie":
                torrent_info = self.get_torrent_info(cached_torrent_ids[0])
            elif stream_type == "series":
                torrent_info = self.__get_cached_torrent_info(
                    cached_torrent_ids, file_index, season, episode, file_name
                )
            else:
                return "Error: Unsupported stream type."

        # The torrent is not yet added
        if torrent_info is None:
            torrent_info = self.__add_magnet_or_torrent(magnet, torrent_download)
            if not torrent_info or 'files' not in torrent_info:
                return "Error: Failed to get torrent info."

            logger.info("Selecting file")
            chosen = self.__select_file(
                torrent_info, stream_type, file_index, season, episode, file_name
            )
            if chosen is None:
                logger.warning("No matching video file in Real-Debrid torrent %s", info_hash)
                return NO_CACHE_VIDEO_URL

            # == operator, to avoid adding the season pack twice and setting 5 as season pack treshold
            if len(cached_torrent_ids) == 0 and stream_type == "series" and len(torrent_info["files"]) > 5:
                logger.info("Prefetching season pack")
                prefetched_torrent_info = self.__prefetch_season_pack(magnet, torrent_download)
                if len(prefetched_torrent_info["links"]) > 0:
                    self.delete_torrent(torrent_info["id"])
                    torrent_info = prefetched_torrent_info

        torrent_id = torrent_info["id"]
        logger.info(f"Waiting for the link(s) to be ready for torrent ID: {torrent_id}")
        # Waiting for the link(s) to be ready
        links = self.wait_for_link(torrent_id)
        if links is None:
            return NO_CACHE_VIDEO_URL

        if len(links) > 1:
            logger.info("Finding appropriate link")
            # Selection / season-pack prefetch can change the selected set.
            fresh_torrent_info = self.get_torrent_info(torrent_id)
            if fresh_torrent_info:
                torrent_info = fresh_torrent_info
            download_link = self.__find_appropiate_link(
                torrent_info, links, stream_type, file_index, season, episode, file_name
            )
        else:
            download_link = links[0]

        if not download_link or download_link == NO_CACHE_VIDEO_URL:
            return NO_CACHE_VIDEO_URL

        logger.info(f"Unrestricting the download link: {download_link}")
        # Unrestricting the download link
        unrestrict_response = self.unrestrict_link(download_link)
        if not unrestrict_response or 'download' not in unrestrict_response:
            return "Error: Failed to unrestrict link."

        logger.info(f"Got download link: {unrestrict_response['download']}")
        return unrestrict_response['download']

    def __get_cached_torrent_ids(self, info_hash):
        url = f"{self.base_url}/rest/1.0/torrents"
        torrents = self.get_json_response(url, headers=self.headers)

        logger.info(f"Searching users real-debrid downloads for {info_hash}")
        torrent_ids = []
        for torrent in torrents:
            if torrent['hash'].lower() == info_hash:
                torrent_ids.append(torrent['id'])

        return torrent_ids

    @staticmethod
    def __file_matches_name(file, file_name):
        if not file_name:
            return False
        return str(file.get('path', '')).replace('\\', '/').rsplit('/', 1)[-1].casefold() == (
            str(file_name).replace('\\', '/').rsplit('/', 1)[-1].casefold()
        )

    def __choose_file(self, files, stream_type, file_index, season, episode, file_name):
        videos = [
            f for f in files if is_video_file(str(f.get('path', '')).lower())
        ]
        if not videos:
            return None

        # file_index is only a provider-issued file ID supplied by the
        # Real-Debrid availability response, never a .torrent position.
        by_id = next(
            (f for f in videos if file_index is not None
             and str(f.get('id')) == str(file_index)), None
        )
        by_name = next(
            (f for f in videos if self.__file_matches_name(f, file_name)), None
        )

        if stream_type == 'movie':
            return by_id or by_name or max(videos, key=lambda f: f.get('bytes') or 0)

        if stream_type != 'series':
            return None

        matches = []
        if season is not None and episode is not None:
            for f in videos:
                try:
                    if season_episode_in_filename(str(f['path']).lower(), season, episode):
                        matches.append(f)
                except (AttributeError, TypeError, ValueError):
                    continue

        if matches:
            if by_name in matches:
                return by_name
            if by_id in matches:
                return by_id
            return max(matches, key=lambda f: f.get('bytes') or 0)

        # Known filename from .torrent or provider data is usable even
        # when the episode parser cannot identify an unconventional name.
        if by_name:
            return by_name
        if by_id:
            return by_id
        if len(videos) == 1:
            # Only use the single-file fallback if its name does not
            # explicitly identify a different episode or season.
            try:
                from RTN import parse
                parsed = parse(str(videos[0]['path']))
                requested_season = int(str(season).replace('S', '')) if season is not None else None
                requested_episode = int(str(episode).replace('E', '')) if episode is not None else None
                if parsed.episodes and requested_episode not in parsed.episodes:
                    return None
                if parsed.seasons and requested_season not in parsed.seasons:
                    return None
            except (AttributeError, TypeError, ValueError):
                pass
            return videos[0]
        return None

    def __get_cached_torrent_info(self, cached_ids, file_index, season, episode, file_name=None):
        candidates = []
        for torrent_id in cached_ids:
            info = self.get_torrent_info(torrent_id)
            if self.__torrent_contains_file(info, file_index, season, episode, file_name):
                if info.get('links'):
                    return info
                candidates.append(info)
        return max(candidates, key=lambda x: x.get('progress') or 0) if candidates else None

    def __torrent_contains_file(self, torrent_info, file_index, season, episode, file_name=None):
        if not torrent_info or 'files' not in torrent_info:
            return False
        selected = [f for f in torrent_info['files'] if f.get('selected') == 1]
        return self.__choose_file(
            selected, 'series', file_index, season, episode, file_name
        ) is not None

    def __add_magnet_or_torrent(self, magnet, torrent_download=None):
        torrent_id = ""
        if torrent_download is None:
            logger.info(f"Adding magnet to RealDebrid")
            magnet_response = self.add_magnet(magnet)
            logger.info(f"RealDebrid add magnet response: {magnet_response}")

            if not magnet_response or 'id' not in magnet_response:
                return "Error: Failed to add magnet."

            torrent_id = magnet_response['id']
        else:
            logger.info(f"Downloading torrent file from Jackett")
            torrent_file = self.donwload_torrent_file(torrent_download)
            logger.info(f"Torrent file downloaded from Jackett")

            logger.info(f"Adding torrent file to RealDebrid")
            upload_response = self.add_torrent(torrent_file)
            logger.info(f"RealDebrid add torrent file response: {upload_response}")

            if not upload_response or 'id' not in upload_response:
                return "Error: Failed to add torrent file."

            torrent_id = upload_response['id']

        logger.info(f"New torrent ID: {torrent_id}")
        return self.get_torrent_info(torrent_id)

    def __prefetch_season_pack(self, magnet, torrent_download):
        torrent_info = self.__add_magnet_or_torrent(magnet, torrent_download)
        video_file_indexes = []

        for file in torrent_info["files"]:
            if is_video_file(file["path"]):
                video_file_indexes.append(str(file["id"]))

        self.select_files(torrent_info["id"], ",".join(video_file_indexes))
        time.sleep(10)
        return self.get_torrent_info(torrent_info["id"])

    def __select_file(self, torrent_info, stream_type, file_index, season, episode, file_name=None):
        file = self.__choose_file(
            torrent_info.get('files', []), stream_type, file_index, season, episode, file_name
        )
        if file is None:
            return None
        file_id = file['id']
        logger.info("Selecting Real-Debrid file ID: %s", file_id)
        self.select_files(torrent_info['id'], file_id)
        return file_id

    def __find_appropiate_link(self, torrent_info, links, stream_type,
                               file_index, season, episode, file_name=None):
        selected = [f for f in torrent_info.get('files', []) if f.get('selected') == 1]
        file = self.__choose_file(
            selected, stream_type, file_index, season, episode, file_name
        )
        if file is None:
            return NO_CACHE_VIDEO_URL
        for index, selected_file in enumerate(selected):
            if str(selected_file.get('id')) == str(file['id']):
                if index < len(links):
                    return links[index]
                break
        logger.warning("Real-Debrid selected file/link mismatch")
        return NO_CACHE_VIDEO_URL
