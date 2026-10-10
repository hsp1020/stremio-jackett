import threading
from typing import List, Dict

# [v1.1] 기존 임포트 유지
from RTN import parse

from debrid.alldebrid import AllDebrid
from debrid.premiumize import Premiumize
from debrid.realdebrid import RealDebrid
from debrid.torbox import TorBox
from torrent.torrent_item import TorrentItem
from utils.cache import cache_results
from utils.general import season_episode_in_filename
from utils.logger import setup_logger
from jackett.jackett_indexer import JackettIndexer
from jackett.jackett_result import JackettResult


class TorrentSmartContainer:
    def __init__(self, torrent_items: List[TorrentItem], media):
        self.logger = setup_logger(__name__)
        # [v1.1] 초기화 로그 추가
        self.logger.debug(f"[TorrentSmartContainer] 초기화 시작. 입력 아이템 수: {len(torrent_items)}")
        self.__itemsDict: Dict[TorrentItem] = self.__build_items_dict_by_infohash(torrent_items)
        self.__media = media
        self.logger.debug(f"[TorrentSmartContainer] 초기화 완료. 중복 제거 후 아이템 수: {len(self.__itemsDict)}")

    def get_hashes(self):
        hashes = list(self.__itemsDict.keys())
        self.logger.debug(f"[get_hashes] 해시 목록 반환 (개수: {len(hashes)})")
        return hashes

    def get_items(self):
        items = list(self.__itemsDict.values())
        self.logger.debug(f"[get_items] 아이템 목록 반환 (개수: {len(items)})")
        return items

    def get_direct_torrentable(self):
        self.logger.debug("[get_direct_torrentable] 즉시 재생 가능한 Public 토렌트 필터링 시작")
        direct_torrentable_items = []
        for torrent_item in self.__itemsDict.values():
            # [v1.1] 조건 검사 로그 상세화
            if torrent_item.privacy == "public" and torrent_item.file_index is not None:
                direct_torrentable_items.append(torrent_item)
                # 너무 많은 로그 방지를 위해 주석 처리하거나 필요시 해제
                # self.logger.debug(f"  -> 추가됨: {torrent_item.raw_title}")

        self.logger.debug(f"[get_direct_torrentable] 필터링 완료. 결과 개수: {len(direct_torrentable_items)}")

        # [v1.1] [CRITICAL FIX] 누락된 return 문 추가
        return direct_torrentable_items

    def cache_container_items(self):
        self.logger.debug("[cache_container_items] 캐시 저장 스레드 시작")
        threading.Thread(target=self.__save_to_cache).start()

    def __save_to_cache(self):
        try:
            public_torrents = list(filter(lambda x: x.privacy == "public", self.get_items()))
            self.logger.debug(f"[__save_to_cache] Public 토렌트 {len(public_torrents)}개 캐싱 시도")
            cache_results(public_torrents, self.__media)
            self.logger.debug("[__save_to_cache] 캐싱 완료")
        except Exception as e:
            self.logger.error(f"[__save_to_cache] 캐싱 중 오류 발생: {e}")

    def update_availability(self, debrid_response, debrid_type, media):
        self.logger.debug(f"[update_availability] Debrid 가용성 업데이트 시작. Type: {debrid_type}")
        try:
            if debrid_type is RealDebrid:
                self.__update_availability_realdebrid(debrid_response, media)
            elif debrid_type is AllDebrid:
                self.__update_availability_alldebrid(debrid_response, media)
            elif debrid_type is Premiumize:
                self.__update_availability_premiumize(debrid_response)
            elif debrid_type is TorBox:
                self.__update_availability_torbox(debrid_response, media)
            else:
                self.logger.error(f"[update_availability] 지원하지 않는 Debrid 타입: {debrid_type}")
                raise NotImplementedError
        except Exception as e:
            self.logger.error(f"[update_availability] 업데이트 중 예외 발생: {e}")

    def __update_availability_realdebrid(self, response, media):
        count = 0
        for info_hash, details in response.items():
            if "rd" not in details:
                continue

            if info_hash not in self.__itemsDict:
                continue

            torrent_item: TorrentItem = self.__itemsDict[info_hash]

            files = []
            # self.logger.info(torrent_item.type)
            if torrent_item.type == "series":
                for variants in details["rd"]:
                    for file_index, file in variants.items():
                        # self.logger.info(file["filename"])
                        clean_season = media.season.replace("S", "")
                        clean_episode = media.episode.replace("E", "")
                        numeric_season = int(clean_season)
                        numeric_episode = int(clean_episode)
                        if season_episode_in_filename(file["filename"], numeric_season, numeric_episode):
                            # self.logger.info("File details 2")
                            # self.logger.info(file["filename"])
                            files.append({
                                "file_index": file_index,
                                "title": file["filename"],
                                "size": file["filesize"]
                            })
            else:
                for variants in details["rd"]:
                    for file_index, file in variants.items():
                        # self.logger.info("File details 3")
                        # self.logger.info(file["filename"])
                        files.append({
                            "file_index": file_index,
                            "title": file["filename"],
                            "size": file["filesize"]
                        })

            if files:
                count += 1
                self.__update_file_details(torrent_item, files)
        self.logger.debug(f"[RealDebrid] {count}개 아이템 가용성 확인됨")

    def __update_availability_alldebrid(self, response, media):
        if response["status"] != "success":
            self.logger.error(f"Error while updating availability: {response}")
            return

        count = 0
        for data in response["data"]["magnets"]:
            if data["instant"] == False:
                continue

            if data["hash"] not in self.__itemsDict:
                continue

            torrent_item: TorrentItem = self.__itemsDict[data["hash"]]

            files = []
            self.__explore_folders(data["files"], files, 1, torrent_item.type, media.season,
                                   media.episode)

            if files:
                count += 1
                self.__update_file_details(torrent_item, files)
        self.logger.debug(f"[AllDebrid] {count}개 아이템 가용성 확인됨")

    def __update_availability_torbox(self, response, media):
        count = 0
        for torrent_hash, data in response.items():
            if not torrent_hash or torrent_hash not in self.__itemsDict:
                self.logger.warning(f"Hash {torrent_hash} not found in itemsDict.")
                continue
            torrent_item: TorrentItem = self.__itemsDict[torrent_hash]
            files = []

            self.__explore_folders(
                    folder=data.get("files", []),
                    files=files,
                    file_index=1,
                    type=torrent_item.type,
                    season=media.season,
                    episode=media.episode
            )
            if files:
                count += 1
                self.__update_file_details(torrent_item, files)
        self.logger.debug(f"[TorBox] {count}개 아이템 가용성 확인됨")

    def __update_availability_premiumize(self, response):
        if response["status"] != "success":
            self.logger.error(f"Error while updating availability: {response}")
            return

        torrent_items = self.get_items()
        count = 0
        for i in range(len(response["response"])):
            if i < len(torrent_items) and bool(response["response"][i]):
                is_cached = response["transcoded"][i] == True
                torrent_items[i].availability = is_cached
                if is_cached:
                    count += 1
        self.logger.debug(f"[Premiumize] {count}개 아이템 가용성 확인됨")

    def __update_file_details(self, torrent_item, files):
        if len(files) == 0:
            return

        file = max(files, key=lambda file: file["size"])
        torrent_item.availability = True
        torrent_item.file_index = file["file_index"]
        torrent_item.file_name = file["title"]
        torrent_item.size = file["size"]
        # self.logger.debug(f"  -> File Selected: {file['title']} (Size: {file['size']})")

    def __build_items_dict_by_infohash(self, items: List[TorrentItem]):
        self.logger.info(f"=== 중복 제거 프로세스 시작 (총 아이템 수: {len(items)}) ===")

        # 우선순위 테이블 (낮을수록 더 높은 우선순위)
        priority_order = {
            "torrentzota": 1,
            "bt4g": 3,
            "52bt": 4,
            "stremthru": 5,
        }

        def get_priority(indexer_name: str) -> int:
            if not indexer_name:
                return 999

            normalized = indexer_name.lower().strip()

            # btdigg1, btdigg2, btdigg10 ... 처럼
            # btdigg 뒤가 숫자로만 구성된 인덱서
            if (
                normalized.startswith("btdigg")
                and normalized[len("btdigg"):].isdigit()
            ):
                return 2

            return priority_order.get(normalized, 999)

        items_dict = dict()

        for item in items:
            # 1. 고유 키 식별 (로그용 타입 포함)
            if item.info_hash:
                unique_key = item.info_hash
                key_type = "HASH"
            elif item.link:
                unique_key = f"ext_{item.link}"
                key_type = "LINK"
            else:
                unique_key = f"tmp_{item.raw_title}"
                key_type = "TITLE"

            # 2. 현재 아이템 정보 추출 (속성 안전하게 가져오기)
            # TorrentItem 객체에 'indexer' 필드가 있는지 확인이 필요합니다.
            indexer = getattr(item, 'indexer', 'unknown')
            priority = get_priority(indexer)
            seeders = int(item.seeders or 0)

            # 너무 많은 로그는 디버그 레벨로 조정
            # self.logger.debug(f"[검사 중] 제목: {item.raw_title[:30]}... | 인덱서: {indexer}(순위:{priority}) | 시더: {seeders} | Key: {unique_key[:10]}...")

            # 3. 중복 처리 로직
            if unique_key not in items_dict:
                # 처음 보는 키라면 추가
                items_dict[unique_key] = item
                # self.logger.debug(f"  -> [신규] 딕셔너리에 추가됨.")
            else:
                # 중복 발생 시 비교 시작
                existing_item = items_dict[unique_key]
                existing_indexer = getattr(existing_item, 'indexer', 'unknown')
                existing_priority = get_priority(existing_indexer)
                existing_seeders = int(existing_item.seeders or 0)


                # 조건 A: 인덱서 우선순위가 더 높은가? (숫자가 더 낮은가?)
                if priority < existing_priority:
                    items_dict[unique_key] = item
                    self.logger.info(f"  -> [교체 완료] 인덱서 우선순위 높음: {indexer}({priority}) > {existing_indexer}({existing_priority})")

                # 조건 B: 우선순위가 같은데 시더가 더 많은가?
                elif priority == existing_priority:
                    if seeders > existing_seeders:
                        items_dict[unique_key] = item
                        self.logger.info(f"  -> [교체 완료] 동일 인덱서/순위 내 시더 우위: {seeders} > {existing_seeders}")
                    else:
                        pass

                # 조건 C: 기존 우선순위가 더 높음
                else:
                    pass

        self.logger.info(f"=== 중복 제거 완료 (남은 아이템: {len(items_dict)} / 전체: {len(items)}) ===")
        return items_dict


    # Simple recursion to traverse the file structure returned by AllDebrid
    def __explore_folders(self, folder, files, file_index, type, season=None, episode=None):
        if type == "series":
            for file in folder:
                if "e" in file or "files" in file:
                    sub_folder = file.get("e") or file.get("files")
                    file_index = self.__explore_folders(sub_folder, files, file_index, type, season,
                                                        episode)
                    continue

                file_name = file.get("n") or file.get("name")
                file_size = file.get("s") or file.get("size", 0)
                if not file_name:
                    self.logger.warning(f"Filename missing for : {file}")
                    continue

                if season_episode_in_filename(file_name, season, episode):
                    files.append({
                        "file_index": file_index,
                        "title": file_name,
                        "size": file_size
                    })
                file_index += 1

        elif type == "movie":
            file_index = 1
            for file in folder:
                if "e" in file or "files" in file:
                    sub_folder = file.get("e") or file.get("files")
                    file_index = self.__explore_folders(sub_folder, files, file_index, type)
                    continue

                file_name = file.get("n") or file.get("name")
                file_size = file.get("s") or file.get("size", 0)

                if not file_name:
                    self.logger.warning(f"Filename missing for : {file}")
                    continue

                files.append({
                    "file_index": file_index,
                    "title": file_name,
                    "size": file_size
                })
                file_index += 1

        return file_index
