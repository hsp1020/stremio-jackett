import re
import hashlib
import os
import threading
import time
import urllib.parse
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

import bencode
import requests
from RTN import parse

from jackett.jackett_result import JackettResult
from torrent.torrent_item import TorrentItem
from utils.general import get_info_hash_from_magnet
from utils.general import season_episode_in_filename
from utils.logger import setup_logger

MAX_WORKERS = 200
REQUEST_TIMEOUT = 4

_thread_local = threading.local()

def get_thread_session():
    """스레드별 requests.Session 재사용"""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session

class TorrentService:
    def __init__(self):
        self.logger = setup_logger(__name__)

        self._prefetch_executor = None
        self._prefetch_futures = {}
        self._prefetch_lock = threading.Lock()

        self._perf_lock = threading.Lock()
        self._perf = {
            "converted": 0,
            "no_link": 0,
            "link_magnet": 0,
            "web_with_existing_magnet": 0,
            "web_without_existing_magnet": 0,
            "http_requests": 0,
            "http_sum": 0.0,
            "http_max": 0.0,
            "redirects": 0,
            "redirect_to_magnet": 0,
            "torrent_files": 0,
            "html_magnets": 0,
            "html_external": 0,
            "http_errors": 0,
        }

    def _prefetch_jackett_dl_magnet(self, link):
        """
        Jackett /dl/ URL의 첫 응답을 미리 받는다.

        Returns:
            (magnet_url, status_code)

        요청 자체가 실패한 경우:
            (None, None)
        """
        session = get_thread_session()
        start = time.perf_counter()

        try:
            response = session.get(
                link,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT
            )

            elapsed = time.perf_counter() - start
            status_code = response.status_code
            location = response.headers.get("Location")

            if (
                status_code in [301, 302, 303, 307, 308]
                and location
                and location.startswith("magnet:")
            ):
                self.logger.info(
                    f"[PERF][TorrentPrefetch] magnet resolved: "
                    f"{elapsed:.3f}s"
                )
                return location, status_code

            self.logger.info(
                f"[PERF][TorrentPrefetch] no magnet: "
                f"{elapsed:.3f}s / status={status_code}"
            )

            return None, status_code

        except Exception as e:
            self.logger.debug(
                f"[TorrentPrefetch] failed: {e}"
            )
            return None, None

    def start_web_prefetch(self, results: List[JackettResult]):
        """
        필터링/RTN이 실행되는 동안 Jackett /dl/ → magnet 요청을 병렬로 선행한다.
        결과가 필요 없어지는 경우에도 검색 결과 자체는 변경하지 않는다.
        """
        candidates = []

        for result in results:
            if getattr(result, "info_hash", None):
                continue

            magnet = getattr(result, "magnet", None)
            if magnet and "xt=urn:btih:" in str(magnet):
                continue

            link = str(getattr(result, "link", "") or "")

            if (
                link.startswith(("http://", "https://"))
                and "/dl/" in link
            ):
                candidates.append(link)

        candidates = list(dict.fromkeys(candidates))

        if not candidates:
            self.logger.info(
                "[PERF][TorrentPrefetch] candidates=0"
            )
            return

        workers = min(8, len(candidates))

        self._prefetch_executor = ThreadPoolExecutor(
            max_workers=workers
        )

        with self._prefetch_lock:
            for link in candidates:
                self._prefetch_futures[link] = (
                    self._prefetch_executor.submit(
                        self._prefetch_jackett_dl_magnet,
                        link
                    )
                )

        self.logger.info(
            f"[PERF][TorrentPrefetch] started: "
            f"candidates={len(candidates)} / workers={workers}"
        )

    def convert_and_process(self, results: List[JackettResult]):
        """Jackett 결과를 TorrentItem으로 변환하고 상세 정보 처리"""
        self.logger.debug(f"[TorrentService] 변환 시작. 대상 아이템 수: {len(results)}")
        torrent_items_result = []

        known_hashes = [
            str(getattr(r, "info_hash", "") or "").lower()
            for r in results
            if getattr(r, "info_hash", None)
        ]

        magnet_count = sum(
            1 for r in results
            if getattr(r, "magnet", None)
            and "xt=urn:btih:" in str(getattr(r, "magnet", ""))
        )

        http_link_count = sum(
            1 for r in results
            if str(getattr(r, "link", "") or "").startswith(("http://", "https://"))
        )

        self.logger.info(
            f"[PERF][TorrentService] INPUT: "
            f"items={len(results)} / "
            f"known_hash={len(known_hashes)} / "
            f"unique_hash={len(set(known_hashes))} / "
            f"hash_duplicates={len(known_hashes) - len(set(known_hashes))} / "
            f"magnet={magnet_count} / "
            f"http_link={http_link_count}"
        )

        if not results:
            return []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(self.__process_single, result) for result in results]

            success_count = 0
            fail_count = 0

            for future in as_completed(futures):
                try:
                    item = future.result()
                    if item:
                        torrent_items_result.append(item)
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    self.logger.error(f"Error processing torrent: {e}")

        self.logger.debug(f"[TorrentService] 변환 완료. 성공: {success_count}, 실패/스킵: {fail_count}, 총 반환: {len(torrent_items_result)}")

        with self._perf_lock:
            perf = dict(self._perf)

        self.logger.info(
            f"[PERF][TorrentService] PATHS: "
            f"converted={perf['converted']} / "
            f"no_link={perf['no_link']} / "
            f"link_magnet={perf['link_magnet']} / "
            f"web_with_existing_magnet={perf['web_with_existing_magnet']} / "
            f"web_without_existing_magnet={perf['web_without_existing_magnet']}"
        )

        self.logger.info(
            f"[PERF][TorrentService] HTTP: "
            f"requests={perf['http_requests']} / "
            f"summed={perf['http_sum']:.3f}s / "
            f"max={perf['http_max']:.3f}s / "
            f"redirects={perf['redirects']} / "
            f"redirect_to_magnet={perf['redirect_to_magnet']} / "
            f"torrent_files={perf['torrent_files']} / "
            f"html_magnets={perf['html_magnets']} / "
            f"html_external={perf['html_external']} / "
            f"errors={perf['http_errors']}"
        )

        if self._prefetch_executor is not None:
            self._prefetch_executor.shutdown(
                wait=False,
                cancel_futures=True
            )
            self._prefetch_executor = None

        return torrent_items_result

    def __process_single(self, result: JackettResult):
        """단일 JackettResult를 TorrentItem으로 변환하고 상세 정보 처리"""
        try:
            torrent_item = result.convert_to_torrent_item()
            torrent_item.detail_link = getattr(result, 'detail_link', result.link)

            with self._perf_lock:
                self._perf["converted"] += 1

            if not torrent_item.link:
                with self._perf_lock:
                    self._perf["no_link"] += 1
                return torrent_item

            if torrent_item.link.startswith("magnet:"):
                with self._perf_lock:
                    self._perf["link_magnet"] += 1

                torrent_item.magnet = torrent_item.link
                return self.__process_magnet(torrent_item)

            existing_magnet = getattr(torrent_item, "magnet", None)

            if existing_magnet and "xt=urn:btih:" in str(existing_magnet):
                with self._perf_lock:
                    self._perf["web_with_existing_magnet"] += 1
            else:
                with self._perf_lock:
                    self._perf["web_without_existing_magnet"] += 1

                self.logger.info(
                    f"[PERF][TorrentService] WEB ITEM: "
                    f"indexer={getattr(torrent_item, 'indexer', None)} / "
                    f"title={getattr(torrent_item, 'raw_title', None)} / "
                    f"link={getattr(torrent_item, 'link', None)} / "
                    f"detail={getattr(torrent_item, 'detail_link', None)}"
                )

            session = get_thread_session()
            return self.__process_web_url(torrent_item, session)

        except Exception as e:
            self.logger.error(f"Unexpected error in __process_single: {e}")
            return None

    def __process_web_url(self, result: TorrentItem, session: requests.Session):
        """Web URL 처리 - 케이스 1(마그넷 리다이렉트)과 케이스 2(토렌트 파일) 모두 대응"""
        current_url = result.link

        prefetch_future = None

        with self._prefetch_lock:
            prefetch_future = self._prefetch_futures.pop(
                current_url,
                None
            )

        if prefetch_future is not None:
            wait_start = time.perf_counter()

            try:
                prefetched = prefetch_future.result()

                if (
                    isinstance(prefetched, tuple)
                    and len(prefetched) == 2
                ):
                    (
                        prefetched_magnet,
                        prefetched_status,
                    ) = prefetched
                else:

                    prefetched_magnet = prefetched
                    prefetched_status = None

            except Exception:
                prefetched_magnet = None
                prefetched_status = None

            wait_elapsed = (
                time.perf_counter()
                - wait_start
            )

            self.logger.info(
                f"[PERF][TorrentPrefetch] consume: "
                f"wait={wait_elapsed:.3f}s / "
                f"hit={bool(prefetched_magnet)} / "
                f"status={prefetched_status}"
            )

            if prefetched_magnet:
                result.magnet = prefetched_magnet
                return self.__process_magnet(result)

            if prefetched_status == 404:
                self.logger.info(
                    "[PERF][TorrentPrefetch] "
                    "reuse 404 / duplicate HTTP skipped"
                )
                return result

        try:

            for _ in range(5):

                try:
                    perf_http_start = time.perf_counter()

                    response = session.get(
                        current_url,
                        allow_redirects=False,
                        timeout=REQUEST_TIMEOUT
                    )

                    perf_http_elapsed = time.perf_counter() - perf_http_start

                    with self._perf_lock:
                        self._perf["http_requests"] += 1
                        self._perf["http_sum"] += perf_http_elapsed
                        if perf_http_elapsed > self._perf["http_max"]:
                            self._perf["http_max"] = perf_http_elapsed

                except requests.RequestException as re:
                    with self._perf_lock:
                        self._perf["http_errors"] += 1

                    break

                if response.status_code in [301, 302, 303, 307, 308]:
                    with self._perf_lock:
                        self._perf["redirects"] += 1

                    location = response.headers.get("Location")
                    if not location:
                        break

                    if location.startswith("magnet:"):
                        with self._perf_lock:
                            self._perf["redirect_to_magnet"] += 1

                        result.magnet = location

                        return self.__process_magnet(result)

                    current_url = location
                    continue

                if response.status_code == 200:
                    content = response.content
                    ctype = response.headers.get("Content-Type", "").lower()

                    if "application/x-bittorrent" in ctype or content.startswith(b'd8:'):
                        with self._perf_lock:
                            self._perf["torrent_files"] += 1

                        return self.__process_torrent(result, content)

                    if "text/html" in ctype:
                        import re
                        magnet_match = re.search(r'magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}', response.text)
                        if magnet_match:
                            with self._perf_lock:
                                self._perf["html_magnets"] += 1

                            result.magnet = magnet_match.group(0)
                            return self.__process_magnet(result)

                        with self._perf_lock:
                            self._perf["html_external"] += 1

                        return result

                break

        except Exception as e:
            self.logger.debug(f"Failed to process URL {result.link}: {e}")

        return result

    def __process_torrent(self, result: TorrentItem, torrent_file):
        """.torrent 파일 내용 처리"""
        try:
            metadata = bencode.bdecode(torrent_file)
        except Exception as e:
            self.logger.error(f"Error decoding torrent file: {e}")
            return result

        result.torrent_download = result.link
        result.trackers = self.__get_trackers_from_torrent(metadata)
        result.info_hash = self.__convert_torrent_to_hash(metadata["info"])
        result.magnet = self.__build_magnet(
            result.info_hash,
            metadata["info"].get("name", ""),
            result.trackers
        )

        if "files" not in metadata["info"]:

            result.file_index = 1
            return result

        result.files = metadata["info"]["files"]

        if result.type == "series":
            file_details = self.__find_episode_file(
                result.files,
                result.parsed_data.seasons,
                result.parsed_data.episodes
            )

            if file_details is not None:

                result.file_index = file_details["file_index"]
                result.file_name = file_details["title"]
                result.size = file_details["size"]
            else:

                result.file_index = 1
        else:
            result.file_index = self.__find_movie_file(result.files)

        return result

    def __process_magnet(self, result: TorrentItem):
        """Magnet 링크 처리"""
        if result.magnet is None:
            result.magnet = result.link

        if result.info_hash is None:
            result.info_hash = get_info_hash_from_magnet(result.magnet)

        result.trackers = self.__get_trackers_from_magnet(result.magnet)

        return result

    def __convert_torrent_to_hash(self, torrent_contents):
        """Torrent info에서 info_hash 생성"""
        hashcontents = bencode.bencode(torrent_contents)
        hex_hash = hashlib.sha1(hashcontents).hexdigest()
        return hex_hash.lower()

    def __build_magnet(self, hash_val, display_name, trackers):
        """Info_hash와 tracker로 magnet 링크 생성"""
        magnet_base = "magnet:?xt=urn:btih:"
        magnet = f"{magnet_base}{hash_val}&dn={urllib.parse.quote(display_name)}"

        if trackers:
            magnet = f"{magnet}&tr={'&tr='.join([urllib.parse.quote(tracker) for tracker in trackers])}"

        return magnet

    def __get_trackers_from_torrent(self, torrent_metadata):
        """Torrent 메타데이터에서 tracker 추출"""
        announce = torrent_metadata.get("announce", [])
        announce_list = torrent_metadata.get("announce-list", [])

        trackers = set()
        if isinstance(announce, str):
            trackers.add(announce)
        elif isinstance(announce, list):
            for tracker in announce:
                trackers.add(tracker)

        for announce_list_item in announce_list:
            if isinstance(announce_list_item, list):
                for tracker in announce_list_item:
                    trackers.add(tracker)
            elif isinstance(announce_list_item, str):
                trackers.add(announce_list_item)

        return list(trackers)

    def __get_trackers_from_magnet(self, magnet: str):
        """Magnet 링크에서 tracker 추출"""
        url_parts = urllib.parse.urlparse(magnet)
        query_parts = urllib.parse.parse_qs(url_parts.query)

        trackers = []
        if "tr" in query_parts:
            trackers = query_parts["tr"]

        return trackers

    def __is_video_file(self, filename):
        """비디오 파일 확장자 체크"""
        video_extensions = {
            ".mkv", ".mp4", ".avi", ".mov",
            ".wmv", ".flv", ".webm", ".m4v", ".ts"
        }
        return any(filename.lower().endswith(ext) for ext in video_extensions)

    def __find_episode_file(self, file_structure, season, episode):
        """시리즈 에피소드 파일 찾기"""
        if len(season) == 0 or len(episode) == 0:
            return None

        target_season = season[0]
        target_episode = episode[0]

        file_index = 1
        episode_files = []

        for files in file_structure:
            length = files.get("length", 0)
            paths = files.get("path", [])

            for file in paths:

                if not self.__is_video_file(file):
                    continue

                if length < 50 * 1024 * 1024:
                    continue

                if not season_episode_in_filename(file, target_season, target_episode):
                    continue

                try:
                    parsed_file = parse(file)

                    if (
                        target_season in parsed_file.seasons
                        and target_episode in parsed_file.episodes
                    ):
                        episode_files.append(
                            {
                                "file_index": file_index,
                                "title": file,
                                "size": length,
                            }
                        )
                except Exception as e:
                    self.logger.warning(f"Error parsing filename {file}: {e}")
                    continue

            file_index += 1

        if not episode_files:
            return None

        return max(episode_files, key=lambda file: file["size"])

    def __find_movie_file(self, file_structure):
        """영화 파일 찾기 (가장 큰 비디오 파일)"""
        max_size = 0
        max_file_index = 1
        current_file_index = 1

        for files in file_structure:
            length = files.get("length", 0)
            paths = files.get("path", [])

            is_video = False
            for path_part in paths:
                if self.__is_video_file(path_part):
                    is_video = True
                    break

            if is_video and length > max_size:
                max_file_index = current_file_index
                max_size = length

            current_file_index += 1

        return max_file_index
