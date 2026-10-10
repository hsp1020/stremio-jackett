import re
import os
import multiprocessing as mp
import time
from collections import Counter
from typing import List, Optional
from concurrent.futures import ProcessPoolExecutor

from RTN import RTN, DefaultRanking, SettingsModel, sort_torrents, parse

try:
    from RTN import batch_parse
except ImportError:
    batch_parse = None
from rapidfuzz import fuzz

from models.media import Media
from models.series import Series
from utils.filter.language_filter import LanguageFilter
from utils.filter.max_size_filter import MaxSizeFilter
from utils.filter.quality_exclusion_filter import QualityExclusionFilter
from utils.filter.results_per_quality_filter import ResultsPerQualityFilter
from utils.filter.title_exclusion_filter import TitleExclusionFilter
from utils.logger import setup_logger

logger = setup_logger(__name__)
quality_order = {"4k": 0, "2160p": 0, "1080p": 1, "720p": 2, "480p": 3}

NON_ENGLISH_CHARS = (
    r'\u0E00-\u0E7F\u0600-\u06FF\u0900-\u097F\u0980-\u09FF'
    r'\u0B80-\u0BFF\u0C00-\u0C7F\u3400-\u4DBF\u4E00-\u9FFF'
    r'\u3040-\u309F\u30A0-\u30FF\u0400-\u04FF'
)
NON_ENGLISH_CHARS_NO_CHINESE = (
    r'\u0E00-\u0E7F\u0600-\u06FF\u0900-\u097F\u0980-\u09FF'
    r'\u0B80-\u0BFF\u0C00-\u0C7F\u3040-\u309F\u30A0-\u30FF'
    r'\u0400-\u04FF'
)

RUSSIAN_CAST_REGEX = re.compile(
    r'(\([^)]*[\u0400-\u04FF][^)]*\))$|(?:\/.*?)(\(.*\))$', re.IGNORECASE
)
ALT_TITLES_REGEX = re.compile(
    fr'[^/|(]*[\d{NON_ENGLISH_CHARS}]+[^/|]*[/|]|[/|][^/|(]*[\d{NON_ENGLISH_CHARS}]+[^/|]*',
    re.IGNORECASE,
)
COMBINED_NON_ENGLISH_REGEX = re.compile(
    fr'(?<!\w)([{NON_ENGLISH_CHARS}\d]+)(?!\w)', re.IGNORECASE
)
NOT_ONLY_NON_ENGLISH_REGEX = re.compile(
    fr'(?:[a-zA-Z][^{NON_ENGLISH_CHARS}]+)([{NON_ENGLISH_CHARS}].*[{NON_ENGLISH_CHARS}])'
    fr'|([{NON_ENGLISH_CHARS}].*[{NON_ENGLISH_CHARS}])(?:[^{NON_ENGLISH_CHARS}]+[a-zA-Z])',
    re.IGNORECASE,
)
COMBINED_NON_ENGLISH_NO_CHINESE_REGEX = re.compile(
    fr'(?<!\w)([{NON_ENGLISH_CHARS_NO_CHINESE}\d]+)(?!\w)', re.IGNORECASE
)
NOT_ONLY_NON_ENGLISH_NO_CHINESE_REGEX = re.compile(
    fr'(?:[a-zA-Z][^{NON_ENGLISH_CHARS_NO_CHINESE}]+)'
    fr'([{NON_ENGLISH_CHARS_NO_CHINESE}].*[{NON_ENGLISH_CHARS_NO_CHINESE}])'
    fr'|([{NON_ENGLISH_CHARS_NO_CHINESE}].*[{NON_ENGLISH_CHARS_NO_CHINESE}])'
    fr'(?:[^{NON_ENGLISH_CHARS_NO_CHINESE}]+[a-zA-Z])', re.IGNORECASE,
)
NOT_ALLOWED_SYMBOLS_START_END_REGEX = re.compile(
    fr'^[^\w{NON_ENGLISH_CHARS}#[\u2605\u3010]+|[ \-:/\\[\]{{(#$&^]+$', re.IGNORECASE
)
REMAINING_NOT_ALLOWED_SYMBOLS_REGEX = re.compile(
    fr'^[^\w{NON_ENGLISH_CHARS}#]+|[\[\]{{}}() ]+$', re.IGNORECASE
)
MOVIE_INDICATOR_REGEX = re.compile(r'(?i)[\[(]movie[\)\]]', re.IGNORECASE)
RELEASE_GROUP_START_REGEX = re.compile(r'^[\[\(【 ★].*?[\]\)】 ★][ .]?(.*)', re.IGNORECASE)
RELEASE_GROUP_END_REGEX = re.compile(r'(.*?)[ .]?[\[\(【 ★].*?[\]\)】 ★]$', re.IGNORECASE)
REDUNDANT_SYMBOLS_END_REGEX = re.compile(r'[ \-:./\\]+$', re.IGNORECASE)
WHITESPACES_REGEX = re.compile(r'\s+', re.IGNORECASE)
KOREAN_BROADCAST_REGEX = re.compile(
    r'\s*(?:\b(?:SBS|UXN|KBS|MBC|JTBC|KBS1|KBS 1|KBS 2|KBS2|MBC TV|'
    r'KBS TV|SBS TV|CHANEL A|채널A|tvN|ENA|MBN|WAVVE|TVING)\b'
    r'|TV조선|티비조선|TV CHOSEN'
    r'|특별기획|주말드라마|월화드라마|수목드라마|금토드라마|일일드라마)\s*',
    re.IGNORECASE,
)
SEASON_EPISODE_PATTERNS = [
    r'(?:[Ss]\s?\d{1,2}[Ee]\s?\d{1,3})',
    r'(?:\d{1,2}[Xxх]\d{1,3})',
    r'(?:시즌\s*\d+\s*(?:화|회|부)\s*\d+)',
    r'(?:에피소드\s*\d+)',
    r'(?:[Ss]\s?\d{1,2})',
    r'(?:[Ss]\d{1,2}[ ._-]?\d{2,3})',
    r'(?:\b[Ss]easons?\b[ .-]?\d{1,2})',
    r'(?:season\d+)',
    r'(?:시즌\s*\d+)',
    r'(?:\d{1,2}(?:st|nd|rd|th)[ .]?[Ss]eason)',
    r'(?:[Ee]\s?\d{1,3})',
    r'(?:[Ee][Pp]?\s?\d{1,3})',
    r'(?:[Ee]pisodes?\s*\d+)',
    r'(?:\d+\s*(?:화|회|부))',
    r'(?:\d{1,3}[ ._-]?(?:of|из|iz)[ ._]?\d{1,3})',
    r'(?:\[\d{2,3}\])',
    r'(?:총\s*\d{1,3}\s*(?:부작|화))',
    r'(?:\d{1,3}\s*부작)',
]
BRACKETS = [('{', '}'), ('[', ']'), ('(', ')')]

