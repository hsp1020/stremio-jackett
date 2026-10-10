from unittest.mock import patch
from queue import Queue
from threading import Lock
from requests.exceptions import ConnectionError
from types import SimpleNamespace

from jackett.jackett_indexer import JackettIndexer
from jackett.jackett_result import JackettResult
from jackett.jackett_service import JackettService
from metdata.tmdb import TMDB
from models.movie import Movie
from models.series import Series
from torrent.torrent_smart_container import TorrentSmartContainer
from utils.filter_results import remove_non_matching_title, filter_out_non_matching
from utils.filter.language_filter import LanguageFilter
from utils.filter.quality_exclusion_filter import QualityExclusionFilter


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def mapping(imdb_season=None, tvdb_season=None, imdb_id="tt36034547",
            trakt_season=None, tmdb_season=None):
    return {
        "success": True,
        "data": {
            "mappings": {"imdbId": imdb_id, "kitsuId": 50634, "malId": 12345, "themoviedbId": 278043},
            "imdb": {"seasonNumber": imdb_season} if imdb_season is not None else None,
            "tvdb": {"seasonNumber": tvdb_season},
            "trakt": {"seasonNumber": trakt_season},
            "tmdb": {"seasonNumber": tmdb_season, "fromEpisode": 13},
        },
    }


tmdb = TMDB({"tmdbApi": "test", "languages": ["en"]})
with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(tvdb_season=2))):
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "1"], "series") == [
        "tt36034547", "2", "1"
    ]
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634"], "movie") == ["tt36034547"]
    with patch.object(tmdb, "_TMDB__get_tmdb_metadata", side_effect=lambda i, parts, kind: (i, parts, kind)):
        result = tmdb.get_metadata("kitsu:50634:5", "series")
        assert result == ("tt36034547:2:5", ["tt36034547", "2", "5"], "series")

# MAL input follows the same IMDb conversion with MAL-specific request and identity check.
with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(tvdb_season=2))) as network:
    assert tmdb._TMDB__convert_anime_to_imdb(["mal", "12345", "5"], "series") == [
        "tt36034547", "2", "5"
    ]
    assert network.call_args.kwargs["params"] == {"idType": "malId", "idValue": 12345}
    assert tmdb._TMDB__convert_anime_to_imdb(["mal", "12345"], "movie") == ["tt36034547"]
    with patch.object(tmdb, "_TMDB__get_tmdb_metadata", side_effect=lambda i, parts, kind: (i, parts, kind)):
        assert tmdb.get_metadata("mal:12345:7", "series") == (
            "tt36034547:2:7", ["tt36034547", "2", "7"], "series"
        )

wrong_mal = mapping(tvdb_season=2)
wrong_mal["data"]["mappings"]["malId"] = 99999
with patch("metdata.tmdb.requests.get",
           side_effect=[FakeResponse(wrong_mal), FakeResponse(mapping(trakt_season=3))]) as network:
    assert tmdb._TMDB__convert_anime_to_imdb(["mal", "12345", "8"], "series") == [
        "tt36034547", "3", "8"
    ]
    assert network.call_count == 2

with patch("metdata.tmdb.requests.get") as network:
    assert tmdb.get_metadata("mal:invalid:1", "series") is None
    assert tmdb._TMDB__convert_anime_to_imdb(["mal", "12345", "0"], "series") is None
    network.assert_not_called()

with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(imdb_season=1, tvdb_season=3))):
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "1"], "series") == [
        "tt36034547", "1", "1"
    ]
with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping())):
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "1"], "series") is None

# Use existing AIOStreams JSON season values, without calling the Trakt API.
for data, expected in (
    (mapping(imdb_season=1, tvdb_season=2, trakt_season=3, tmdb_season=4), 1),
    (mapping(imdb_season=0, tvdb_season=2, trakt_season=3, tmdb_season=4), 2),
    (mapping(trakt_season=3, tmdb_season=4), 3),
    (mapping(tmdb_season=4), 4),
):
    with patch("metdata.tmdb.requests.get", return_value=FakeResponse(data)) as network:
        assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "8"], "series") == [
            "tt36034547", str(expected), "8"
        ]
        network.assert_called_once()

