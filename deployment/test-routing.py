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