MOVIE_EPISODE_REGEX = re.compile(
    r'(?:\bs\d{1,2}e\d{1,4}\b|\bs\d{1,2}\b|\be\d{1,4}\b|'
    r'season\s?\d{1,2}|episode\s?\d{1,4}|ep\s?\d{1,4}|시즌\s?\d{1,2})',
    re.IGNORECASE,
)
YEAR_PATTERN = r'(?:18|19|20)\d{2}'

YEAR_REGEX = re.compile(
    rf'\b({YEAR_PATTERN})\b'
)

YEAR_INLINE_REGEX = re.compile(
    YEAR_PATTERN
)

YEAR_RANGE_REGEX = re.compile(
    rf'({YEAR_PATTERN})\s*[-~]\s*({YEAR_PATTERN})'
)

CHINESE_SEASON_REGEX = re.compile(r'第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*季')
CHINESE_RANGE_REGEX = re.compile(r'\[第\s*(\d+)-(\d+)\s*集\s*\]')
CHINESE_ALL_REGEX = re.compile(r'\[全\s*(\d+)\s*集\s*\]')
CHINESE_SINGLE_REGEX = re.compile(r'\[第\s*(\d+)\s*集\s*\]')
KOREAN_RANGE_REGEXES = (
    re.compile(r'(?:^|\D)(\d{1,3})\s*(?:회|화|부)\s*[-~]\s*(\d{1,3})\s*(?:회|화|부)'),
    re.compile(r'(?:^|\D)(\d{1,3})\s*[-~]\s*(\d{1,3})\s*(?:회|화|부)'),
)
KOREAN_SINGLE_REGEX = re.compile(r'(?:^|\D)(\d{1,2})(?:회|화|부)')
FULL_SERIES_REGEXES = (
    re.compile(r'총\s*(\d{1,3})\s*부작'),
    re.compile(r'총\s*(\d{1,3})\s*화'),
    re.compile(r'(\d{1,3})\s*부작'),
)
EPISODE_RANGE_REGEXES = (
    re.compile(r'(?:e|ep)(\d{1,3})\s*[-~]\s*(?:e|ep)?(\d{1,3})', re.IGNORECASE),
    re.compile(r's\d+e(\d{1,3})\s*[-~]\s*e?(\d{1,3})', re.IGNORECASE),
)
EPISODE_LIST_REGEX = re.compile(
    r'(?:e|ep)(\d{1,3})((?:\s*,\s*\d{1,3})+)', re.IGNORECASE
)

def sort_quality(item):
    parsed_data = item.parsed_data
    if parsed_data.resolution is None:
        return float('inf'), True
    return quality_order.get(parsed_data.resolution, float('inf')), False

def items_sort(items, config):
    total_start = time.perf_counter()

    sort_mode = config['sort']

    logger.info(
        '[PERF] items_sort start: mode=%s / items=%s',
        sort_mode,
        len(items),
    )

    rank_start = time.perf_counter()

    settings = SettingsModel(
        require=[],
        exclude=config['exclusionKeywords'] + config['exclusion'],
    )
    rtn = RTN(
        settings=settings,
        ranking_model=DefaultRanking(),
    )

    torrents = [
        rtn.rank(item.raw_title, item.info_hash)
        for item in items
    ]

    rank_end = time.perf_counter()

    logger.info(
        '[PERF] items_sort RTN.rank: %.3fs / items=%s',
        rank_end - rank_start,
        len(items),
    )

    sort_torrents_start = time.perf_counter()

    sorted_torrents = sort_torrents(
        set(torrents)
    )

    sort_torrents_end = time.perf_counter()

    logger.info(
        '[PERF] items_sort sort_torrents: %.3fs',
        sort_torrents_end - sort_torrents_start,
    )

    mapping_start = time.perf_counter()

    first_by_hash = {}

    for item in items:
        first_by_hash.setdefault(
            item.info_hash,
            item,
        )

    for key, value in sorted_torrents.items():
        item = first_by_hash.get(key)

        if item is not None:
            item.parsed_data = value

    mapping_end = time.perf_counter()

    logger.info(
        '[PERF] items_sort mapping: %.3fs',
        mapping_end - mapping_start,
    )

    final_sort_start = time.perf_counter()

    if sort_mode == 'quality':
        result = sorted(
            items,
            key=sort_quality,
        )
    elif sort_mode == 'sizeasc':
        result = sorted(
            items,
            key=lambda x: int(x.size),
        )
    elif sort_mode == 'seedsdesc':
        result = sorted(
            items,
            key=lambda x: int(x.seeders),
            reverse=True,
        )
    elif sort_mode == 'sizedesc':
        result = sorted(
            items,
            key=lambda x: int(x.size),
            reverse=True,
        )
    elif sort_mode == 'qualitythensize':
        result = sorted(
            items,
            key=lambda x: (
                sort_quality(x),
                -int(x.size),
            ),
        )
    else:
        result = items

    final_sort_end = time.perf_counter()

    logger.info(
        '[PERF] items_sort final sort: %.3fs',
        final_sort_end - final_sort_start,
    )

    logger.info(
        '[PERF] items_sort TOTAL: %.3fs',
        time.perf_counter() - total_start,
    )

    return result