with patch("metdata.tmdb.requests.get") as network:
    assert tmdb.get_metadata("trakt:1234", "movie") is None
    network.assert_not_called()

service = JackettService({"jackettApiKey": "test", "jackettHost": "http://localhost"})
indexer = JackettIndexer()
indexer.id = "regular"
indexer.title = "Regular"
indexer.language = "ko"
indexer.movie_search_capatabilities = ["q", "imdbid", "year"]
indexer.tv_search_capatabilities = ["q", "imdbid", "season", "ep"]
captured = []
service._JackettService__execute_search_tasks = (
    lambda _indexer, tasks, *_rest: captured.extend(tasks)
)
service._JackettService__search_movie_indexer(
    Movie(id="tt26743760", titles=["한국어 영화"], year="2026", languages=["ko"]),
    indexer, None, None, None, None, None,
)
assert len(captured) == 1 and captured[0][0]["imdbid"] == "tt26743760"
assert "q" not in captured[0][0] and "year" not in captured[0][0]
captured.clear()
service._JackettService__search_series_indexer(
    Series(id="tt26743760:3:1", titles=["한국어 드라마"],
           season="S03", episode="E01", languages=["ko"]),
    indexer, None, None, None, None, None,
)
assert len(captured) == 1
assert captured[0][0]["imdbid"] == "tt26743760"
assert captured[0][0]["season"] == 3 and captured[0][0]["ep"] == 1
assert "q" not in captured[0][0]
captured.clear()
service._JackettService__search_movie_indexer(
    Movie(id="tt26743760", titles=["English Movie"], year="2026", languages=["en"]),
    indexer, None, None, None, None, None,
)
assert captured and captured[0][0]["q"] == "English Movie"
assert captured[0][0]["imdbid"] == "tt26743760"
captured.clear()
service._JackettService__search_movie_indexer(
    Movie(id="tmdb:42", titles=["Movie"], year="2026", languages=["en"]),
    indexer, None, None, None, None, None,
)
assert captured and all("imdbid" not in task[0] for task in captured)
assert service._get_indexer_policy(indexer)["search_all_languages"] is False
indexer.id = "btdigg3"
assert service._get_indexer_policy(indexer)["search_all_languages"] is True
indexer.id = "bt4g"
assert service._get_indexer_policy(indexer)["search_all_languages"] is False
assert not hasattr(service, "BT4G_MIN_INTERVAL")

low = SimpleNamespace(info_hash="ABCD", link="magnet:a", raw_title="low",
                      indexer="torrentzota", seeders=2)
high = SimpleNamespace(info_hash="ABCD", link="magnet:b", raw_title="high",
                       indexer="regular", seeders=20)
container = TorrentSmartContainer([low, high], None)
assert container.get_items() == [high]

# Reject mismatched API records, and try the next AIOStreams instance.
wrong_id = mapping(tvdb_season=2)
wrong_id["data"]["mappings"]["kitsuId"] = 99999
with patch("metdata.tmdb.requests.get",
           side_effect=[FakeResponse(wrong_id), FakeResponse(mapping(tvdb_season=2))]) as network:
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "8"], "series") == [
        "tt36034547", "2", "8"
    ]
    assert network.call_count == 2

with patch("metdata.tmdb.requests.get",
           side_effect=[ConnectionError("first host down"), FakeResponse(mapping(tvdb_season=2))]) as network:
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "2"], "series") == [
        "tt36034547", "2", "2"
    ]
    assert network.call_count == 2

with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(imdb_season=0, tvdb_season=3))):
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "1"], "series") == [
        "tt36034547", "3", "1"
    ]
with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(imdb_season=2, tvdb_season=1))):
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "50634", "1"], "series") == [
        "tt36034547", "2", "1"
    ]
