import requests
from concurrent.futures import ThreadPoolExecutor
from metdata.metadata_provider_base import MetadataProvider
from models.media import Media
from models.movie import Movie
from models.series import Series
from utils.logger import setup_logger
import re
import time

class TMDB(MetadataProvider):
    def __init__(self, config):
        super().__init__(config)
        self._indexers = None

    @property
    def indexers(self):
        return self._indexers

    @indexers.setter
    def indexers(self, indexers_):
        self._indexers = indexers_

    def get_metadata(self, id, media_type):
        """
        메타데이터 조회 메인 진입점
        ID 접두사에 따라 적절한 처리 로직으로 분기합니다.
        """
        self.logger.info(f"[TMDB] 메타데이터 조회 요청: Type={media_type}, ID={id}")
        perf_metadata_start = time.perf_counter()

        full_id = id.split(":")
        self.logger.debug(f"[TMDB] 파싱된 ID 리스트: {full_id}")

        try:
            prefix = full_id[0]

            if prefix == "tvdb":
                full_id = self.__convert_tvdb_to_tmdb(full_id, media_type)

                if full_id: prefix = full_id[0]

            if prefix == "kitsu":
                full_id = self.__convert_kitsu_to_imdb(full_id, media_type)
                if not full_id:
                    return None
                id = ":".join(full_id)

            if full_id[0] != "tmdb" and not re.fullmatch(r"tt\d+", full_id[0]):
                self.logger.warning("Unsupported metadata ID: %s", id)
                return None

            result = self.__get_tmdb_metadata(id, full_id, media_type)

            self.logger.info(
                f"[PERF][TMDB] get_metadata TOTAL: "
                f"{time.perf_counter() - perf_metadata_start:.3f}s / "
                f"type={media_type} / id={id}"
            )
            return result

        except Exception as e:
            self.logger.error(f"[TMDB] 메타데이터 조회 중 치명적 오류: {str(e)}", exc_info=True)
            return None

    def __convert_tvdb_to_tmdb(self, full_id, media_type):
        """TVDB ID를 TMDB ID로 변환"""
        tvdb_id = full_id[1]
        self.logger.info(f"[ID변환] TVDB({tvdb_id}) -> TMDB 변환 시도")

        try:
            find_url = f"https://api.themoviedb.org/3/find/{tvdb_id}?api_key={self.config['tmdbApi']}&external_source=tvdb_id"
            response = requests.get(find_url)
            response.raise_for_status()
            find_data = response.json()

            tmdb_id = None
            if find_data.get("tv_results"):
                tmdb_id = find_data["tv_results"][0].get("id")
            elif find_data.get("movie_results"):
                tmdb_id = find_data["movie_results"][0].get("id")

            if not tmdb_id:
                raise ValueError(f"TVDB ID({tvdb_id})에 해당하는 TMDB ID를 찾지 못했습니다.")

            self.logger.info(f"[ID변환] 성공: TVDB({tvdb_id}) -> TMDB({tmdb_id})")

            new_full_id = ['tmdb', str(tmdb_id)]
            if len(full_id) > 2: new_full_id.append(full_id[2])
            if len(full_id) > 3: new_full_id.append(full_id[3])

            return new_full_id

        except Exception as e:
            self.logger.error(f"[ID변환] TVDB 변환 실패: {str(e)}")
            return full_id

    ANIME_MAPPING_HOSTS = (
        "aiostreams.elfhosted.com",
        "aiostreams.fortheweak.cloud",
        "aiostreamsfortheweebsstable.midnightignite.me",
        "aio.atbphosting.com",
        "aiostreams.stremio.ru",
        "aiostreams.viren070.me",
    )

    def __convert_kitsu_to_imdb(self, full_id, media_type):
        if len(full_id) < 2 or not full_id[1].isdigit() or int(full_id[1]) <= 0:
            return None
        if media_type == "movie":
            if len(full_id) != 2:
                return None
            episode = None
        elif media_type == "series":
            if len(full_id) != 3 or not full_id[2].isdigit() or int(full_id[2]) < 1:
                return None
            episode = full_id[2]
        else:
            return None

        kitsu_id = int(full_id[1])
        mapping_deadline = time.monotonic() + 12
        for host in self.ANIME_MAPPING_HOSTS:
            remaining = mapping_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = requests.get(
                    f"https://{host}/api/v1/anime",
                    params={"idType": "kitsuId", "idValue": kitsu_id},
                    timeout=(min(1.5, remaining), min(2.5, remaining)),
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("success") is not True:
                    continue
                data = payload.get("data") or {}
                ids = data.get("mappings") or {}
                if str(ids.get("kitsuId")) != str(kitsu_id):
                    continue
                imdb_id = ids.get("imdbId")
                if not isinstance(imdb_id, str) or not re.fullmatch(r"tt\d+", imdb_id):
                    continue
                if media_type == "movie":
                    self.logger.info("[ID변환] Kitsu(%s) -> IMDb(%s)", kitsu_id, imdb_id)
                    return [imdb_id]

                def valid_season(value):
                    return isinstance(value, int) and not isinstance(value, bool) and value > 0

                seasons = (
                    (data.get(source) or {}).get("seasonNumber")
                    for source in ("imdb", "tvdb", "trakt", "tmdb")
                )
                season = next((value for value in seasons if valid_season(value)), None)
                if season is None:
                    continue
                self.logger.info(
                    "[ID변환] Kitsu(%s) -> IMDb(%s:%s:%s) via %s",
                    kitsu_id, imdb_id, season, episode, host,
                )
                return [imdb_id, str(season), episode]
            except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
                self.logger.debug("[ID변환] Anime mapping host %s failed: %s", host, exc)

        self.logger.warning("[ID변환] Kitsu(%s) -> IMDb mapping unavailable", kitsu_id)
        return None

    def __get_tmdb_metadata(self, original_id, full_id, media_type):
        """TMDB/IMDB 표준 메타데이터 조회 (중국어 별칭 처리 포함)"""
        if self.config.get('getAllLanguages', None) and self._indexers and len(self._indexers) > 0:
            target_languages = list({indexer.language for indexer in self._indexers})
        else:
            target_languages = self.config['languages']

        self.logger.debug(f"[TMDB] 대상 언어: {target_languages}")
        chinese_char_pattern = re.compile(r'[一-鿿]')

        def fetch_tmdb_data(lang):
            perf_lang_start = time.perf_counter()

            session = requests.Session()

            try:

                if full_id[0] == "tmdb":
                    if media_type == "movie":
                        url = f"https://api.themoviedb.org/3/movie/{full_id[1]}?api_key={self.config['tmdbApi']}&language={lang}"
                    else:
                        url = f"https://api.themoviedb.org/3/tv/{full_id[1]}?api_key={self.config['tmdbApi']}&language={lang}"
                else:
                    url = f"https://api.themoviedb.org/3/find/{full_id[0]}?api_key={self.config['tmdbApi']}&external_source=imdb_id&language={lang}"

                perf_request_start = time.perf_counter()
                response = session.get(url)
                perf_request_end = time.perf_counter()

                self.logger.info(
                    f"[PERF][TMDB][{lang}] primary request: "
                    f"{perf_request_end - perf_request_start:.3f}s / "
                    f"status={response.status_code}"
                )

                data = response.json()

                if lang == 'zh' and isinstance(data, dict):
                    current_title = ""
                    tmdb_real_id = None

                    if media_type == "movie":
                        if full_id[0] == "tmdb":
                            current_title = data.get("title", "")
                            tmdb_real_id = data.get("id")
                        elif data.get("movie_results"):
                            current_title = data["movie_results"][0].get("title", "")
                            tmdb_real_id = data["movie_results"][0].get("id")
                    else:
                        if full_id[0] == "tmdb":
                            current_title = data.get("name", "")
                            tmdb_real_id = data.get("id")
                        elif data.get("tv_results"):
                            current_title = data["tv_results"][0].get("name", "")
                            tmdb_real_id = data["tv_results"][0].get("id")

                    if current_title and tmdb_real_id and not chinese_char_pattern.search(str(current_title)):
                        self.logger.debug(f"[ZH] 제목 '{current_title}'에 한자가 없어 별칭을 검색합니다.")
                        alias_url = ""
                        if media_type == "movie":
                            alias_url = f"https://api.themoviedb.org/3/movie/{tmdb_real_id}/alternative_titles?api_key={self.config['tmdbApi']}"
                        else:
                            alias_url = f"https://api.themoviedb.org/3/tv/{tmdb_real_id}/alternative_titles?api_key={self.config['tmdbApi']}"

                        try:
                            perf_alias_start = time.perf_counter()
                            alias_res = session.get(alias_url)
                            perf_alias_end = time.perf_counter()

                            self.logger.info(
                                f"[PERF][TMDB][zh] alternative_titles: "
                                f"{perf_alias_end - perf_alias_start:.3f}s / "
                                f"status={alias_res.status_code}"
                            )

                            if alias_res.status_code == 200:
                                alias_data = alias_res.json()
                                aliases = alias_data.get("titles", []) if media_type == "movie" else alias_data.get("results", [])

                                found_alias = None
                                for country in ['CN', 'TW', 'HK']:
                                    for item in aliases:
                                        if item.get('iso_3166_1') == country and item.get('title'):
                                            found_alias = item.get('title')
                                            break
                                    if found_alias: break

                                if found_alias:
                                    self.logger.info(f"[ZH] 별칭 발견: {current_title} -> {found_alias}")

                                    if full_id[0] == "tmdb":
                                        if media_type == "movie": data["title"] = found_alias
                                        else: data["name"] = found_alias
                                    else:
                                        if media_type == "movie": data["movie_results"][0]["title"] = found_alias
                                        else: data["tv_results"][0]["name"] = found_alias
                        except Exception as ex:
                            self.logger.warning(f"별칭 검색 중 오류: {str(ex)}")

                self.logger.info(
                    f"[PERF][TMDB][{lang}] worker TOTAL: "
                    f"{time.perf_counter() - perf_lang_start:.3f}s"
                )
                return (lang, data, session)

            except Exception as e:
                self.logger.info(
                    f"[PERF][TMDB][{lang}] worker TOTAL on error: "
                    f"{time.perf_counter() - perf_lang_start:.3f}s"
                )
                self.logger.error(f"Failed to fetch TMDB data for language {lang}: {str(e)}")
                return (lang, None, session)

        perf_pool_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=len(target_languages)) as executor:
            futures = [executor.submit(fetch_tmdb_data, lang) for lang in target_languages]
            tmdb_results = [future.result() for future in futures]

        perf_pool_end = time.perf_counter()

        self.logger.info(
            f"[PERF][TMDB] language pool WALL: "
            f"{perf_pool_end - perf_pool_start:.3f}s / "
            f"languages={target_languages}"
        )

        perf_post_start = time.perf_counter()

        title_lang_map = {}
        metadata_data = None

        reusable_tmdb_session = None

        for lang, data, worker_session in tmdb_results:
            if reusable_tmdb_session is None:
                reusable_tmdb_session = worker_session

            if not data or not isinstance(data, dict):
                continue

            raw_title = ""
            if media_type == "movie":
                if full_id[0] == "tmdb":
                    raw_title = data.get("title", "")
                else:
                    if data.get("movie_results"):
                        raw_title = data["movie_results"][0].get("title", "")
            else:
                if full_id[0] == "tmdb":
                    raw_title = data.get("name", "")
                else:
                    if data.get("tv_results"):
                        raw_title = data["tv_results"][0].get("name", "")

            if raw_title:
                clean_title = self.replace_weird_characters(raw_title)
                if clean_title not in title_lang_map:
                    title_lang_map[clean_title] = lang
                else:
                    if lang == 'en':
                        title_lang_map[clean_title] = lang

                if metadata_data is None or (lang == 'en'):
                    metadata_data = data

        if not title_lang_map or metadata_data is None:
            self.logger.warning(f"No valid metadata found for {original_id}")
            return None

        final_titles = list(title_lang_map.keys())
        final_languages = list(title_lang_map.values())
        data = metadata_data

        self.logger.info(
            f"[PERF][TMDB] language result processing: "
            f"{time.perf_counter() - perf_post_start:.3f}s / "
            f"titles={final_titles}"
        )

        if media_type == "movie":
            year = ""
            if full_id[0] == "tmdb":
                year = data.get("release_date", "")[:4]
            else:
                if data.get("movie_results"):
                    year = data["movie_results"][0].get("release_date", "")[:4]

            return Movie(id=original_id, titles=final_titles, year=year, languages=final_languages)
        else:
            is_korean = False
            tv_id = None
            year = ""

            if full_id[0] == "tmdb":
                tv_id = full_id[1]
                is_korean = "KR" in data.get("origin_country", [])
                year = data.get("first_air_date", "")[:4]
            else:
                if data.get("tv_results"):
                    tv_item = data["tv_results"][0]
                    tv_id = tv_item.get("id")
                    is_korean = "KR" in tv_item.get("origin_country", [])
                    year = tv_item.get("first_air_date", "")[:4]

            current_season = int(full_id[2]) if full_id[0] == "tmdb" else int(full_id[1])
            current_episode = int(full_id[3]) if full_id[0] == "tmdb" else int(full_id[2])

            total_episodes = None
            if is_korean and current_season >= 1 and tv_id:
                try:
                    series_info_url = f"https://api.themoviedb.org/3/tv/{tv_id}?api_key={self.config['tmdbApi']}"

                    perf_series_info_start = time.perf_counter()
                    if reusable_tmdb_session is not None:
                        series_info_response = reusable_tmdb_session.get(
                            series_info_url
                        )
                    else:
                        series_info_response = requests.get(
                            series_info_url
                        )

                    perf_series_info_end = time.perf_counter()

                    self.logger.info(
                        f"[PERF][TMDB] KR series info request: "
                        f"{perf_series_info_end - perf_series_info_start:.3f}s / "
                        f"status={series_info_response.status_code}"
                    )

                    series_info = series_info_response.json()

                    ep_sum = 0
                    for s in series_info.get("seasons", []):
                        s_num = s.get("season_number")
                        e_count = s.get("episode_count")
                        if isinstance(s_num, int) and isinstance(e_count, int):
                            if 0 < s_num < current_season:
                                ep_sum += e_count

                    total_episodes = ep_sum + current_episode
                    self.logger.debug(f"[TotalEpisodes] 누적 계산: {total_episodes} (KR={is_korean})")
                except Exception as e:
                    self.logger.error(f"에피소드 계산 실패: {str(e)}")
                    total_episodes = None

            return Series(
                id=original_id,
                titles=final_titles,
                year=year,
                season=f"S{current_season:02d}",
                episode=f"E{current_episode:02d}",
                languages=final_languages,
                total_episodes=total_episodes
            )
