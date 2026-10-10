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
    # Search lifecycle settings
    SEARCH_TIMEOUT = 15.0
    HTTP_TIMEOUT = 15
    MOVIE_NO_YEAR_MERGE_THRESHOLD = 25
    MOVIE_NO_YEAR_RESULT_TAG = 'movie_no_year'

    # Session and rate-limit settings
    BT4G_MIN_INTERVAL = 61
    BT4G_RATE_LIMIT_STATE_KEY = 'bt4g_global'
    CLOUDFLARE_SESSION_DURATION = 1200

    # Language and media policies
    LANGUAGE_ENGLISH = 'en'
    LANGUAGE_KOREAN = 'ko'
    LANGUAGE_CHINESE = 'zh'
    PRESERVE_FULL_INDEXER_LOCALES = frozenset({'pt'})

    KITSU_ID_PREFIX = 'kitsu:'
    KITSU_BLOCKED_INDEXER_LANGUAGES = frozenset({LANGUAGE_KOREAN})
    KITSU_PART_PATTERN_EN = re.compile(
        r"part\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        re.IGNORECASE,
    )
    KITSU_PART_PATTERN_JP = re.compile(r"\d+クール")

    # Torznab and environment settings
    DISABLE_IMDB_SEARCH_ENV = 'DISABLE_JACKETT_IMDB_SEARCH'
    IMDB_DISABLED_VALUE = 'true'
    IMDB_ID_PARAM = 'imdbid'
    TORZNAB_MOVIE_SEARCH_TYPE = 'movie'
    TORZNAB_MOVIE_CATEGORY = '2000'
    TORZNAB_SERIES_SEARCH_TYPE = 'tvsearch'
    TORZNAB_SERIES_CATEGORY = '5000'

    # Centralized indexer policies. Resolution order is family -> prefix -> exact.
    DEFAULT_INDEXER_POLICY = {
        'search_all_languages': False,
        'series_title_only': False,
        'is_bt4g_rate_limited': False,
        'requires_warmup': False,
        'is_50bt_69bt': False,
    }

    EXACT_INDEXER_POLICIES = {
        'bt4g': {
            'search_all_languages': True,
            'series_title_only': False,
            'is_bt4g_rate_limited': True,
            'requires_warmup': True,
        },
    }

    PREFIX_INDEXER_POLICIES = {
        'btdigg': {
            'search_all_languages': True,
        },
    }

    FAMILY_INDEXER_POLICIES = (
        {
            'name': '50bt_69bt',
            'matcher': 'numeric_prefix_range',
            'numeric_length': 2,
            'minimum': 50,
            'maximum': 69,
            'marker': 'bt',
            'policy': {
                'search_all_languages': True,
                'is_50bt_69bt': True,
            },
        },
    )

    LIMITED_WORD_COUNT = 8

    _last_request_times = {}
    _rate_limit_lock = threading.Lock()

    _indexer_warmup_locks = {}
    _warmup_locks_mutex = threading.Lock()

    def __init__(self, config):
        self.logger = setup_logger(__name__)
        self._indexers = None
        self.__api_key = config['jackettApiKey']
        self.__base_url = f"{config['jackettHost']}/api/v2.0"
        self.__session = requests.Session()

    def _matches_indexer_family(self, idx_id, family_policy):
        if family_policy['matcher'] != 'numeric_prefix_range':
            return False

        numeric_length = family_policy['numeric_length']
        numeric_prefix = idx_id[:numeric_length]
        marker = family_policy['marker']
        marker_end = numeric_length + len(marker)

        return (
            numeric_prefix.isdigit()
            and family_policy['minimum']
            <= int(numeric_prefix)
            <= family_policy['maximum']
            and idx_id[numeric_length:marker_end] == marker
        )

    def _get_indexer_policy(self, indexer):
        idx_id = str(indexer.id).lower().strip()
        idx_lang = str(indexer.language).lower().split('-')[0].strip()

        policy = self.DEFAULT_INDEXER_POLICY.copy()

        for family_policy in self.FAMILY_INDEXER_POLICIES:
            if self._matches_indexer_family(idx_id, family_policy):
                policy.update(family_policy['policy'])

        for prefix, prefix_policy in self.PREFIX_INDEXER_POLICIES.items():
            if idx_id.startswith(prefix):
                policy.update(prefix_policy)

        policy.update(self.EXACT_INDEXER_POLICIES.get(idx_id, {}))
        policy.update({
            'id': idx_id,
            'base_language': idx_lang,
            'is_korean_indexer': idx_lang == self.LANGUAGE_KOREAN,
        })
        return policy

    def _is_kitsu_id(self, media_id):
        normalized_media_id = str(media_id).lower().strip()
        return normalized_media_id.startswith(self.KITSU_ID_PREFIX)

    def _should_skip_kitsu_indexer(self, policy, is_kitsu_id):
        return (
            is_kitsu_id
            and policy['base_language'] in self.KITSU_BLOCKED_INDEXER_LANGUAGES
        )

    def _get_warmup_lock(self, indexer_id):
        with JackettService._warmup_locks_mutex:
            if indexer_id not in JackettService._indexer_warmup_locks:
                JackettService._indexer_warmup_locks[indexer_id] = threading.Lock()
            return JackettService._indexer_warmup_locks[indexer_id]

    def _has_valid_session(self, indexer_id):
        with JackettService._rate_limit_lock:
            last_time = JackettService._last_request_times.get(indexer_id, 0)
            elapsed = time.time() - last_time
            return elapsed < JackettService.CLOUDFLARE_SESSION_DURATION

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

    def _build_query_string(self, title, suffix, is_50bt_69bt, has_year=False):
        if not is_50bt_69bt:
            return f"{title} {suffix}".strip()

        extra_words = len(suffix.split()) if suffix else 0
        if re.search(r'[sS]\d+[eE]\d+', suffix):
            extra_words += 1

        if has_year:
            extra_words += 1

        allowed_title_words = self.LIMITED_WORD_COUNT - extra_words
        if allowed_title_words < 1:
            allowed_title_words = 1

        limited_title = ' '.join(title.split()[:allowed_title_words])
        final_query = f"{limited_title} {suffix}".strip()
        self.logger.debug(
            f"[_build_query_string] Base '{title}' + Suffix '{suffix}' "
            f"(has_year={has_year}) => '{final_query}'"
        )
        return final_query

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
            proceed_with_search = True
            policy = self._get_indexer_policy(indexer)

            # Only BT4G is rate-limited. The complete 50bt-69bt family is independent.
            if policy['is_bt4g_rate_limited']:
                with JackettService._rate_limit_lock:
                    now = time.time()
                    last_req_time = JackettService._last_request_times.get(
                        self.BT4G_RATE_LIMIT_STATE_KEY,
                        0,
                    )
                    elapsed = now - last_req_time

                    if elapsed < JackettService.BT4G_MIN_INTERVAL:
                        self.logger.warning(
                            f"Rate limiting bt4g: Skipping. Elapsed: {elapsed:.2f}s"
                        )
                        proceed_with_search = False
                    else:
                        self.logger.info("Sufficient time for bt4g. Proceeding.")
                        JackettService._last_request_times[
                            self.BT4G_RATE_LIMIT_STATE_KEY
                        ] = now

            if proceed_with_search:
                try:
                    self.logger.info(f"Searching on {indexer.title} ({indexer.id})")
                    if isinstance(media, Movie):
                        self.__search_movie_indexer(
                            media,
                            indexer,
                            results_queue,
                            found_results_count,
                            count_lock,
                            unique_seen_keys,
                            seen_lock,
                        )
                    elif isinstance(media, Series):
                        self.__search_series_indexer(
                            media,
                            indexer,
                            results_queue,
                            found_results_count,
                            count_lock,
                            unique_seen_keys,
                            seen_lock,
                        )
                    else:
                        raise TypeError("Only Movie and Series allowed!")

                    self.logger.info(
                        f"Search logic finished on {indexer.title} "
                        f"({time.time() - start_time:.2f}s)"
                    )

                    with JackettService._rate_limit_lock:
                        JackettService._last_request_times[indexer.id] = time.time()
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

        tasks_to_run_parallel = tasks
        policy = self._get_indexer_policy(indexer)

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

        if policy['requires_warmup'] and not self._has_valid_session(indexer.id):
            warmup_lock = self._get_warmup_lock(indexer.id)
            if warmup_lock.acquire(blocking=False):
                try:
                    self.logger.info(f"Running exclusive warm-up for {indexer.title}...")
                    first_task = tasks[0]
                    first_params, first_name = first_task[0], first_task[1]
                    first_result_tag = first_task[2] if len(first_task) > 2 else None
                    try:
                        first_result = search_func(first_params, first_name)
                        process_and_count(first_result, first_result_tag)

                        with JackettService._rate_limit_lock:
                            JackettService._last_request_times[indexer.id] = time.time()
                    except Exception as e:
                        self.logger.warning(f"Warm-up failed for {indexer.title}: {e}")

                    tasks_to_run_parallel = tasks[1:]
                finally:
                    warmup_lock.release()
            else:
                self.logger.warning(
                    f"Indexer {indexer.title} is busy (warm-up lock). "
                    "Skipping (Non-Blocking)."
                )
                return

        if tasks_to_run_parallel:
            with ThreadPoolExecutor(max_workers=len(tasks_to_run_parallel)) as executor:
                futures = {}
                for task in tasks_to_run_parallel:
                    params, name = task[0], task[1]
                    result_tag = task[2] if len(task) > 2 else None
                    future = executor.submit(search_func, params, name)
                    futures[future] = result_tag

                for future in as_completed(futures):
                    try:
                        data = future.result()
                        process_and_count(data, futures[future])
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

        is_kitsu_id = self._is_kitsu_id(mov_id)
        is_50bt_69bt = policy['is_50bt_69bt']

        if self._should_skip_kitsu_indexer(policy, is_kitsu_id):
            self.logger.info(
                f"🚫 [SKIP] {indexer.title} ({policy['id']}) skipped. "
                f"Reason: Korean Indexer + Kitsu ID ({mov_id})"
            )
            return

        if is_kitsu_id:
            languages = movie.languages * len(movie.titles)
            titles = [title for title in movie.titles for _ in movie.languages]
            has_imdb_search_capability = False
        else:
            has_imdb_search_capability = (
                os.getenv(self.DISABLE_IMDB_SEARCH_ENV) != self.IMDB_DISABLED_VALUE
                and indexer.movie_search_capatabilities is not None
                and self.IMDB_ID_PARAM in indexer.movie_search_capatabilities
            )

            if policy['search_all_languages']:
                languages = movie.languages
                titles = movie.titles
            elif has_imdb_search_capability:
                languages = [self.LANGUAGE_ENGLISH]
                index_of_language = [
                    i
                    for i, lang in enumerate(movie.languages)
                    if lang == self.LANGUAGE_ENGLISH
                ][0]
                titles = [movie.titles[index_of_language]]
            elif idx_lang == self.LANGUAGE_KOREAN:
                index_of_language = []
                for i, title in enumerate(movie.titles):
                    if (
                        movie.languages[i] == self.LANGUAGE_KOREAN
                        or self.LANGUAGE_KOREAN in detect_languages(title)
                    ):
                        index_of_language.append(i)
                if not index_of_language:
                    index_of_language = [0]
                languages = [self.LANGUAGE_KOREAN for _ in index_of_language]
                titles = [movie.titles[i] for i in index_of_language]
            else:
                index_of_language = [
                    i
                    for i, lang in enumerate(movie.languages)
                    if lang == indexer.language or lang == self.LANGUAGE_ENGLISH
                ]
                if not index_of_language:
                    index_of_language = [0]
                languages = [movie.languages[i] for i in index_of_language]
                titles = [movie.titles[i] for i in index_of_language]

        titles = [re.sub(r'\s+', ' ', title).strip() for title in titles]

        def search_with_params(params_dict, search_type):
            kitsu_task_start = time.perf_counter()
            try:
                url = f"{self.__base_url}/indexers/{indexer.id}/results/torznab/api"
                response = self.__session.get(
                    url,
                    params=params_dict,
                    timeout=self.HTTP_TIMEOUT,
                )
                self.logger.debug(
                    f"[Jackett][Movie][{search_type}] URL: {response.request.url}"
                )
                response.raise_for_status()
                task_results = self.__get_torrent_links_from_xml(
                    response.text
                )

                if is_kitsu_id:
                    self.logger.info(
                        "[PERF][KITSU-SEARCH] task: "
                        "media=movie / indexer=%s / "
                        "pattern=%s / elapsed=%.3fs / "
                        "results=%s",
                        indexer.title,
                        search_type,
                        time.perf_counter() - kitsu_task_start,
                        len(task_results),
                    )

                return task_results
            except Exception as e:
                if is_kitsu_id:
                    self.logger.info(
                        "[PERF][KITSU-SEARCH] task: "
                        "media=movie / indexer=%s / "
                        "pattern=%s / elapsed=%.3fs / "
                        "FAILED",
                        indexer.title,
                        search_type,
                        time.perf_counter() - kitsu_task_start,
                    )

                self.logger.error(
                    f"Movie search [{search_type}] failed for {indexer.title}: {str(e)}"
                )
                return []

        phase1_tasks = []
        for index, lang in enumerate(languages):
            query_value = self._build_query_string(
                titles[index],
                '',
                is_50bt_69bt,
                has_year=True,
            )
            params = {
                'apikey': self.__api_key,
                't': self.TORZNAB_MOVIE_SEARCH_TYPE,
                'cat': self.TORZNAB_MOVIE_CATEGORY,
                'q': query_value,
                'year': movie.year,
            }
            if has_imdb_search_capability and not is_kitsu_id:
                params[self.IMDB_ID_PARAM] = mov_id
            phase1_tasks.append((params, f"WithYear-{lang}"))

        no_year_tasks = []
        for index, lang in enumerate(languages):
            is_korean_search = (
                lang == self.LANGUAGE_KOREAN
                or self.LANGUAGE_KOREAN in detect_languages(titles[index])
            )
            if is_korean_search:
                query_value = self._build_query_string(
                    titles[index],
                    '',
                    is_50bt_69bt,
                    has_year=False,
                )
                params_no_year = {
                    'apikey': self.__api_key,
                    't': self.TORZNAB_MOVIE_SEARCH_TYPE,
                    'cat': self.TORZNAB_MOVIE_CATEGORY,
                    'q': query_value,
                }
                if has_imdb_search_capability and not is_kitsu_id:
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

        if is_kitsu_id:
            kitsu_generated_tasks = (
                phase1_tasks + no_year_tasks
            )

            kitsu_seen_params = set()
            kitsu_pattern_counts = {}
            kitsu_unique_task_count = 0

            for task in kitsu_generated_tasks:
                params_dict = task[0]
                pattern_name = task[1]

                params_key = tuple(
                    sorted(
                        (key, str(value))
                        for key, value
                        in params_dict.items()
                    )
                )

                if params_key in kitsu_seen_params:
                    continue

                kitsu_seen_params.add(params_key)
                kitsu_unique_task_count += 1

                pattern_group = pattern_name.rsplit(
                    "-",
                    1,
                )[0]

                kitsu_pattern_counts[
                    pattern_group
                ] = (
                    kitsu_pattern_counts.get(
                        pattern_group,
                        0,
                    )
                    + 1
                )

            self.logger.info(
                "[PERF][KITSU-SEARCH] plan: "
                "media=movie / indexer=%s / "
                "aliases=%s / tasks_raw=%s / "
                "tasks_unique=%s / patterns=%s",
                indexer.title,
                len(titles),
                len(kitsu_generated_tasks),
                kitsu_unique_task_count,
                kitsu_pattern_counts,
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

        is_kitsu_id = self._is_kitsu_id(ser_id)
        is_50bt_69bt = policy['is_50bt_69bt']

        if self._should_skip_kitsu_indexer(policy, is_kitsu_id):
            self.logger.info(
                f"🚫 [SKIP] {indexer.title} ({policy['id']}) skipped. "
                f"Reason: Korean Indexer + Kitsu ID ({ser_id})"
            )
            return

        season = f"{int(series.season.replace('S', '')):02d}"
        episode = f"{int(series.episode.replace('E', '')):02d}"

        if is_kitsu_id:
            languages = series.languages * len(series.titles)
            titles = [title for title in series.titles for _ in series.languages]
            has_imdb_search_capability = False
        else:
            has_imdb_search_capability = (
                os.getenv(self.DISABLE_IMDB_SEARCH_ENV) != self.IMDB_DISABLED_VALUE
                and indexer.tv_search_capatabilities is not None
                and self.IMDB_ID_PARAM in indexer.tv_search_capatabilities
            )

            if policy['search_all_languages']:
                languages = series.languages
                titles = series.titles
            elif has_imdb_search_capability:
                languages = [self.LANGUAGE_ENGLISH]
                index_of_language = next(
                    (
                        i
                        for i, lang in enumerate(series.languages)
                        if lang == self.LANGUAGE_ENGLISH
                    ),
                    0,
                )
                titles = [series.titles[index_of_language]]
            elif idx_lang == self.LANGUAGE_KOREAN:
                index_of_language = []
                for i, title in enumerate(series.titles):
                    if (
                        series.languages[i] == self.LANGUAGE_KOREAN
                        or self.LANGUAGE_KOREAN in detect_languages(title)
                    ):
                        index_of_language.append(i)
                if not index_of_language:
                    index_of_language = [0]
                languages = [self.LANGUAGE_KOREAN for _ in index_of_language]
                titles = [series.titles[i] for i in index_of_language]
            else:
                index_of_language = [
                    i
                    for i, lang in enumerate(series.languages)
                    if lang == indexer.language or lang == self.LANGUAGE_ENGLISH
                ]
                if not index_of_language:
                    index_of_language = [0]
                languages = [series.languages[i] for i in index_of_language]
                titles = [series.titles[i] for i in index_of_language]

        titles = [re.sub(r'\s+', ' ', title).strip() for title in titles]

        def search_pattern(params_dict, pattern_name):
            kitsu_task_start = time.perf_counter()
            try:
                url = f"{self.__base_url}/indexers/{indexer.id}/results/torznab/api"
                response = self.__session.get(
                    url,
                    params=params_dict,
                    timeout=self.HTTP_TIMEOUT,
                )
                self.logger.debug(
                    f"[Jackett][Series][{pattern_name}] URL: {response.request.url}"
                )
                response.raise_for_status()
                task_results = self.__get_torrent_links_from_xml(
                    response.text
                )

                if is_kitsu_id:
                    self.logger.info(
                        "[PERF][KITSU-SEARCH] task: "
                        "media=series / indexer=%s / "
                        "pattern=%s / elapsed=%.3fs / "
                        "results=%s",
                        indexer.title,
                        pattern_name,
                        time.perf_counter() - kitsu_task_start,
                        len(task_results),
                    )

                return task_results
            except Exception as e:
                if is_kitsu_id:
                    self.logger.info(
                        "[PERF][KITSU-SEARCH] task: "
                        "media=series / indexer=%s / "
                        "pattern=%s / elapsed=%.3fs / "
                        "FAILED",
                        indexer.title,
                        pattern_name,
                        time.perf_counter() - kitsu_task_start,
                    )

                self.logger.error(
                    f"Series search [{pattern_name}] failed for {indexer.title}: {str(e)}"
                )
                return []

        def create_params(base_params, title, suffix):
            params = base_params.copy()
            params['q'] = self._build_query_string(title, suffix, is_50bt_69bt)
            return params

        # imdbid 지원 여부와 season/ep 지원 여부는 서로 독립적이다.
        # Kitsu는 imdbid를 사용하지 않더라도, 인덱서가 season/ep를
        # 지원하면 구조화된 TV search 파라미터를 사용할 수 있다.
        tv_search_capabilities = set(
            indexer.tv_search_capatabilities or []
        )

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

        all_search_tasks = []
        for index, lang in enumerate(languages):
            base_params = {
                'apikey': self.__api_key,
                't': self.TORZNAB_SERIES_SEARCH_TYPE,
                'cat': self.TORZNAB_SERIES_CATEGORY,
            }
            if has_imdb_search_capability and not is_kitsu_id:
                base_params[self.IMDB_ID_PARAM] = ser_id.split(':', 1)[0]

            params_title_only = create_params(base_params, titles[index], '')

            is_kitsu_part_series = False
            if is_kitsu_id:
                if (
                    self.KITSU_PART_PATTERN_EN.search(titles[index])
                    or self.KITSU_PART_PATTERN_JP.search(titles[index])
                ):
                    is_kitsu_part_series = True

            if policy['series_title_only']:
                all_search_tasks.append((params_title_only, f"TitleOnly-{lang}"))
                continue

            if is_kitsu_part_series:
                # Part 표기가 있는 Kitsu는 기존 E01 텍스트 검색 의미를 유지한다.
                params_episode = create_params(
                    base_params,
                    titles[index],
                    f"E{episode}",
                )
                all_search_tasks.append((params_episode, f"Episode-{lang}"))
            elif lang == self.LANGUAGE_KOREAN:
                if int(season) >= 2:
                    params_episode = create_episode_params(
                        base_params,
                        titles[index],
                        f"{int(season)} e{episode}",
                    )
                    all_search_tasks.append((params_episode, f"Episode-{lang}"))

                    # 시즌 없는 누적/한국식 에피소드 검색은 별도 패턴이므로
                    # 구조화 검색으로 치환하지 않는다.
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
                not is_kitsu_part_series
                and int(season) >= 2
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

            if not is_kitsu_part_series:
                if int(season) >= 2 or lang != self.LANGUAGE_KOREAN:
                    if lang == self.LANGUAGE_KOREAN:
                        season_suffix = f"{int(season)}"
                    else:
                        season_suffix = f"S{season}"
                    params_season = create_season_params(
                        base_params,
                        titles[index],
                        season_suffix,
                    )
                    all_search_tasks.append((params_season, f"Season-{lang}"))

            all_search_tasks.append((params_title_only, f"Title-{lang}"))

        if not all_search_tasks:
            self.logger.warning(f"No search tasks for {indexer.title}. Skipping.")
            return

        if is_kitsu_id:
            kitsu_seen_params = set()
            kitsu_pattern_counts = {}
            kitsu_unique_task_count = 0

            for task in all_search_tasks:
                params_dict = task[0]
                task_pattern_name = task[1]

                params_key = tuple(
                    sorted(
                        (key, str(value))
                        for key, value
                        in params_dict.items()
                    )
                )

                if params_key in kitsu_seen_params:
                    continue

                kitsu_seen_params.add(params_key)
                kitsu_unique_task_count += 1

                pattern_group = (
                    task_pattern_name.rsplit(
                        "-",
                        1,
                    )[0]
                )

                kitsu_pattern_counts[
                    pattern_group
                ] = (
                    kitsu_pattern_counts.get(
                        pattern_group,
                        0,
                    )
                    + 1
                )

            self.logger.info(
                "[PERF][KITSU-SEARCH] plan: "
                "media=series / indexer=%s / "
                "aliases=%s / tasks_raw=%s / "
                "tasks_unique=%s / patterns=%s",
                indexer.title,
                len(titles),
                len(all_search_tasks),
                kitsu_unique_task_count,
                kitsu_pattern_counts,
            )

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
        if not self._indexers:
            self.logger.info("Indexer cache miss. Requesting API...")
            url = f"{self.__base_url}/indexers/all/results/torznab/api"
            params = {
                'apikey': self.__api_key,
                't': 'indexers',
                'configured': 'true',
            }
            try:
                response = self.__session.get(
                    url,
                    params=params,
                    timeout=self.HTTP_TIMEOUT,
                )
                response.raise_for_status()
                self._indexers = self.__get_indexer_from_xml(response.text)
                self.logger.info(
                    f"Successfully retrieved {len(self._indexers)} indexers from "
                    "Jackett. Storing in cache..."
                )
            except Exception as e:
                self.logger.error(
                    f"An exception occured while getting indexers from Jackett: {str(e)}"
                )
                return []
        return self._indexers

    def __get_indexer_from_xml(self, xml_content):
        xml_root = ET.fromstring(xml_content)
        indexer_list = []
        for item in xml_root.findall('.//indexer'):
            indexer = JackettIndexer()
            indexer.title = item.find('title').text
            indexer.id = item.attrib['id']
            indexer.link = item.find('link').text
            indexer.type = item.find('type').text
            raw_language = item.find('language').text
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
                indexer.movie_search_capatabilities = movie_search.attrib[
                    'supportedParams'
                ].split(',')
            else:
                self.logger.info(f"Movie search not available for {indexer.title}")

            if tv_search is not None:
                indexer.tv_search_capatabilities = tv_search.attrib[
                    'supportedParams'
                ].split(',')
            else:
                self.logger.info(f"TV search not available for {indexer.title}")

            indexer_list.append(indexer)
        return indexer_list

    def __get_torrent_links_from_xml(self, xml_content):
        xml_root = ET.fromstring(xml_content)
        result_list = []
        namespace = {'torznab': 'http://torznab.com/schemas/2015/feed'}

        for item in xml_root.findall('.//item'):
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
            result.magnet = magnet.attrib['value'] if magnet is not None else None
            info_hash = item.find('.//torznab:attr[@name="infohash"]', namespace)
            result.info_hash = info_hash.attrib['value'] if info_hash is not None else None
            seeders = item.find('.//torznab:attr[@name="seeders"]', namespace)
            result.seeders = seeders.attrib['value'] if seeders is not None else '0'

            if int(result.seeders) <= -1:
                continue
            result_list.append(result)

        return result_list

    def __post_process_results(self, results, media):
        language_total = 0.0
        language_cache = {}
        language_cache_hits = 0

        # Request-scoped RTN cache.
        # filter_results가 실제 parsed metadata를 필요로 할 때까지
        # RTN parse를 실행하지 않는다.
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