with patch("metdata.tmdb.requests.get") as network:
    assert tmdb._TMDB__convert_anime_to_imdb(["kitsu", "-1", "1"], "series") is None
    network.assert_not_called()

# Verify t=indexers capability parsing and mode selection, without per-indexer t=caps HTTP calls.
xml = """
<indexers>
  <indexer id="caps-example">
    <title>Caps Example</title><link>http://example.invalid</link>
    <type>public</type><language>en</language>
    <caps><searching>
      <search available="yes" supportedParams="q"/>
      <movie-search available="yes" supportedParams="q,imdbid,year"/>
      <tv-search available="no" supportedParams="q"/>
    </searching></caps>
  </indexer>
</indexers>
"""
parsed = service._JackettService__get_indexer_from_xml(xml)
assert len(parsed) == 1
assert parsed[0].search_available is True
assert parsed[0].movie_search_capatabilities == ["q", "imdbid", "year"]
assert parsed[0].tv_search_capatabilities is None

captured.clear()
generic = parsed[0]
service._JackettService__search_series_indexer(
    Series(id="tt26743760:3:1", titles=["Test Series"],
           season="S03", episode="E01", languages=["en"]),
    generic, None, None, None, None, None,
)
assert captured and all(t[0]["t"] == "search" and "imdbid" not in t[0] for t in captured)
assert any("S03E01" in t[0]["q"] for t in captured)

captured.clear()
generic.movie_search_capatabilities = None
service._JackettService__search_movie_indexer(
    Movie(id="tt26743760", titles=["Test Movie"], year="2026", languages=["en"]),
    generic, None, None, None, None, None,
)
assert len(captured) == 1 and captured[0][0]["t"] == "search"
assert "imdbid" not in captured[0][0] and "year" not in captured[0][0]

captured.clear()
generic.search_available = False
service._JackettService__search_movie_indexer(
    Movie(id="tt26743760", titles=["Test Movie"], year="2026", languages=["en"]),
    generic, None, None, None, None, None,
)
assert not captured

captured.clear()
indexer.movie_search_capatabilities = ["imdbid"]
service._JackettService__search_movie_indexer(
    Movie(id="tt26743760", titles=["English Movie"], year="2026", languages=["en"]),
    indexer, None, None, None, None, None,
)
assert len(captured) == 1 and captured[0][0]["t"] == "movie"
assert captured[0][0]["imdbid"] == "tt26743760" and "q" not in captured[0][0]

# Tag results by the actual Torznab search parameters, not by indexer capability.
source_service = JackettService({"jackettApiKey": "test", "jackettHost": "http://localhost"})
source_queue = Queue()

def stub_search(params, task_name):
    result = JackettResult()
    result.raw_title = "Unrelated Movie S02E03"
    result.info_hash = "hash-" + task_name
    result.parsed_data = SimpleNamespace(
        raw_title=result.raw_title, type="series", seasons=[2], episodes=[3]
    )
    return [result]

source_service._JackettService__execute_search_tasks(
    indexer,
    [
        ({"t": "tvsearch", "imdbid": "tt26743760", "q": "Different"}, "id"),
        ({"t": "search", "q": "Different"}, "title"),
    ],
    stub_search,
    source_queue,
    [0],
    Lock(),
    set(),
    Lock(),
)
results = []
while not source_queue.empty():
    results.extend(source_queue.get_nowait())
assert len(results) == 2
imdb_hit = next(result for result in results if result.imdb_id_search)
title_hit = next(result for result in results if not result.imdb_id_search)
assert imdb_hit.imdb_id_search is True and title_hit.imdb_id_search is False
assert JackettResult().imdb_id_search is False
with patch("utils.filter_results._matches_standard_title", return_value=False):
    assert remove_non_matching_title([imdb_hit, title_hit], ["Desired Series"]) == [imdb_hit]
assert filter_out_non_matching([imdb_hit], "S02", "E03") == [imdb_hit]
assert filter_out_non_matching([imdb_hit], "S02", "E04") == []