def chinese_to_arabic(cn_str: str) -> Optional[int]:
    cn_nums = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
               '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    if cn_str.isdigit():
        return int(cn_str)
    if len(cn_str) == 1:
        return cn_nums.get(cn_str)
    if len(cn_str) == 2 and cn_str[0] == '十':
        return 10 + cn_nums.get(cn_str[1], 0)
    if len(cn_str) == 2 and cn_str[1] == '十':
        return cn_nums.get(cn_str[0], 0) * 10
    if len(cn_str) == 3 and cn_str[1] == '十':
        return cn_nums.get(cn_str[0], 0) * 10 + cn_nums.get(cn_str[2], 0)
    return None

def _episodes_from_title(raw_title):
    """Extract additional episodes without changing the RTN parser's values."""
    episodes = set()
    range_match = CHINESE_RANGE_REGEX.search(raw_title)
    if range_match:
        start, end = map(int, range_match.groups())
        episodes.update(range(start, end + 1))
    total_match = CHINESE_ALL_REGEX.search(raw_title)
    if total_match:
        episodes.update(range(1, int(total_match.group(1)) + 1))
    single_match = CHINESE_SINGLE_REGEX.search(raw_title)
    if single_match and not range_match:
        episodes.add(int(single_match.group(1)))

    without_years = YEAR_REGEX.sub('', raw_title)
    for pattern in KOREAN_RANGE_REGEXES:
        match = pattern.search(without_years)
        if match:
            start, end = map(int, match.groups())
            if 1 <= start <= end <= 1000:
                episodes.update(range(start, end + 1))
    for value in KOREAN_SINGLE_REGEX.findall(without_years):
        number = int(value)
        if 1 <= number <= 1000:
            episodes.add(number)
    for pattern in FULL_SERIES_REGEXES:
        match = pattern.search(raw_title)
        if match:
            episodes.update(range(1, int(match.group(1)) + 1))
            break
    for pattern in EPISODE_RANGE_REGEXES:
        match = pattern.search(raw_title)
        if match:
            start, end = map(int, match.groups())
            episodes.update(range(start, end + 1))

    for match in EPISODE_LIST_REGEX.finditer(raw_title):
        episodes.add(int(match.group(1)))
        episodes.update(int(value) for value in re.findall(r'\d{1,3}', match.group(2)))
    return episodes

_SHADOW_SEASON_RANGE_REGEX = re.compile(
    r'(?i)(?<![A-Za-z0-9])S(\d{1,2})\s*[-~–—]\s*S?(\d{1,2})(?!\d)'
)

_SHADOW_SEASON_TOKEN_REGEX = re.compile(
    r'(?i)(?<![A-Za-z0-9])S(\d{1,2})(?!\d)'
)

_SHADOW_SEASON_EPISODE_REGEX = re.compile(
    r'(?i)(?<![A-Za-z0-9])S(\d{1,2})[ ._-]*(?:EP|E|Episode)[ ._-]*'
    r'(\d{1,3})(?:\s*[-~–—]\s*(?:(?:EP|E|Episode)[ ._-]*)?(\d{1,3}))?(?!\d)'
)

_SHADOW_EPISODE_REGEX = re.compile(
    r'(?i)(?<![A-Za-z0-9])(?:EP|E|Episode)[ ._-]*(\d{1,3})'
    r'(?:\s*[-~–—]\s*(?:(?:EP|E|Episode)[ ._-]*)?(\d{1,3}))?(?!\d)'
)

def _shadow_expand_range(start, end, maximum=1000):
    start = int(start)
    end = int(end)

    if 1 <= start <= end <= maximum:
        return set(range(start, end + 1))

    return {start, end}

def _shadow_explicit_episode_mismatch(
    raw_title,
    requested_season,
    requested_episode,
    total_episode=None,
):
    """
    Conservative v2.

    실제 제거 조건:
      1. title에 명시적 season 정보가 있고
      2. 요청 season이 그 season/range에 없고
      3. cumulative episode 예외에도 해당하지 않을 때만 제거.

    같은 season의 다른 episode라는 이유만으로는 제거하지 않는다.
    """
    seasons = set()
    episodes = set()

    for match in _SHADOW_SEASON_RANGE_REGEX.finditer(raw_title):
        start_season = int(match.group(1))
        end_season = int(match.group(2))

        if 1 <= start_season <= end_season <= 99:
            seasons.update(
                range(
                    start_season,
                    end_season + 1,
                )
            )
        else:
            seasons.update(
                {
                    start_season,
                    end_season,
                }
            )

    seasons.update(
        int(value)
        for value
        in _SHADOW_SEASON_TOKEN_REGEX.findall(raw_title)
    )

    for match in _SHADOW_SEASON_EPISODE_REGEX.finditer(raw_title):
        start_episode = int(match.group(2))
        end_episode = match.group(3)

        if end_episode is None:
            episodes.add(start_episode)
        else:
            episodes.update(
                _shadow_expand_range(
                    start_episode,
                    end_episode,
                )
            )

    for match in _SHADOW_EPISODE_REGEX.finditer(raw_title):
        start_episode = int(match.group(1))
        end_episode = match.group(2)

        if end_episode is None:
            episodes.add(start_episode)
        else:
            episodes.update(
                _shadow_expand_range(
                    start_episode,
                    end_episode,
                )
            )

    if not seasons:
        return False

    if requested_season in seasons:
        return False

    if (
        total_episode is not None
        and total_episode in episodes
    ):
        return False

    return True

