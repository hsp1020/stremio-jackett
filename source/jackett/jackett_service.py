import os
import queue
import threading
import time
import re
import xml.etree.ElementTree as ET
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from RTN import parse
from jackett.jackett_indexer import JackettIndexer
from jackett.jackett_result import JackettResult
from models.movie import Movie
from models.series import Series
from utils.detection import detect_languages
from utils.logger import setup_logger

class _LazyParsedData:
    """Compatibility proxy that defers RTN parsing until parsed metadata is accessed."""

    _is_lazy_rtn_proxy = True

    def __init__(self, raw_title, cache, lock):
        object.__setattr__(self, '_raw_title', raw_title)
        object.__setattr__(self, '_cache', cache)
        object.__setattr__(self, '_lock', lock)
        object.__setattr__(self, '_parsed', None)

    def materialize(self):
        parsed = object.__getattribute__(self, '_parsed')
        if parsed is not None:
            return parsed

        raw_title = object.__getattribute__(self, '_raw_title')
        cache = object.__getattribute__(self, '_cache')
        lock = object.__getattribute__(self, '_lock')

        with lock:
            parsed = cache.get(raw_title)
            if parsed is None:
                parsed = parse(raw_title)
                cache[raw_title] = parsed

            object.__setattr__(self, '_parsed', parsed)

        return parsed

    def __getattr__(self, name):
        return getattr(self.materialize(), name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        else:
            setattr(self.materialize(), name, value)

class JackettService:

    SEARCH_TIMEOUT = 15.0
    HTTP_TIMEOUT = 15
    INDEXER_CACHE_TTL = 60
    _indexer_cache = {}
    _indexer_cache_lock = threading.Lock()
    MOVIE_NO_YEAR_MERGE_THRESHOLD = 25
    MOVIE_NO_YEAR_RESULT_TAG = 'movie_no_year'

    LANGUAGE_ENGLISH = 'en'
    LANGUAGE_KOREAN = 'ko'
    LANGUAGE_CHINESE = 'zh'
    PRESERVE_FULL_INDEXER_LOCALES = frozenset({'pt'})

    DISABLE_IMDB_SEARCH_ENV = 'DISABLE_JACKETT_IMDB_SEARCH'
    IMDB_DISABLED_VALUE = 'true'
    IMDB_ID_PARAM = 'imdbid'
    TORZNAB_MOVIE_SEARCH_TYPE = 'movie'
    TORZNAB_MOVIE_CATEGORY = '2000'
    TORZNAB_SERIES_SEARCH_TYPE = 'tvsearch'
    TORZNAB_SERIES_CATEGORY = '5000'

    def __init__(self, config):
        self.logger = setup_logger(__name__)
        self._indexers = None
        self.__api_key = config['jackettApiKey']
        self.__base_url = f"{config['jackettHost']}/api/v2.0"
        self.__session = requests.Session()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "search_http_requests": 0,
            "search_http_errors": 0,
            "search_http_non_2xx": 0,
            "search_http_seconds": 0.0,
            "search_http_max_seconds": 0.0,
            "xml_documents": 0,
            "xml_items": 0,
            "xml_bytes": 0,
            "xml_seconds": 0.0,
            "xml_max_seconds": 0.0,
        }

    def _search_torznab_request(self, url, params):
        started = time.perf_counter()
        with self._metrics_lock:
            self._metrics["search_http_requests"] += 1
        try:
            response = self.__session.get(
                url, params=params, timeout=self.HTTP_TIMEOUT,
            )
            if getattr(response, 'status_code', 200) >= 400:
                with self._metrics_lock:
                    self._metrics["search_http_non_2xx"] += 1
            return response
        except requests.RequestException:
            with self._metrics_lock:
                self._metrics["search_http_errors"] += 1
            raise
        finally:
            elapsed = time.perf_counter() - started
            with self._metrics_lock:
                self._metrics["search_http_seconds"] += elapsed
                self._metrics["search_http_max_seconds"] = max(
                    self._metrics["search_http_max_seconds"], elapsed
                )

    def _get_indexer_policy(self, indexer):
        return {
            'base_language': str(indexer.language).lower().split('-')[0].strip(),
            'search_all_languages': str(indexer.id).lower().strip().startswith('btdigg'),
        }

    def _get_unique_key(self, result: JackettResult):
        if result.info_hash:
            return result.info_hash.lower()
        if result.magnet:
            match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', result.magnet)
            if match:
                return match.group(1).lower()
        if result.link:
            return result.link
        return result.raw_title

    def _deduplicate_search_tasks(self, tasks):
        deduplicated_tasks = []
        seen_params = set()

        for task in tasks:
            params, name = task[0], task[1]
            params_key = tuple(
                sorted((key, str(value)) for key, value in params.items())
            )
            if params_key in seen_params:
                self.logger.debug(
                    f"[SearchTask] Skipping duplicate task: {name}"
                )
                continue

            seen_params.add(params_key)
            deduplicated_tasks.append(task)

        return deduplicated_tasks

    def search(
        self,
        media,
        prewarm_callback=None,
        prewarm_threshold=2000,
    ):
        self.logger.info("Started Jackett search for " + media.type + " " + media.titles[0])
        indexers = self.get_indexers()
        threads = []

        results_queue = queue.Queue()
        found_results_count = [0]
        count_lock = threading.Lock()

        unique_seen_keys = set()
        seen_lock = threading.Lock()

        prewarm_triggered = False

        def thread_target(media, indexer):
            start_time = time.time()
            try:
                self.logger.info(f"Searching on {indexer.title} ({indexer.id})")
                if isinstance(media, Movie):
                    self.__search_movie_indexer(
                        media, indexer, results_queue, found_results_count,
                        count_lock, unique_seen_keys, seen_lock,
                    )
                elif isinstance(media, Series):
                    self.__search_series_indexer(
                        media, indexer, results_queue, found_results_count,
                        count_lock, unique_seen_keys, seen_lock,
                    )
                else:
                    raise TypeError("Only Movie and Series allowed!")
                self.logger.info(
                    f"Search logic finished on {indexer.title} "
                    f"({time.time() - start_time:.2f}s)"
                )
            except Exception as e:
                self.logger.error(f"Error search thread {indexer.title}: {e}")

        for indexer in indexers:
            thread = threading.Thread(target=thread_target, args=(media, indexer))
            thread.daemon = True
            threads.append(thread)
            thread.start()

        start_wait = time.time()

        while True:
            elapsed = time.time() - start_wait

            if (
                prewarm_callback is not None
                and not prewarm_triggered
            ):
                with count_lock:
                    current_found = (
                        found_results_count[0]
                    )

                if (
                    current_found
                    >= prewarm_threshold
                ):
                    prewarm_triggered = True

                    self.logger.info(
                        "[PERF][RTN-PREWARM] "
                        "trigger / found=%s / "
                        "smartwait_elapsed=%.3fs",
                        current_found,
                        elapsed,
                    )

                    try:
                        prewarm_callback(
                            current_found
                        )
                    except Exception as exc:
                        self.logger.warning(
                            "RTN prewarm failed: %s",
                            exc,
                        )

            if all(not thread.is_alive() for thread in threads):
                self.logger.debug(f"[SmartWait] All threads finished in {elapsed:.2f}s")
                break

            if elapsed >= self.SEARCH_TIMEOUT:
                self.logger.info(
                    f"[SmartWait] Timeout reached ({elapsed:.2f}s). "
                    "Returning collected results."
                )
                break

            time.sleep(0.1)

        perf_collect_start = time.perf_counter()
        self.logger.info("[PERF] SmartWait finished / result collection starting")

        year_results = []
        no_year_results = []
        while not results_queue.empty():
            queued_item = results_queue.get()
            if (
                isinstance(queued_item, tuple)
                and len(queued_item) == 2
                and queued_item[0] == self.MOVIE_NO_YEAR_RESULT_TAG
            ):
                no_year_results.extend(queued_item[1])
            else:
                year_results.extend(queued_item)

        perf_queue_done = time.perf_counter()
        self.logger.info(
            f"[PERF] Queue drain: "
            f"{perf_queue_done - perf_collect_start:.3f}s / "
            f"year={len(year_results)} / no_year={len(no_year_results)}"
        )

        perf_merge_start = time.perf_counter()

        if isinstance(media, Movie):
            year_unique_keys = {
                self._get_unique_key(result)
                for result in year_results
            }
            if len(year_unique_keys) < self.MOVIE_NO_YEAR_MERGE_THRESHOLD:
                self.logger.info(
                    f"[MovieFallback] Merging no-year results: "
                    f"{len(year_unique_keys)} unique year results < "
                    f"{self.MOVIE_NO_YEAR_MERGE_THRESHOLD}; "
                    f"no-year results={len(no_year_results)}"
                )
                results = year_results + no_year_results
            else:
                self.logger.info(
                    f"[MovieFallback] Discarding no-year results: "
                    f"{len(year_unique_keys)} unique year results >= "
                    f"{self.MOVIE_NO_YEAR_MERGE_THRESHOLD}; "
                    f"discarded={len(no_year_results)}"
                )
                results = year_results
        else:
            results = year_results

        perf_merge_done = time.perf_counter()
        self.logger.info(
            f"[PERF] Result merge/select: "
            f"{perf_merge_done - perf_merge_start:.3f}s / "
            f"results={len(results)}"
        )

        perf_post_start = time.perf_counter()
        processed = self.__post_process_results(results, media)
        perf_post_done = time.perf_counter()

        self.logger.info(
            f"[PERF] Post process: "
            f"{perf_post_done - perf_post_start:.3f}s / "
            f"results={len(processed)}"
        )

        self.logger.info(
            f"[PERF] After SmartWait total: "
            f"{perf_post_done - perf_collect_start:.3f}s"
        )

        # Snapshot at SmartWait return: slow background indexer threads may
        # still run, so these are counts observed by response assembly time.
        with self._metrics_lock:
            metrics = dict(self._metrics)
        self.logger.info(
            "[PERF][JackettHTTP] at_return_requests=%s / errors=%s / "
            "non_2xx=%s / summed_http_s=%.3f / max_http_s=%.3f / "
            "xml_documents=%s / xml_items=%s / xml_bytes=%s / "
            "xml_parse_s=%.3f / xml_max_s=%.3f",
            metrics["search_http_requests"], metrics["search_http_errors"],
            metrics["search_http_non_2xx"], metrics["search_http_seconds"],
            metrics["search_http_max_seconds"], metrics["xml_documents"],
            metrics["xml_items"], metrics["xml_bytes"],
            metrics["xml_seconds"], metrics["xml_max_seconds"],
        )

        return processed

    def __execute_search_tasks(
        self,
        indexer,
        tasks,
        search_func,
        results_queue,
        counter,
        count_lock,
        unique_seen_keys,
        seen_lock,
    ):
        tasks = self._deduplicate_search_tasks(tasks)
        if not tasks:
            return

        def process_and_count(new_results, result_tag=None):
            if not new_results:
                return

            if result_tag is None:
                results_queue.put(new_results)
            else:
                results_queue.put((result_tag, new_results))

            unique_increment = 0
            with seen_lock:
                for item in new_results:
                    key = self._get_unique_key(item)
                    if key not in unique_seen_keys:
                        unique_seen_keys.add(key)
                        unique_increment += 1

            if unique_increment > 0:
                with count_lock:
                    counter[0] += unique_increment

        if tasks:
            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                futures = {}
                for task in tasks:
                    params, name = task[0], task[1]
                    result_tag = task[2] if len(task) > 2 else None
                    future = executor.submit(search_func, params, name)
                    futures[future] = (
                        result_tag,
                        bool(params.get(self.IMDB_ID_PARAM)),
                    )

                for future in as_completed(futures):
                    try:
                        data = future.result()
                        result_tag, is_imdb_id_search = futures[future]
                        if is_imdb_id_search and data:
                            for item in data:
                                item.imdb_id_search = True
                        process_and_count(data, result_tag)
                    except Exception as e:
                        self.logger.error(f"Error in parallel search task: {e}")

    def __search_movie_indexer(
        self,
        movie,
        indexer,
        results_queue,
        counter,
        count_lock,
        unique_seen_keys,
        seen_lock,
    ):
        policy = self._get_indexer_policy(indexer)
        idx_lang = policy['base_language']
        mov_id = str(movie.id).lower().strip()
        has_imdb_search_capability = (
            os.getenv(self.DISABLE_IMDB_SEARCH_ENV) != self.IMDB_DISABLED_VALUE
            and re.fullmatch(r'tt\d+', mov_id) is not None
            and self.IMDB_ID_PARAM in (indexer.movie_search_capatabilities or [])
        )
        movie_caps = set(indexer.movie_search_capatabilities or [])
        movie_search_supported = indexer.movie_search_capatabilities is not None
        supports_query = 'q' in movie_caps
        if movie_search_supported and (supports_query or has_imdb_search_capability):
            search_mode = self.TORZNAB_MOVIE_SEARCH_TYPE
        elif indexer.search_available:
            search_mode = 'search'
            has_imdb_search_capability = False
        else:
            self.logger.info("Indexer %s has no supported movie or general search", indexer.title)
            return

        english_index = next((
            i for i, lang in enumerate(movie.languages)
            if str(lang).lower().split('-')[0] == 'en'
            and i < len(movie.titles) and movie.titles[i]
        ), None)
        imdb_only = has_imdb_search_capability and (english_index is None or not supports_query)

        if imdb_only:
            languages, titles = [], []
        elif policy['search_all_languages']:
            languages, titles = movie.languages, movie.titles
        elif has_imdb_search_capability:
            languages = [movie.languages[english_index]]
            titles = [movie.titles[english_index]]
        elif idx_lang == self.LANGUAGE_KOREAN:
            indices = [
                i for i, title in enumerate(movie.titles)
                if movie.languages[i] == self.LANGUAGE_KOREAN
                or self.LANGUAGE_KOREAN in detect_languages(title)
            ]
            if not indices:
                indices = [0]
            languages = [self.LANGUAGE_KOREAN for _ in indices]
            titles = [movie.titles[i] for i in indices]
        else:
            indices = [
                i for i, lang in enumerate(movie.languages)
                if lang == indexer.language or lang == self.LANGUAGE_ENGLISH
            ]
            if not indices:
                indices = [0]
            languages = [movie.languages[i] for i in indices]
            titles = [movie.titles[i] for i in indices]

        titles = [re.sub(r'\s+', ' ', title).strip() for title in titles]

        def search_with_params(params_dict, search_type):
            try:
                url = f"{self.__base_url}/indexers/{indexer.id}/results/torznab/api"
                response = self._search_torznab_request(url, params_dict)
                self.logger.debug(f"[Jackett][Movie][{search_type}] URL: {response.request.url}")
                response.raise_for_status()
                return self.__get_torrent_links_from_xml(response.text)
            except Exception as e:
                self.logger.error(f"Movie search [{search_type}] failed for {indexer.title}: {e}")
                return []

        if imdb_only:
            params = {
                'apikey': self.__api_key,
                't': search_mode,
                'cat': self.TORZNAB_MOVIE_CATEGORY,
                self.IMDB_ID_PARAM: mov_id,
            }
            self.__execute_search_tasks(
                indexer, [(params, "IMDbOnly")], search_with_params,
                results_queue, counter, count_lock, unique_seen_keys, seen_lock,
            )
            return

        phase1_tasks = []
        for index, lang in enumerate(languages):
            query_value = titles[index]
            params = {
                'apikey': self.__api_key,
                't': search_mode,
                'cat': self.TORZNAB_MOVIE_CATEGORY,
                'q': query_value,
            }
            if search_mode == self.TORZNAB_MOVIE_SEARCH_TYPE and 'year' in movie_caps and movie.year:
                params['year'] = movie.year
            if has_imdb_search_capability:
                params[self.IMDB_ID_PARAM] = mov_id
            phase1_tasks.append((params, f"WithYear-{lang}"))

        no_year_tasks = []
        for index, lang in enumerate(languages):
            is_korean_search = (
                lang == self.LANGUAGE_KOREAN
                or self.LANGUAGE_KOREAN in detect_languages(titles[index])
            )
            if is_korean_search and search_mode == self.TORZNAB_MOVIE_SEARCH_TYPE and 'year' in movie_caps and movie.year:
                query_value = titles[index]
                params_no_year = {
                    'apikey': self.__api_key,
                    't': search_mode,
                    'cat': self.TORZNAB_MOVIE_CATEGORY,
                    'q': query_value,
                }
                if has_imdb_search_capability:
                    params_no_year[self.IMDB_ID_PARAM] = mov_id

                no_year_tasks.append(
                    (
                        params_no_year,
                        f"WithoutYear-{lang}",
                        self.MOVIE_NO_YEAR_RESULT_TAG,
                    )
                )

        if no_year_tasks:
            self.logger.info(
                f"[MovieSearchPlan] Starting year and no-year searches in parallel "
                f"({len(phase1_tasks)} year, {len(no_year_tasks)} no-year)"
            )

        self.__execute_search_tasks(
            indexer,
            phase1_tasks + no_year_tasks,
            search_with_params,
            results_queue,
            counter,
            count_lock,
            unique_seen_keys,
            seen_lock,
        )

    def __search_series_indexer(
        self,
        series,
        indexer,
        results_queue,
        counter,
        count_lock,
        unique_seen_keys,
        seen_lock,
    ):
        policy = self._get_indexer_policy(indexer)
        idx_lang = policy['base_language']
        ser_id = str(series.id).lower().strip()
        has_imdb_search_capability = (
            os.getenv(self.DISABLE_IMDB_SEARCH_ENV) != self.IMDB_DISABLED_VALUE
            and re.fullmatch(r'tt\d+', ser_id.split(':', 1)[0]) is not None
            and self.IMDB_ID_PARAM in (indexer.tv_search_capatabilities or [])
        )
        tv_caps = set(indexer.tv_search_capatabilities or [])
        tv_search_supported = indexer.tv_search_capatabilities is not None
        supports_query = 'q' in tv_caps
        if tv_search_supported and (supports_query or has_imdb_search_capability):
            search_mode = self.TORZNAB_SERIES_SEARCH_TYPE
        elif indexer.search_available:
            search_mode = 'search'
            has_imdb_search_capability = False
        else:
            self.logger.info("Indexer %s has no supported TV or general search", indexer.title)
            return

        english_index = next((
            i for i, lang in enumerate(series.languages)
            if str(lang).lower().split('-')[0] == 'en'
            and i < len(series.titles) and series.titles[i]
        ), None)
        imdb_only = has_imdb_search_capability and (english_index is None or not supports_query)

        season = f"{int(series.season.replace('S', '')):02d}"
        episode = f"{int(series.episode.replace('E', '')):02d}"

        if imdb_only:
            languages, titles = [], []
        elif policy['search_all_languages']:
            languages, titles = series.languages, series.titles
        elif has_imdb_search_capability:
            languages = [series.languages[english_index]]
            titles = [series.titles[english_index]]
        elif idx_lang == self.LANGUAGE_KOREAN:
            indices = [
                i for i, title in enumerate(series.titles)
                if series.languages[i] == self.LANGUAGE_KOREAN
                or self.LANGUAGE_KOREAN in detect_languages(title)
            ]
            if not indices:
                indices = [0]
            languages = [self.LANGUAGE_KOREAN for _ in indices]
            titles = [series.titles[i] for i in indices]
        else:
            indices = [
                i for i, lang in enumerate(series.languages)
                if lang == indexer.language or lang == self.LANGUAGE_ENGLISH
            ]
            if not indices:
                indices = [0]
            languages = [series.languages[i] for i in indices]
            titles = [series.titles[i] for i in indices]

        titles = [re.sub(r'\s+', ' ', title).strip() for title in titles]

        def search_pattern(params_dict, pattern_name):
            try:
                url = f"{self.__base_url}/indexers/{indexer.id}/results/torznab/api"
                response = self._search_torznab_request(url, params_dict)
                self.logger.debug(f"[Jackett][Series][{pattern_name}] URL: {response.request.url}")
                response.raise_for_status()
                return self.__get_torrent_links_from_xml(response.text)
            except Exception as e:
                self.logger.error(f"Series search [{pattern_name}] failed for {indexer.title}: {e}")
                return []

        def create_params(base_params, title, suffix):
            params = base_params.copy()
            params['q'] = f'{title} {suffix}'.strip()
            return params

        tv_search_capabilities = set(
            indexer.tv_search_capatabilities or []
        ) if search_mode == self.TORZNAB_SERIES_SEARCH_TYPE else set()

        supports_structured_episode_search = {
            'season',
            'ep',
        }.issubset(tv_search_capabilities)

        supports_structured_season_search = (
            'season' in tv_search_capabilities
        )

        def create_episode_params(base_params, title, suffix):
            if supports_structured_episode_search:
                params = create_params(
                    base_params,
                    title,
                    '',
                )
                params['season'] = int(season)
                params['ep'] = int(episode)
                return params

            return create_params(
                base_params,
                title,
                suffix,
            )

        def create_season_params(base_params, title, suffix):
            if supports_structured_season_search:
                params = create_params(
                    base_params,
                    title,
                    '',
                )
                params['season'] = int(season)
                return params

            return create_params(
                base_params,
                title,
                suffix,
            )

        if imdb_only:
            params = {
                'apikey': self.__api_key,
                't': search_mode,
                'cat': self.TORZNAB_SERIES_CATEGORY,
                self.IMDB_ID_PARAM: ser_id.split(':', 1)[0],
            }
            if supports_structured_episode_search:
                params['season'] = int(season)
                params['ep'] = int(episode)
            self.__execute_search_tasks(
                indexer, [(params, "IMDbOnly")], search_pattern,
                results_queue, counter, count_lock, unique_seen_keys, seen_lock,
            )
            return

        all_search_tasks = []
        for index, lang in enumerate(languages):
            base_params = {
                'apikey': self.__api_key,
                't': search_mode,
                'cat': self.TORZNAB_SERIES_CATEGORY,
            }
            if has_imdb_search_capability:
                base_params[self.IMDB_ID_PARAM] = ser_id.split(':', 1)[0]

            params_title_only = create_params(base_params, titles[index], '')

            if lang == self.LANGUAGE_KOREAN:
                if int(season) >= 2:
                    params_episode = create_episode_params(
                        base_params,
                        titles[index],
                        f"{int(season)} e{episode}",
                    )
                    all_search_tasks.append((params_episode, f"Episode-{lang}"))

                    params_episode_no_season = create_params(
                        base_params,
                        titles[index],
                        f"e{episode}",
                    )
                    all_search_tasks.append(
                        (params_episode_no_season, f"Episode-NoSeason-{lang}")
                    )
                else:
                    params_episode = create_episode_params(
                        base_params,
                        titles[index],
                        f"e{episode}",
                    )
                    all_search_tasks.append((params_episode, f"Episode-{lang}"))
            elif lang == self.LANGUAGE_CHINESE:
                params_episode = create_episode_params(
                    base_params,
                    titles[index],
                    f"S{season} 第{episode}",
                )
                all_search_tasks.append((params_episode, f"Episode-{lang}"))
            else:
                params_episode = create_episode_params(
                    base_params,
                    titles[index],
                    f"S{season}E{episode}",
                )
                all_search_tasks.append((params_episode, f"Episode-{lang}"))

            if (
                int(season) >= 2
                and hasattr(series, 'total_episodes')
                and series.total_episodes
            ):
                if lang == self.LANGUAGE_KOREAN:
                    total_suffix = f"e{int(series.total_episodes):02d}"
                else:
                    total_suffix = f"E{int(series.total_episodes):02d}"
                params_total = create_params(
                    base_params,
                    titles[index],
                    total_suffix,
                )
                all_search_tasks.append((params_total, f"TotalEpisodes-{lang}"))

            if int(season) >= 2 or lang != self.LANGUAGE_KOREAN:
                season_suffix = f"{int(season)}" if lang == self.LANGUAGE_KOREAN else f"S{season}"
                params_season = create_season_params(
                    base_params, titles[index], season_suffix,
                )
                all_search_tasks.append((params_season, f"Season-{lang}"))

            all_search_tasks.append((params_title_only, f"Title-{lang}"))

        if not all_search_tasks:
            self.logger.warning(f"No search tasks for {indexer.title}. Skipping.")
            return

        self.__execute_search_tasks(
            indexer,
            all_search_tasks,
            search_pattern,
            results_queue,
            counter,
            count_lock,
            unique_seen_keys,
            seen_lock,
        )

    def get_indexers(self):
        started = time.perf_counter()
        if self._indexers is not None:
            self.logger.info(
                "[PERF][IndexerCaps] cache_hit=true / lookup_ms=%.1f",
                (time.perf_counter() - started) * 1000,
            )
            return self._indexers

        cache_key = (self.__base_url, self.__api_key)
        # Shared across requests but isolated by both host and credentials.
        with self._indexer_cache_lock:
            cached = self._indexer_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                self._indexers = cached[1]
                self.logger.info(
                    "[PERF][IndexerCaps] cache_hit=true / lookup_ms=%.1f",
                    (time.perf_counter() - started) * 1000,
                )
                return self._indexers

            self.logger.info("Indexer cache miss. Requesting API...")
            url = f"{self.__base_url}/indexers/all/results/torznab/api"
            params = {
                'apikey': self.__api_key,
                't': 'indexers',
                'configured': 'true',
            }
            try:
                response = self.__session.get(
                    url, params=params, timeout=self.HTTP_TIMEOUT,
                )
                response.raise_for_status()
                indexers = self.__get_indexer_from_xml(response.text)
            except Exception as e:
                self.logger.info(
                    "[PERF][IndexerCaps] cache_hit=false / failed=true / lookup_ms=%.1f",
                    (time.perf_counter() - started) * 1000,
                )
                self.logger.error("Failed to get indexer list: %s", type(e).__name__)
                return []

            self._indexers = indexers
            if len(self._indexer_cache) >= 32:
                self._indexer_cache.clear()
            self._indexer_cache[cache_key] = (
                time.monotonic() + self.INDEXER_CACHE_TTL, indexers,
            )
            self.logger.info("Cached %s indexers for %ss", len(indexers), self.INDEXER_CACHE_TTL)
            self.logger.info(
                "[PERF][IndexerCaps] cache_hit=false / lookup_ms=%.1f / indexers=%s",
                (time.perf_counter() - started) * 1000, len(indexers),
            )
            return indexers

    def __get_indexer_from_xml(self, xml_content):
        xml_root = ET.fromstring(xml_content)
        indexer_list = []
        for item in xml_root.findall('.//indexer'):
            try:
                indexer_id = (item.get('id') or '').strip()
                raw_language = (item.findtext('language') or '').strip()
                if not indexer_id or not raw_language:
                    self.logger.warning("Skipping indexer with missing ID or language")
                    continue

                indexer = JackettIndexer()
                indexer.id = indexer_id
                indexer.title = (item.findtext('title') or indexer_id).strip()
                indexer.link = (item.findtext('link') or '').strip()
                indexer.type = (item.findtext('type') or '').strip()
                indexer.search_available = item.find('.//searching/search[@available="yes"]') is not None
                base_language = raw_language.split('-')[0]
                if base_language in self.PRESERVE_FULL_INDEXER_LOCALES:
                    indexer.language = raw_language
                else:
                    indexer.language = base_language

                self.logger.info(
                    f"Indexer: {indexer.title} - {indexer.link} - {indexer.type}"
                )

                movie_search = item.find('.//searching/movie-search[@available="yes"]')
                tv_search = item.find('.//searching/tv-search[@available="yes"]')

                if movie_search is not None:
                    indexer.movie_search_capatabilities = [
                        part.strip().lower()
                        for part in movie_search.attrib.get('supportedParams', '').split(',')
                        if part.strip()
                    ]
                else:
                    self.logger.info(f"Movie search not available for {indexer.title}")

                if tv_search is not None:
                    indexer.tv_search_capatabilities = [
                        part.strip().lower()
                        for part in tv_search.attrib.get('supportedParams', '').split(',')
                        if part.strip()
                    ]
                else:
                    self.logger.info(f"TV search not available for {indexer.title}")

                indexer_list.append(indexer)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                self.logger.warning("Skipping malformed indexer: %s", exc)
        return indexer_list

    def __get_torrent_links_from_xml(self, xml_content):
        parse_started = time.perf_counter()
        xml_root = ET.fromstring(xml_content)
        result_list = []
        namespace = {'torznab': 'http://torznab.com/schemas/2015/feed'}

        for item in xml_root.findall('.//item'):
            try:
                result = JackettResult()

                def get_text(tag, default=''):
                    node = item.find(tag)
                    return node.text.strip() if node is not None and node.text else default

                result.raw_title = get_text('title', 'Unknown')
                raw_size = get_text('size', '0')
                result.size = raw_size if raw_size.isdigit() else '0'
                result.indexer = get_text('jackettindexer', 'Unknown')
                result.privacy = get_text('type', 'public')

                link = get_text('link', '')
                guid = get_text('guid', '')

                result.link = link if link else guid
                result.detail_link = guid if guid else link

                magnet = item.find('.//torznab:attr[@name="magneturl"]', namespace)
                result.magnet = magnet.get('value') if magnet is not None else None
                info_hash = item.find('.//torznab:attr[@name="infohash"]', namespace)
                result.info_hash = info_hash.get('value') if info_hash is not None else None
                seeders = item.find('.//torznab:attr[@name="seeders"]', namespace)
                try:
                    seeders_value = int(seeders.get('value', '0')) if seeders is not None else 0
                except (TypeError, ValueError):
                    seeders_value = 0
                if seeders_value < 0:
                    continue
                result.seeders = str(seeders_value)
                result_list.append(result)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                self.logger.warning("Skipping malformed Torznab result: %s", exc)

        elapsed = time.perf_counter() - parse_started
        with self._metrics_lock:
            self._metrics["xml_documents"] += 1
            self._metrics["xml_items"] += len(result_list)
            self._metrics["xml_bytes"] += len(xml_content)
            self._metrics["xml_seconds"] += elapsed
            self._metrics["xml_max_seconds"] = max(
                self._metrics["xml_max_seconds"], elapsed
            )
        return result_list

    def __post_process_results(self, results, media):
        language_total = 0.0
        language_cache = {}
        language_cache_hits = 0

        parse_cache = {}
        parse_lock = threading.Lock()

        for result in results:
            raw_title = result.raw_title

            result.parsed_data = _LazyParsedData(
                raw_title,
                parse_cache,
                parse_lock,
            )

            if raw_title in language_cache:
                result.languages = list(language_cache[raw_title])
                language_cache_hits += 1
            else:
                t0 = time.perf_counter()
                detected_languages = detect_languages(raw_title)
                language_total += time.perf_counter() - t0
                language_cache[raw_title] = detected_languages
                result.languages = list(detected_languages)

            result.type = media.type

        self.logger.info(
            f"[PERF] RTN parse deferred: items={len(results)}"
        )

        self.logger.info(
            f"[PERF] Language detect total: {language_total:.3f}s / "
            f"unique={len(language_cache)} / "
            f"hits={language_cache_hits} / "
            f"items={len(results)}"
        )

        return results