# Filtering one excluded item must not truncate subsequent results.
quality_items = [
    SimpleNamespace(parsed_data=SimpleNamespace(quality=q))
    for q in ("WEB-DL", "CAM", "BluRay")
]
assert QualityExclusionFilter({"exclusion": ["CAM"]})(quality_items) == [
    quality_items[0], quality_items[2]
]
unknown_item = SimpleNamespace(parsed_data=SimpleNamespace(quality=None))
assert QualityExclusionFilter({"exclusion": ["Unknown"]})([unknown_item]) == []

# Multi-language and 'multi' tags cannot occupy multiple result slots.
multi_item = SimpleNamespace(languages=["ko", "en", "multi"])
single_item = SimpleNamespace(languages=["ko"])
assert LanguageFilter({"languages": ["ko", "en"], "getAllLanguages": False})(
    [multi_item, single_item]
) == [multi_item, single_item]

print("Kitsu/MAL anime normalization, season fallback, search provenance and filter tests passed")

# Phase 1+2 regression checks: malformed feeds, filtering, priorities and order.
from utils.filter.title_exclusion_filter import TitleExclusionFilter
from utils.filter.max_size_filter import MaxSizeFilter
from utils.filter.results_per_quality_filter import ResultsPerQualityFilter
from utils.filter_results import sort_items
from utils.stremio_parser import parse_to_stremio_streams

excluded = SimpleNamespace(raw_title="Movie.2026.CAM.WEB", size="1024")
kept = SimpleNamespace(raw_title="Movie.2026.WEB", size="1024")
assert TitleExclusionFilter({"exclusionKeywords": ["cam"]})([excluded, kept]) == [kept]

gib = 1024 ** 3
size_items = [
    SimpleNamespace(size=str(gib)),
    SimpleNamespace(size=str(2 * gib)),
    SimpleNamespace(size="0"),
]
assert MaxSizeFilter({"maxSize": "1.5"}, "movie")(size_items) == [
    size_items[0], size_items[2]
]
assert MaxSizeFilter({"maxSize": "1.5"}, "series")(size_items) == size_items

broken_indexers = """
<indexers>
  <indexer id="broken"><title>Missing language</title></indexer>
  <indexer id="ok"><title>Working</title><language>en</language>
    <caps><searching><search available="yes" supportedParams="q"/></searching></caps>
  </indexer>
</indexers>"""
parsed_indexers = service._JackettService__get_indexer_from_xml(broken_indexers)
assert len(parsed_indexers) == 1 and parsed_indexers[0].id == "ok"
broken_feed = """
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
  <item><title>Bad seeds</title><size>99</size>
    <torznab:attr name="seeders" value="not-a-number" /></item>
  <item><title>Negative seeds</title><size>99</size>
    <torznab:attr name="seeders" value="-1" /></item>
  <item><title>Good</title><size>1000</size>
    <torznab:attr name="seeders" value="40" /></item>
</channel></rss>"""
feed = service._JackettService__get_torrent_links_from_xml(broken_feed)
assert [item.raw_title for item in feed] == ["Bad seeds", "Good"]
assert [item.seeders for item in feed] == ["0", "40"]

def ranked(title, resolution, size, seeders):
    return SimpleNamespace(
        raw_title=title, size=str(size), seeders=str(seeders),
        parsed_data=SimpleNamespace(resolution=resolution),
    )

candidates = [
    ranked("small-low", "1080p", 100, 1),
    ranked("large-high", "1080p", 400, 40),
    ranked("medium-mid", "1080p", 200, 20),
    ranked("other", "720p", 150, 5),
]
limiter = ResultsPerQualityFilter({"resultsPerQuality": "2"})
assert [i.raw_title for i in limiter(sort_items(candidates, {
    "sort": "sizedesc", "resultsPerQuality": "2"
}))] == ["large-high", "medium-mid", "other"]
assert [i.raw_title for i in limiter(sort_items(candidates, {
    "sort": "seedsdesc", "resultsPerQuality": "2"
}))] == ["large-high", "medium-mid", "other"]
assert [i.raw_title for i in limiter(sort_items(candidates, {
    "sort": "sizeasc", "resultsPerQuality": "2"
}))] == ["small-low", "other", "medium-mid"]
assert [i.raw_title for i in limiter(sort_items(candidates, {
    "sort": None, "resultsPerQuality": "2"
}))] == ["large-high", "medium-mid", "other"]
assert [i.raw_title for i in sort_items(candidates, {
    "sort": "quality", "resultsPerQuality": "0"
})] == ["small-low", "large-high", "medium-mid", "other"]
assert [i.raw_title for i in sort_items(candidates, {
    "sort": "qualitythensize", "resultsPerQuality": "0"
})] == ["large-high", "medium-mid", "small-low", "other"]

