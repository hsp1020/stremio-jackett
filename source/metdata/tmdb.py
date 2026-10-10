# [v1.1] TMDB 메타데이터 처리 로직 - 정규표현식 백슬래시 누락 버그 픽스
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
        
        # 1. ID 파싱
        full_id = id.split(":")
        self.logger.debug(f"[TMDB] 파싱된 ID 리스트: {full_id}")
        
        try:
            prefix = full_id[0]

            # 2. ID 변환 및 처리 로직 분기
            if prefix == "tvdb":
                full_id = self.__convert_tvdb_to_tmdb(full_id, media_type)
                # 변환된 ID로 계속 진행하기 위해 prefix 갱신
                if full_id: prefix = full_id[0]

            if prefix == "trakt":
                full_id = self.__convert_trakt_to_tmdb(full_id, media_type)
                if full_id: prefix = full_id[0]

            # 3. 실제 메타데이터 조회
            if prefix == "kitsu":
                result = self.__get_kitsu_metadata(id, full_id, media_type)
            else:
                # tmdb, imdb 등 표준 처리
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
            
            # 기존 ID 구조 유지하며 교체
            new_full_id = ['tmdb', str(tmdb_id)]
            if len(full_id) > 2: new_full_id.append(full_id[2])
            if len(full_id) > 3: new_full_id.append(full_id[3])
            
            return new_full_id
            
        except Exception as e:
            self.logger.error(f"[ID변환] TVDB 변환 실패: {str(e)}")
            return full_id  # 실패 시 원본 반환 (이후 로직에서 처리)

    def __convert_trakt_to_tmdb(self, full_id, media_type):
        """Trakt ID를 TMDB ID로 변환"""
        trakt_id = full_id[1]
        self.logger.info(f"[ID변환] Trakt({trakt_id}) -> TMDB 변환 시도")
        
        try:
            headers = {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": "4d42daf7ebfb145686602a1a8158f7c0d771e1157513e2a8e3dd8b14f55a23aa"
            }
            if media_type == "movie":
                trakt_url = f"https://api.trakt.tv/movies/{trakt_id}"
            else:
                trakt_url = f"https://api.trakt.tv/shows/{trakt_id}"
            
            trakt_response = requests.get(trakt_url, headers=headers)
            trakt_data = trakt_response.json()
            
            tmdb_id = trakt_data.get('ids', {}).get('tmdb')
            if not tmdb_id:
                raise ValueError(f"Trakt 응답에서 TMDB ID를 찾을 수 없습니다. (ID: {trakt_id})")
            
            self.logger.info(f"[ID변환] 성공: Trakt({trakt_id}) -> TMDB({tmdb_id})")
            
            # 기존 ID 구조 유지하며 교체
            new_full_id = ["tmdb", str(tmdb_id)]
            if len(full_id) >= 4:
                new_full_id.append(full_id[3]) # Season
                if len(full_id) >= 5:
                    new_full_id.append(full_id[4]) # Episode
            
            return new_full_id
            
        except Exception as e:
            self.logger.error(f"[ID변환] Trakt 변환 실패: {str(e)}", exc_info=True)
            raise  # Trakt는 실패 시 진행 불가하므로 예외 전파

    def __get_kitsu_metadata(self, original_id, full_id, media_type):
        """Kitsu 애니메이션 메타데이터 처리"""
        self.logger.info(f"[Kitsu] 메타데이터 처리 시작: {original_id}")
        kitsu_total_start = time.perf_counter()
        try:
            kitsu_id = full_id[1]
            kitsu_url = f"https://kitsu.io/api/edge/anime/{kitsu_id}"
            kitsu_api_start = time.perf_counter()
            kitsu_response = requests.get(kitsu_url)
            kitsu_data = kitsu_response.json()

            self.logger.info(
                "[PERF][KITSU] API: %.3fs / status=%s / id=%s",
                time.perf_counter() - kitsu_api_start,
                getattr(kitsu_response, "status_code", None),
                kitsu_id,
            )

            kitsu_alias_start = time.perf_counter()
            attributes = kitsu_data['data']['attributes']
            titles = attributes.get('titles', {})
            canonical_title = attributes.get('canonicalTitle')
            slug = attributes.get('slug', '')
            abbreviated_titles = attributes.get('abbreviatedTitles', [])
            start_date = attributes.get('startDate')
            year = start_date[:4] if start_date else None
            languages = ['en']

            # [v1.1] 시즌 탐지 패턴 수정 (s -> \s, d -> \d 로 정상 복구)
            season_matches_patterns_list = [
                (r'\b(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth)\s+(Season|시즌)\b',
                    lambda m: {'First': 1, 'Second': 2, 'Third': 3, 'Fourth': 4, 'Fifth': 5,
                            'Sixth': 6, 'Seventh': 7, 'Eighth': 8, 'Ninth': 9}[m.group(1)]),
                (r'\b(\d+)(?:st|nd|rd|th)\s*(?:Season|시즌)\b', 1),
                (r'[Ss]eason\s*(\d+)', 1),
                (r'[Ss]\s*(\d+)', 1),
                (r'[([]\s*[sS](\d+)\s*[)]]', 1),
                (r'第\s*(\d+)\s*期', 1),
            ]

            # [v1.1] 스킵 패턴 수정 (s -> \s 복구)
            skip_patterns_list = [
                r'\s*(?:The\s+)?Final\s+Season\b',
                r'\s*(?:The\s+)?Last\s+Season\b',
                r'\s*最終章',
                r'\s*完結篇',
            ]

            def detect_season_from_title(title: str) -> int | None:
                if not title: return None
                try:
                    # [v1.1] s+ -> \s+ 로 수정
                    normalized = re.sub(r'\s+', ' ', title.replace('-', ' ')).strip()
                    if any(re.search(pat, normalized, re.IGNORECASE) for pat in skip_patterns_list):
                        return None
                    for pattern, group in season_matches_patterns_list:
                        match = re.search(pattern, normalized, re.IGNORECASE)
                        if match:
                            return int(match.group(group)) if isinstance(group, int) else group(match)
                    return None
                except: return None

            season_number = 1
            season_detected_titles = []
            titles_to_scan = [titles.get('en'), titles.get('en_us'), canonical_title]
            if abbreviated_titles: titles_to_scan.extend(abbreviated_titles)
            titles_to_scan.append(slug.replace('-', ' ') if slug else None)

            for title in titles_to_scan:
                if not title: continue
                season_candidate = detect_season_from_title(title)
                if season_candidate is not None:
                    if not season_detected_titles:
                        season_number = season_candidate
                    season_detected_titles.append(title)

            def process_title(title_to_process, local_season_patterns, local_skip_patterns):
                if not title_to_process: return None
                try:
                    normalized_title = title_to_process.replace('-', ' ').replace('.', ' ')
                    # [v1.1] 정규식 수정
                    normalized_for_search = re.sub(r'\s+', ' ', normalized_title).strip()
                    
                    for pat in [r'\s*OVA$', r'\s*ONA$', r'\s*OAV$']:
                        normalized_for_search = re.sub(pat, '', normalized_for_search, flags=re.IGNORECASE)

                    for skip_pat in local_skip_patterns:
                        match = re.search(skip_pat, normalized_for_search, re.IGNORECASE)
                        if match:
                            return re.sub(r'\s{2,}', ' ', normalized_for_search[:match.start()]).strip()

                    for pattern, group_info in local_season_patterns:
                        match = re.search(pattern, normalized_for_search, re.IGNORECASE)
                        if match:
                            return re.sub(r'\s{2,}', ' ', normalized_for_search[:match.start()]).strip()

                    return re.sub(r'\s{2,}', ' ', re.sub(r'\s+The$', '', normalized_for_search, flags=re.IGNORECASE)).strip()
                except: return None

            seen_titles = set()
            collected_titles = []

            def add_title(title):
                if not title: return
                cleaned = process_title(title, season_matches_patterns_list, skip_patterns_list)
                if not cleaned: return
                final_title = self.replace_weird_characters(cleaned)
                # [v1.1] 정규식 수정, s+ 에서 \s+ 로 변경 및 디버깅 로그 추가
                compare_key = re.sub(r'\s+', ' ', final_title.lower()).strip()
                self.logger.debug(f"[v1.1 Kitsu] 원본: '{title}', 처리후: '{final_title}'")
                
                if compare_key not in seen_titles:
                    seen_titles.add(compare_key)
                    collected_titles.append(final_title)

            add_title(titles.get('en_us') or titles.get('en'))
            add_title(canonical_title)
            if abbreviated_titles: add_title(abbreviated_titles[0])
            add_title(slug.replace('-', ' ') if slug else None)
            for st in season_detected_titles: add_title(st)

            self.logger.info(
                "[PERF][KITSU] aliases: count=%s / build=%.3fs / titles=%s",
                len(collected_titles),
                time.perf_counter() - kitsu_alias_start,
                collected_titles,
            )

            self.logger.info(
                "[PERF][KITSU] metadata TOTAL: %.3fs / id=%s",
                time.perf_counter() - kitsu_total_start,
                kitsu_id,
            )

            if media_type == "movie":
                return Movie(id=original_id, titles=collected_titles, year=year, languages=languages)
            else:
                episode = f"E{int(full_id[2]):02d}" if len(full_id) > 2 else "E01"
                return Series(id=original_id, titles=collected_titles, year=year, season=f"S{season_number:02d}", episode=episode, languages=languages)
            
        except Exception as e:
            self.logger.error(f"[Kitsu] 처리 실패: {str(e)}", exc_info=True)
            raise

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

            # perf_session_reuse:
            # 동일 worker의 연속 TMDB 요청에서 HTTP keep-alive 재사용
            session = requests.Session()

            try:
                # URL 생성
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
                
                # [중국어(zh) 처리] 별칭 검색 로직
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
                    
                    # 제목이 있고, ID가 있고, 한자가 없는 경우
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
                                    # 데이터 덮어쓰기
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