def _shadow_result_key(item):
    info_hash = getattr(item, 'info_hash', None)

    if info_hash:
        return f'hash:{str(info_hash).lower()}'

    magnet = getattr(item, 'magnet', None)

    if magnet:
        match = re.search(
            r'xt=urn:btih:([A-Za-z0-9]+)',
            magnet,
        )

        if match:
            return f'hash:{match.group(1).lower()}'

        return f'magnet:{magnet}'

    link = getattr(item, 'link', None)

    if link:
        return f'link:{link}'

    return f'title:{getattr(item, "raw_title", "")}'

def _matches_requested_episode(item, season, episode, total_episode, extra_episodes):
    parsed_seasons = item.parsed_data.seasons
    parsed_episodes = item.parsed_data.episodes

    if total_episode is not None and (
        total_episode in parsed_episodes or total_episode in extra_episodes
    ):
        return True
    if not parsed_seasons and not parsed_episodes and not extra_episodes:
        return season == 1 and episode == 1
    if not parsed_seasons:
        return episode in parsed_episodes or episode in extra_episodes
    if not parsed_episodes and not extra_episodes:
        return season in parsed_seasons
    return season in parsed_seasons and (
        episode in parsed_episodes or episode in extra_episodes
    )

def filter_out_non_matching(items, season, episode, media: Optional[Media] = None):
    total_episode = None
    if isinstance(media, Series):
        total = getattr(media, 'total_episodes', None)
        if total is not None:
            total_episode = int(total)
    requested_season = int(season.replace('S', ''))
    requested_episode = int(episode.replace('E', ''))
    logger.debug('[filter_out_non_matching] 요청 시즌: %s, 요청 에피소드: %s', season, episode)
    filtered_items = []
    for item in items:
        try:
            raw_title = getattr(item, 'raw_title', '')
            season_match = CHINESE_SEASON_REGEX.search(raw_title)
            if season_match:
                season_value = chinese_to_arabic(season_match.group(1))
                if season_value:
                    existing = set(getattr(item.parsed_data, 'seasons', []))
                    existing.add(season_value)
                    item.parsed_data.seasons = list(existing)
            extra_episodes = _episodes_from_title(raw_title)
            if _matches_requested_episode(
                item, requested_season, requested_episode, total_episode, extra_episodes
            ):
                filtered_items.append(item)
        except Exception as exc:
            logger.error('Error while filtering out non matching torrents', exc_info=exc)
    return filtered_items

def filter_out_season_episode_for_movies(items):
    return [item for item in items if not MOVIE_EPISODE_REGEX.search(item.raw_title)]

def extract_english_title_from_mixed(title: str) -> str:
    title_no_chinese = re.sub(r'[\u4E00-\u9FFF]', '', title)
    title_no_chinese = re.sub(r'\s+', ' ', title_no_chinese).strip()
    season_match = re.search(
        r'(.*?)\s+(?:S\d{1,2}|E\d{1,3}|\d{3,4}p|\d{4}(?:\s|$))',
        title_no_chinese, re.IGNORECASE,
    )
    title_before_season = season_match.group(1).strip() if season_match else title_no_chinese
    words = title_before_season.split()
    if not words:
        return title
    start_idx = 0
    for index, word in enumerate(words):
        if len(word) == 1 or word.isupper():
            continue
        if word[0].isupper():
            start_idx = index
            break
    return ' '.join(words[start_idx:]).strip()