def stream_item(name, cached):
    return SimpleNamespace(
        raw_title=name, size=str(gib), seeders="10", indexer="Mock",
        info_hash=name, file_name=None, file_index=1, privacy="public",
        languages=["en"], availability=cached,
        parsed_data=SimpleNamespace(
            codec=None, audio=[], quality="WEB-DL", resolution="1080p"
        ),
        to_debrid_stream_query=lambda media: {
            "magnet": "magnet:?xt=urn:btih:ABCDEF",
            "type": media.type, "file_index": 1, "season": None,
            "episode": None, "torrent_download": None,
        },
    )

encoded_config = {
    "maxResults": "2", "addonHost": "https://example.invalid",
    "debrid": True, "torrenting": True,
}
formatted = parse_to_stremio_streams(
    [stream_item("First", False), stream_item("Second", True)],
    encoded_config, Movie("tt12345", ["Movie"], "2026", ["en"])
)
assert len(formatted) == 4
assert [row["description"].split("\n")[0] for row in formatted] == [
    "Second", "First", "First", "Second"
]
assert all(("url" in row) != ("infoHash" in row) for row in formatted)
print("Phase 1+2 filtering, Torznab, ranking, cap and formatter regressions passed")

# Exercise filter_items through its real stage sequence without network/RTN reparsing.
from utils.filter_results import filter_items

def filtered_candidate(title, resolution, seeders, size):
    item = JackettResult()
    item.raw_title = title
    item.size = str(size)
    item.seeders = str(seeders)
    item.languages = ["en"]
    item.info_hash = title
    item.imdb_id_search = True
    item.parsed_data = SimpleNamespace(
        raw_title=title, type="movie", resolution=resolution, quality="WEB-DL"
    )
    return item

phase_config = {
    "languages": ["en"], "getAllLanguages": False,
    "maxSize": "2", "exclusionKeywords": ["BLOCK"],
    "exclusion": [], "resultsPerQuality": "1", "sort": "seedsdesc",
}
phase_inputs = [
    filtered_candidate("Movie 2026 1080p low", "1080p", 1, gib),
    filtered_candidate("Movie 2026 1080p best", "1080p", 60, gib),
    filtered_candidate("Movie 2026 BLOCK", "1080p", 999, gib),
    filtered_candidate("Movie 2026 1080p oversized", "1080p", 888, 3 * gib),
    filtered_candidate("Movie 2026 720p", "720p", 5, gib),
]
phase_result = filter_items(
    phase_inputs, Movie("tt12345", ["Movie"], "2026", ["en"]), phase_config
)
assert [item.raw_title for item in phase_result] == [
    "Movie 2026 1080p best", "Movie 2026 720p"
]
print("Phase 1+2 integrated filter sequence passed")


# Phase 3+5 regression tests. All external HTTP calls remain mocked.
import inspect
import json
import bencode
from io import BytesIO
from unittest.mock import Mock
from torrent.torrent_item import TorrentItem
from torrent.torrent_service import TorrentService
from utils.detection import detect_languages, detect_subtitle_hint
from utils.stremio_parser import get_emoji
from utils.cache import search_cache
from debrid.base_debrid import BaseDebrid

assert detect_languages("A.Movie.KOR.SUB.1080p") == ["unknown"]
assert detect_subtitle_hint("A.Movie.KOR.SUB.1080p") == "KO"
assert detect_languages("A.Movie.KOR.DUB.1080p") == ["ko"]
assert detect_languages("An.Anime.JPN.1080p") == ["ja"]
assert detect_languages("A.Series.CHINESE.1080p") == ["zh"]
assert detect_languages("A.Movie.1080p") == ["unknown"]
assert get_emoji("ko") == "🇰🇷" and get_emoji("ja") == "🇯🇵"
assert get_emoji("zh") == "🇨🇳" and get_emoji("unknown") == "❔"
unknown = SimpleNamespace(languages=["unknown"])
assert LanguageFilter({"languages": ["en"], "getAllLanguages": False})([unknown]) == [unknown]
assert LanguageFilter({"languages": ["ko"], "getAllLanguages": False})([unknown]) == []

def make_torrent_item(kind):
    return TorrentItem(
        "Example", "0", "magnet:?xt=urn:btih:0123456789012345678901234567890123456789",
        None, "http://localhost/dl/test", "5", ["en"], "Indexer", "public",
        kind, SimpleNamespace(seasons=[2], episodes=[4], codec=None, audio=[],
                              quality="WEB-DL", resolution="1080p"),
    )

def torrent_bytes(file_entries=None, name="Movie.mkv"):
    info = {"name": name, "piece length": 16384, "pieces": b"x" * 20}
    if file_entries is None:
        info["length"] = 1234567
    else:
        info["files"] = [{"length": length, "path": path}
                         for length, path in file_entries]
    return bencode.bencode({"announce": "http://localhost/announce", "info": info})

movie_service = TorrentService(Movie("tt100", ["Movie"], "2026", ["en"]))
movie_torrent = movie_service._TorrentService__process_torrent(
    make_torrent_item("movie"),
    torrent_bytes([
        (5 * 1024 ** 2, ["Movie", "trailer.mp4"]),
        (100 * 1024 ** 2, ["Movie", "Movie.mkv"]),
    ])
)
assert movie_torrent.file_index == 2
assert movie_torrent.direct_file_index == 1
assert movie_torrent.direct_file_missing is False
single_file = movie_service._TorrentService__process_torrent(
    make_torrent_item("movie"), torrent_bytes()
)
assert single_file.file_index == 1 and single_file.direct_file_index == 0

series_media = Series("tt100:2:3", ["Show"], "S02", "E03", ["en"])
series_service = TorrentService(series_media)
series_files = [
    (120 * 1024 ** 2, ["Show", "Show.S02E04.mkv"]),
    (130 * 1024 ** 2, ["Show", "Show.S02E03.mkv"]),
]
episode_torrent = series_service._TorrentService__process_torrent(
    make_torrent_item("series"), torrent_bytes(series_files)
)
assert episode_torrent.file_index == 2 and episode_torrent.direct_file_index == 1
assert episode_torrent.file_name == "Show.S02E03.mkv"
missing_episode = series_service._TorrentService__process_torrent(
    make_torrent_item("series"),
    torrent_bytes([(200 * 1024 ** 2, ["Show", "Show.S02E04.mkv"])])
)
assert missing_episode.file_index is None
assert missing_episode.direct_file_missing is True
direct_config = {
    "maxResults": "1", "addonHost": "https://example.invalid",
    "debrid": False, "torrenting": True,
}
assert parse_to_stremio_streams([missing_episode], direct_config, series_media) == []
direct_streams = parse_to_stremio_streams([episode_torrent], direct_config, series_media)
assert len(direct_streams) == 1 and direct_streams[0]["fileIdx"] == 1
single_streams = parse_to_stremio_streams(
    [single_file], direct_config, Movie("tt100", ["Movie"], "2026", ["en"])
)
assert len(single_streams) == 1 and single_streams[0]["fileIdx"] == 0
debrid_query = episode_torrent.to_debrid_stream_query(series_media)
assert debrid_query["file_index"] == 2  # Debrid ID is not Stremio fileIdx.