def _strip_title_noise(raw_title):
    """Remove source labels and outer release decorations in original order."""
    title = raw_title.strip()
    title = re.sub(r'[：  :꞉∶]', ' ', title)
    title = re.sub(r'\.6v\s*电 \s*影 .*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'%7[Cc]|%3[Aa]', ' ', title)
    title = KOREAN_BROADCAST_REGEX.sub(' ', title)
    title = re.sub(
        r'^\s*[-\[\]{}()★【  ]*\s*www\.[\w\-]+\.\w{2,}(?:\s*[-–]*)?\s*',
        '', title, flags=re.IGNORECASE,
    )
    title = re.sub(r'【 [^】 ]*】 ', ' ', title)
    title = re.sub(r'\{[^{}\uac00-\ud7a3]*\}|\([^()\uac00-\ud7a3]*\)|\[[^\[\]\uac00-\ud7a3]*\]', ' ', title)
    title = re.sub(r'\bwww\.[\w-]+\.[A-Za-z]{2,}\b', ' ', title)
    title = re.sub(r'[\u200B\u200C\u200D\uFEFF]', '', title)
    title = re.sub(r'[._-]', ' ', title)
    title = re.sub(r'[【 】 ()\[\]:\"\'.,!?;・ 、 。 ~–—〈 〉 《 》 〔 〕 ]', ' ', title)
    title = re.sub(r'\s{2,}', ' ', title).strip()
    title = MOVIE_INDICATOR_REGEX.sub('', title)
    title = NOT_ALLOWED_SYMBOLS_START_END_REGEX.sub('', title)

    for match in RUSSIAN_CAST_REGEX.finditer(title):
        for group in match.groups():
            if group:
                title = title.replace(group, '')
    title = RELEASE_GROUP_START_REGEX.sub(r'\1', title)
    title = RELEASE_GROUP_END_REGEX.sub(r'\1', title)
    return title

def _clean_title_languages(title, keep_chinese):
    """Preserve the original Chinese-aware and alternate-title branches."""
    if not title.replace(' ', '').isnumeric():
        protected_pattern = r'\b(F1RST|NEXT|Sniper)\b'
        parts = title.split('/')
        should_protect = (
            len(parts) > 1
            and bool(re.search(protected_pattern, parts[1], re.IGNORECASE))
        )
        if not should_protect:
            title = ALT_TITLES_REGEX.sub('', title)
        if not re.match(r'^\s*(\d+)\b', title):
            if keep_chinese:
                title = COMBINED_NON_ENGLISH_NO_CHINESE_REGEX.sub('', title)
                for match in NOT_ONLY_NON_ENGLISH_NO_CHINESE_REGEX.finditer(title):
                    for group in match.groups():
                        if group:
                            title = title.replace(group, '')
            else:
                has_chinese = bool(re.search(r'[\u4E00-\u9FFF]', title))
                has_english = bool(re.search(r'[a-zA-Z]', title))
                if has_chinese and has_english:
                    title = extract_english_title_from_mixed(title)
                else:
                    title = COMBINED_NON_ENGLISH_REGEX.sub('', title)
                    for match in NOT_ONLY_NON_ENGLISH_REGEX.finditer(title):
                        for group in match.groups():
                            if group:
                                title = title.replace(group, '')
    return title

def _trim_title_suffix(title):
    """Finish punctuation cleanup and cut release season/episode suffixes."""
    title = REMAINING_NOT_ALLOWED_SYMBOLS_REGEX.sub('', title)
    for open_bracket, close_bracket in BRACKETS:
        if title.count(open_bracket) != title.count(close_bracket):
            title = title.replace(open_bracket, '').replace(close_bracket, '')
    title = REDUNDANT_SYMBOLS_END_REGEX.sub('', title)
    title = WHITESPACES_REGEX.sub(' ', title).strip()
    title = re.sub(r'[._-]', ' ', title)
    title = re.sub(r'\s{2,}', ' ', title).strip()
    for pattern in SEASON_EPISODE_PATTERNS:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            title = title[:match.start()].strip()
            break
    return title

def clean_title(raw_title: str, keep_chinese: bool = False) -> str:
    """Keep the established transformation order; downstream matching depends on it."""
    title = _strip_title_noise(raw_title)
    title = _clean_title_languages(title, keep_chinese)
    return _trim_title_suffix(title)

def _has_chinese(text):
    return bool(re.search(r'[\u4E00-\u9FFF\u3400-\u4DBF]', text))

def _has_korean(text):
    return any('\uac00' <= char <= '\ud7a3' for char in text)

def _extract_korean(text):
    return re.sub(r'\s+', ' ', re.sub(r'[^\uac00-\ud7a3\s]', '', text)).strip()

def _comparison_variants(text, keep_chinese=False):
    base = ' '.join(re.sub(r'(\d+)', r' \1 ', clean_title(text, keep_chinese)).split()).lower()
    variants = [base]
    base_no_article = re.sub(r'^(a|an)\s+', '', base, flags=re.IGNORECASE)
    if base_no_article != base:
        variants.append(base_no_article)
    if _has_korean(base) or (keep_chinese and _has_chinese(base)):
        variants.append(base.replace(' ', ''))
        if base_no_article != base:
            variants.append(base_no_article.replace(' ', ''))
    return variants

def _prepare_variant(text):
    """Compute values reused across comparisons against several titles."""
    korean = _extract_korean(text)
    words = text.split()
    ko_words = korean.split()
    return {
        'text': text,
        'words': words,
        'duplicate_words': len(words) != len(set(words)),
        'remaining_word_counts': Counter(words[1:]),
        'korean': korean,
        'ko_words': ko_words,
        'has_korean': _has_korean(text),
        'has_chinese': _has_chinese(text),
        'no_spaces': text.replace(' ', ''),
        'english': re.sub(r'[^a-z0-9\s]', '', text.lower()),
    }

def _prepare_fuzzy_text(text):
    """Prepare only the fields used by the fuzzy fallback."""
    return {
        'text': text,
        'has_korean': _has_korean(text),
        'korean': _extract_korean(text),
        'english': re.sub(r'[^a-z0-9\s]', '', text.lower()),
    }

def _word_match(item, target):
    """Original exact/word-order fallback, separated from fuzzy matching."""
    if target['has_korean'] and item['has_korean']:
        target_words = target['ko_words']
        item_words = item['ko_words']
        single_word = len(target_words) == 1 and target_words[0] != ''
        if not single_word and item['no_spaces'].startswith(target['korean'].replace(' ', '')):
            return True
        if not target_words or not item_words:
            return False
        if single_word:
            return target_words[0] == item_words[0]
        if len(target_words) >= 2:
            if len(target_words) != len(set(target_words)):
                return len(item_words) >= len(target_words) and item_words[:len(target_words)] == target_words
            return all(word in item_words for word in target_words)
        return False

    target_words = target['words']
    title_words = item['words']
    if not target_words or not title_words:
        return False
    if len(target_words) == 1:
        first_word_match = title_words[0] == target_words[0]
    elif len(target_words) == 2:
        first_word_match = title_words[:2] == target_words[:2]
    else:
        ngram_match = (
            len(title_words) >= 3
            and any(title_words[i:i + 3] == target_words[:3]
                    for i in range(len(title_words) - 2))
        )
        first_word_match = title_words[0] == target_words[0] or ngram_match
    if not first_word_match:
        return False
    if target['duplicate_words']:
        return len(title_words) >= len(target_words) and title_words[:len(target_words)] == target_words
    title_counter = Counter(title_words)
    return all(title_counter[word] >= count for word, count in target['remaining_word_counts'].items())

def _fuzzy_score(text, target):
    if not text['text'].strip() or not target['text'].strip():
        return 0
    if target['has_korean'] and text['has_korean']:
        return fuzz.ratio(text['korean'], target['korean'])
    return fuzz.ratio(text['english'], target['english'])

def _subset_for_first_fuzzy(item_variant, target, media_type, year_pattern, movie_subset):
    if media_type == 'movie':
        if year_pattern.search(target['text']):
            return ' '.join(item_variant['words'][:len(target['words'])])
        return movie_subset

    was_cut = any(re.search(pattern, item_variant['text'], re.IGNORECASE)
                  for pattern in SEASON_EPISODE_PATTERNS)
    if was_cut:
        return item_variant['text']
    return ' '.join(item_variant['words'][:len(target['words'])])

def _matches_standard_title(raw_title, media_type, target_variants, year_pattern):
    processed_variants = [_prepare_variant(value) for value in _comparison_variants(raw_title)]
    movie_subset = None
    fuzzy_cache = {}
    for item_variant in processed_variants:
        for target in target_variants:
            if _word_match(item_variant, target):
                return True
            if media_type == 'movie' and movie_subset is None and not year_pattern.search(target['text']):
                year_match = year_pattern.search(raw_title)
                before_year = raw_title[:year_match.start()].strip() if year_match else raw_title
                movie_subset = clean_title(before_year).lower()
            subset = _subset_for_first_fuzzy(
                item_variant, target, media_type, year_pattern, movie_subset
            )
            if subset not in fuzzy_cache:
                fuzzy_cache[subset] = _prepare_fuzzy_text(subset)
            if _fuzzy_score(fuzzy_cache[subset], target) >= 76:
                return True

    for item_variant in processed_variants:
        for target in target_variants:
            if _fuzzy_score(item_variant, target) >= 76:
                return True
    return False

def _matches_chinese_title(raw_title, target_variants):
    for value in _comparison_variants(raw_title, keep_chinese=True):
        item_variant = _prepare_variant(value)
        for target in target_variants:
            if target['has_chinese'] and item_variant['has_chinese']:
                item_no_space = item_variant['no_spaces']
                target_no_space = target['no_spaces']
                if target_no_space in item_no_space:
                    return True
                if fuzz.ratio(item_no_space, target_no_space) >= 76:
                    return True
    return False

def remove_non_matching_title(items: List, titles: List[str]) -> List:
    standard_targets = [
        _prepare_variant(variant)
        for title in titles for variant in _comparison_variants(title)
    ]

    has_chinese_target = any(_has_chinese(title) for title in titles)

    chinese_targets = (
        [
            _prepare_variant(variant)
            for title in titles
            for variant in _comparison_variants(
                title,
                keep_chinese=True
            )
        ]
        if has_chinese_target
        else []
    )

    year_pattern = YEAR_INLINE_REGEX

    filtered_items = []

    match_cache = {}
    cache_hits = 0
    evaluated = 0

    for item in items:
        if getattr(item, 'imdb_id_search', False):
            filtered_items.append(item)
            continue
        if (
            not hasattr(item, 'parsed_data')
            or not hasattr(item.parsed_data, 'raw_title')
        ):
            continue

        raw_title = item.parsed_data.raw_title
        media_type = getattr(
            item.parsed_data,
            'type',
            'series'
        ).lower()

        cache_key = (raw_title, media_type)

        if cache_key in match_cache:
            matched, chinese_matched = match_cache[cache_key]
            cache_hits += 1

        else:
            evaluated += 1

            standard_matched = _matches_standard_title(
                raw_title,
                media_type,
                standard_targets,
                year_pattern
            )

            chinese_matched = False

            if (
                not standard_matched
                and has_chinese_target
                and _matches_chinese_title(
                    raw_title,
                    chinese_targets
                )
            ):
                chinese_matched = True

            matched = standard_matched or chinese_matched

            match_cache[cache_key] = (
                matched,
                chinese_matched
            )

        if matched:

            if chinese_matched:
                logger.debug(
                    'Matched by Chinese-Preserving Logic: %s',
                    raw_title
                )

            filtered_items.append(item)

    logger.info(
        f"[PERF][TITLE-CACHE] "
        f"items={len(items)} / "
        f"unique={evaluated} / "
        f"hits={cache_hits}"
    )

    logger.debug(
        'Filtered Count: %s/%s',
        len(filtered_items),
        len(items)
    )

    return filtered_items

def get_media_year(media):
    if hasattr(media, 'year') and media.year:
        return int(media.year)
    if hasattr(media, 'release_date') and media.release_date:
        return int(media.release_date[:4])
    if hasattr(media, 'first_air_date') and media.first_air_date:
        return int(media.first_air_date[:4])
    return None

def extract_years_from_title(raw_title: str) -> List[int]:
    return [int(year) for year in YEAR_REGEX.findall(raw_title)]

def is_year_mismatched(media_year: int, raw_title: str, media_titles: List[str]) -> bool:
    if any(YEAR_REGEX.search(title) for title in media_titles):
        return False
    ranges = YEAR_RANGE_REGEX.findall(raw_title)
    if ranges:
        if any(int(start) <= media_year <= int(end) for start, end in ranges):
            return False
        return True
    years = extract_years_from_title(raw_title)
    if not years:
        return False
    if len(years) == 1:
        return abs(media_year - years[0]) > 1
    return media_year not in years

def _rtn_parse_worker(raw_title):
    """
    기존 RTN.parse()와 정확히 동일한 parser를 사용한다.
    PERF 측정을 위해 parse 시간과 worker pid만 추가 반환한다.
    """
    worker_started = time.perf_counter()
    parsed = parse(raw_title)
    worker_elapsed = time.perf_counter() - worker_started

    return (
        raw_title,
        parsed,
        worker_elapsed,
        os.getpid(),
    )

def _rtn_prewarm_worker():
    """
    Spawn worker에서 실제 RTN parse 경로까지 한 번 실행해
    interpreter/import/parser 초기화 비용을 SmartWait 뒤로 숨긴다.
    """
    parse("__rtn_prewarm__")

    time.sleep(0.05)

    return os.getpid()

def start_rtn_prewarm():
    workers = min(
        2,
        max(1, os.cpu_count() or 1),
    )

    if workers <= 1:
        return None

    started = time.perf_counter()

    executor = ProcessPoolExecutor(
        max_workers=workers
    )

    futures = [
        executor.submit(_rtn_prewarm_worker)
        for _ in range(workers)
    ]

    logger.info(
        "[PERF][RTN-PREWARM] started / "
        "workers=%s",
        workers,
    )

    return {
        "executor": executor,
        "futures": futures,
        "started": started,
        "workers": workers,
    }

def shutdown_rtn_prewarm(handle):
    if not handle:
        return

    executor = handle.get("executor")

    if executor is None:
        return

    try:
        executor.shutdown(
            wait=True,
            cancel_futures=False,
        )
    except Exception:
        pass

    handle["executor"] = None

def _materialize_parsed_data(items, rtn_prewarm=None):
    pending = []

    for item in items:
        parsed_data = getattr(
            item,
            'parsed_data',
            None,
        )

        if (
            parsed_data is None
            or getattr(
                parsed_data,
                '_is_lazy_rtn_proxy',
                False,
            )
        ):
            pending.append(item)

    if not pending:
        logger.info(
            '[PERF] Late RTN parse: '
            '0.000s / already materialized'
        )
        return items

    unique_titles = list(
        dict.fromkeys(
            item.raw_title
            for item in pending
        )
    )

    started = time.perf_counter()

    logger.info(
        "[PERF][RTN-MP] "
        "pid=%s / ppid=%s / start_method=%s",
        os.getpid(),
        os.getppid(),
        mp.get_start_method(),
    )

    workers = min(
        2,
        max(1, os.cpu_count() or 1),
    )

    parsed_by_title = {}
    mode = 'parse'

    if (
        workers > 1
        and len(unique_titles) >= 1000
    ):
        executor = None
        owns_executor = False

        try:

            if rtn_prewarm:
                executor = rtn_prewarm.get(
                    "executor"
                )

            pool_wall_started = (
                time.perf_counter()
            )

            if executor is not None:
                warm_wait_started = (
                    time.perf_counter()
                )

                warm_pids = set()

                for future in rtn_prewarm.get(
                    "futures",
                    [],
                ):
                    warm_pids.add(
                        future.result()
                    )

                warm_wait = (
                    time.perf_counter()
                    - warm_wait_started
                )

                prewarm_age = (
                    time.perf_counter()
                    - rtn_prewarm.get(
                        "started",
                        pool_wall_started,
                    )
                )

                logger.info(
                    "[PERF][RTN-PREWARM] "
                    "ready / age=%.3fs / "
                    "wait=%.3fs / pids=%s",
                    prewarm_age,
                    warm_wait,
                    len(warm_pids),
                )

                mode = (
                    f'process-prewarm/'
                    f'{workers}w'
                )

            else:
                executor = (
                    ProcessPoolExecutor(
                        max_workers=workers
                    )
                )

                owns_executor = True
                mode = (
                    f'process/{workers}w'
                )

            first_result_elapsed = None
            worker_cpu_by_pid = {}

            for (
                raw_title,
                parsed,
                worker_elapsed,
                worker_pid,
            ) in executor.map(
                _rtn_parse_worker,
                unique_titles,
                chunksize=64,
            ):
                if first_result_elapsed is None:
                    first_result_elapsed = (
                        time.perf_counter()
                        - pool_wall_started
                    )

                parsed_by_title[
                    raw_title
                ] = parsed

                worker_cpu_by_pid[
                    worker_pid
                ] = (
                    worker_cpu_by_pid.get(
                        worker_pid,
                        0.0,
                    )
                    + worker_elapsed
                )

            pool_wall_elapsed = (
                time.perf_counter()
                - pool_wall_started
            )

            worker_cpu_sum = sum(
                worker_cpu_by_pid.values()
            )

            critical_worker_cpu = max(
                worker_cpu_by_pid.values(),
                default=0.0,
            )

            estimated_overhead = max(
                0.0,
                pool_wall_elapsed
                - critical_worker_cpu,
            )

            logger.info(
                "[PERF][RTN-DEEP] "
                "pool_wall=%.3fs / "
                "first_result=%.3fs / "
                "worker_cpu_sum=%.3fs / "
                "critical_worker=%.3fs / "
                "estimated_pool_ipc_overhead=%.3fs / "
                "worker_pids=%s",
                pool_wall_elapsed,
                first_result_elapsed or 0.0,
                worker_cpu_sum,
                critical_worker_cpu,
                estimated_overhead,
                len(worker_cpu_by_pid),
            )

            if (
                len(parsed_by_title)
                != len(unique_titles)
            ):
                raise RuntimeError(
                    "ProcessPool result mismatch: "
                    f"{len(parsed_by_title)}/"
                    f"{len(unique_titles)}"
                )

        except Exception as exc:
            logger.exception(
                'Parallel RTN parse failed; '
                'falling back to sequential '
                'parse(): %s',
                exc,
            )

            parsed_by_title = {}
            mode = 'parse-fallback'

        finally:

            if (
                owns_executor
                and executor is not None
            ):
                executor.shutdown(
                    wait=True
                )

    if not parsed_by_title:
        for raw_title in unique_titles:
            parsed_by_title[
                raw_title
            ] = parse(raw_title)

    assign_started = time.perf_counter()

    for item in pending:
        item.parsed_data = (
            parsed_by_title[
                item.raw_title
            ]
        )

    assign_elapsed = (
        time.perf_counter()
        - assign_started
    )

    logger.info(
        "[PERF][RTN-DEEP] "
        "assign_items=%.3fs / items=%s",
        assign_elapsed,
        len(pending),
    )

    elapsed = (
        time.perf_counter()
        - started
    )

    logger.info(
        '[PERF] Late RTN parse: %.3fs / '
        'mode=%s / unique=%s / '
        'hits=%s / items=%s',
        elapsed,
        mode,
        len(unique_titles),
        (
            len(pending)
            - len(unique_titles)
        ),
        len(pending),
    )

    return items

def _count_unresolved_dl(items):
    count = 0

    for item in items:
        if getattr(item, "info_hash", None):
            continue

        magnet = getattr(item, "magnet", None)
        if magnet and "xt=urn:btih:" in str(magnet):
            continue

        link = str(getattr(item, "link", "") or "")

        if (
            link.startswith(("http://", "https://"))
            and "/dl/" in link
        ):
            count += 1

    return count

def filter_items(items, media, config, prefetch_callback=None, rtn_prewarm=None):
    filters = {
        'languages': LanguageFilter(config),
        'maxSize': MaxSizeFilter(config, media.type),
        'exclusionKeywords': TitleExclusionFilter(config),
        'exclusion': QualityExclusionFilter(config),
        'resultsPerQuality': ResultsPerQualityFilter(config),
    }

    logger.debug(
        'Item count before filtering: %s',
        len(items),
    )

    pre_rtn_started = time.perf_counter()
    before_pre_rtn = len(items)

    shadow_rejected_titles = set()

    if media.type == 'series':
        requested_season = int(
            media.season.replace('S', '')
        )

        requested_episode = int(
            media.episode.replace('E', '')
        )

        total_episode = getattr(
            media,
            'total_episodes',
            None,
        )

        if total_episode is not None:
            total_episode = int(total_episode)

        shadow_input_titles = {
            getattr(item, 'raw_title', '')
            for item in items
        }

        shadow_removed_items = 0

        for item in items:
            raw_title = getattr(
                item,
                'raw_title',
                '',
            )

            if _shadow_explicit_episode_mismatch(
                raw_title,
                requested_season,
                requested_episode,
                total_episode,
            ):
                shadow_rejected_titles.add(
                    raw_title
                )

                shadow_removed_items += 1

        logger.info(
            '[PERF][RTN-PREFILTER] candidates: '
            'items=%s / unique=%s -> %s / '
            'removed_unique=%s / removed_items=%s',
            len(items),
            len(shadow_input_titles),
            (
                len(shadow_input_titles)
                - len(shadow_rejected_titles)
            ),
            len(shadow_rejected_titles),
            shadow_removed_items,
        )

        items = [
            item
            for item in items
            if getattr(item, 'raw_title', '')
            not in shadow_rejected_titles
        ]

    if media.type == 'movie':
        items = filter_out_season_episode_for_movies(
            items
        )

    media_year = get_media_year(media)

    if media_year and (
        not hasattr(media, 'season')
        or int(
            media.season.replace('S', '')
        ) < 2
    ):
        before = len(items)

        skip_year_check = any(
            YEAR_REGEX.search(title)
            for title in media.titles
        )

        if not skip_year_check:
            items = [
                item
                for item in items
                if not is_year_mismatched(
                    media_year,
                    item.raw_title,
                    [],
                )
            ]

        logger.debug(
            'Filtered by year mismatch: %s items removed',
            before - len(items),
        )
    else:
        logger.debug(
            'Season >= 2, skipping year mismatch filter'
        )

    logger.info(
        '[PERF] Pre-RTN raw filters: %.3fs / %s -> %s',
        time.perf_counter() - pre_rtn_started,
        before_pre_rtn,
        len(items),
    )
    logger.info(
        f"[PERF][DL-CANDIDATES] after_raw="
        f"{_count_unresolved_dl(items)} / items={len(items)}"
    )
    if prefetch_callback is not None:
        prefetch_callback(items)

    items = _materialize_parsed_data(
        items,
        rtn_prewarm=rtn_prewarm,
    )

    if media.type == 'series':
        perf_episode_start = time.perf_counter()
        items = filter_out_non_matching(
            items,
            media.season,
            media.episode,
            media=media,
        )
        logger.info(
            f"[PERF][FILTER] episode_match: "
            f"{time.perf_counter() - perf_episode_start:.3f}s / "
            f"items={len(items)} / "
            f"dl={_count_unresolved_dl(items) if '_count_unresolved_dl' in globals() else -1}"
        )

    logger.info(
        f"[PERF][DL-CANDIDATES] before_title="
        f"{_count_unresolved_dl(items)} / items={len(items)}"
    )

    perf_title_start = time.perf_counter()
    items = remove_non_matching_title(
        items,
        media.titles,
    )
    logger.info(
        f"[PERF][FILTER] title_match: "
        f"{time.perf_counter() - perf_title_start:.3f}s / "
        f"items={len(items)}"
    )
    logger.info(
        f"[PERF][DL-CANDIDATES] after_title="
        f"{_count_unresolved_dl(items)} / items={len(items)}"
    )

    for filter_name, filter_instance in filters.items():
        try:
            perf_filter_stage_start = time.perf_counter()
            items = filter_instance(
                items
            )
            logger.info(
                f"[PERF][FILTER] {filter_name}: "
                f"{time.perf_counter() - perf_filter_stage_start:.3f}s / "
                f"items={len(items)}"
            )
            logger.info(
                f"[PERF][DL-CANDIDATES] after_{filter_name}="
                f"{_count_unresolved_dl(items)} / items={len(items)}"
            )
        except Exception as exc:
            logger.error(
                'Error while filtering by %s',
                filter_name,
                exc_info=exc,
            )

    logger.debug(
        'Final item count after all filtering: %s',
        len(items),
    )

    return items

def sort_items(items, config):
    return items_sort(items, config) if config['sort'] is not None else items