# HTTP redirect handling: relative URLs, unsafe protocols and bounded bodies.
class StreamResponse:
    def __init__(self, status, data=b"", content_type="text/html", location=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        if location:
            self.headers["Location"] = location
        self.encoding = "utf-8"
        self.payload = data
        self.closed = False
    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.payload), chunk_size):
            yield self.payload[i:i + chunk_size]
    def close(self):
        self.closed = True

class MockSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
    def get(self, url, **kwargs):
        assert kwargs.get("stream") is True
        self.urls.append(url)
        return self.responses.pop(0)

redirect_a = StreamResponse(302, location="../dl/final")
redirect_b = StreamResponse(
    200, b"magnet:?xt=urn:btih:" + b"a" * 40, "text/html"
)
session = MockSession([redirect_a, redirect_b])
resolved = TorrentService()._TorrentService__process_web_url(
    make_torrent_item("movie"), session
)
assert session.urls == ["http://localhost/dl/test", "http://localhost/dl/final"]
assert resolved.info_hash == "a" * 40
assert redirect_a.closed and redirect_b.closed
unsafe = StreamResponse(302, location="file:///etc/passwd")
bad_session = MockSession([unsafe])
TorrentService()._TorrentService__process_web_url(make_torrent_item("movie"), bad_session)
assert len(bad_session.urls) == 1 and unsafe.closed
too_large = StreamResponse(200, b"z" * (16 * 1024 * 1024 + 1))
large_session = MockSession([too_large])
TorrentService()._TorrentService__process_web_url(
    make_torrent_item("movie"), large_session
)
assert too_large.closed and len(large_session.urls) == 1

# Cache reads must not mutate Movie/Series objects or fail the whole search.
cache_media = Movie("tt100", ["Movie"], "2026", ["en"])
original = dict(cache_media.__dict__)
with patch("utils.cache.requests.get", return_value=SimpleNamespace(
    status_code=200, json=lambda: [{"title": "Movie"}]
)) as cache_request:
    assert len(search_cache(cache_media)) == 1
    assert cache_request.call_args.kwargs["timeout"] == (3, 8)
assert cache_media.__dict__ == original
with patch("utils.cache.requests.get", side_effect=ConnectionError("down")):
    assert search_cache(cache_media) == []

# Same host+API key share cached indexers; different credentials do not.
jackett_xml = """
<indexers><indexer id="cached"><title>Cached</title><link>http://invalid</link>
<type>public</type><language>en</language></indexer></indexers>
"""
class IndexerResponse:
    text = jackett_xml
    def raise_for_status(self):
        pass

JackettService._indexer_cache.clear()
s1 = JackettService({"jackettApiKey": "test1", "jackettHost": "http://cache-test"})
s2 = JackettService({"jackettApiKey": "test1", "jackettHost": "http://cache-test"})
s3 = JackettService({"jackettApiKey": "test2", "jackettHost": "http://cache-test"})
with patch("jackett.jackett_service.requests.Session.get", return_value=IndexerResponse()) as network:
    assert s1.get_indexers()[0].id == "cached"
    assert s2.get_indexers()[0].id == "cached"
    assert network.call_count == 1
    assert s3.get_indexers()[0].id == "cached"
    assert network.call_count == 2
JackettService._indexer_cache.clear()

# FastAPI blocking endpoints must run via the framework's threadpool.
from main import get_results, get_playback
assert not inspect.iscoroutinefunction(get_results)
assert not inspect.iscoroutinefunction(get_playback)
with patch("main.parse_config", return_value={"service": "realdebrid"}), \
     patch("main.decodeb64", return_value='{}'), \
     patch("main.get_debrid_service", return_value=SimpleNamespace(
         get_stream_link=lambda query, ip: "https://video.invalid/temporary"
     )):
    playback = get_playback("conf", "encoded", SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
assert playback.status_code == 302
assert playback.headers["cache-control"] == "no-store"
assert playback.headers["location"] == "https://video.invalid/temporary"
print("Phase 3+5 file selection, redirect, cache, languages, timeout and playback tests passed")
