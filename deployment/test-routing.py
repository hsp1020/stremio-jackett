from unittest.mock import patch
from types import SimpleNamespace

from jackett.jackett_indexer import JackettIndexer
from jackett.jackett_service import JackettService
from metdata.tmdb import TMDB
from models.movie import Movie
from models.series import Series
from torrent.torrent_smart_container import TorrentSmartContainer


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def mapping(imdb_season=None, tvdb_season=None, imdb_id="tt36034547"):
    return {
        "success": True,
        "data": {
            "mappings": {"imdbId": imdb_id, "themoviedbId": 278043},
            "imdb": {"seasonNumber": imdb_season} if imdb_season is not None else None,
            "tvdb": {"seasonNumber": tvdb_season},
            "tmdb": {"seasonNumber": 1, "fromEpisode": 13},
        },
    }


tmdb = TMDB({"tmdbApi": "test", "languages": ["en"]})
with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(tvdb_season=2))):
    assert tmdb._TMDB__convert_kitsu_to_imdb(["kitsu", "50634", "1"], "series") == [
        "tt36034547", "2", "1"
    ]
    assert tmdb._TMDB__convert_kitsu_to_imdb(["kitsu", "50634"], "movie") == ["tt36034547"]
    with patch.object(tmdb, "_TMDB__get_tmdb_metadata", side_effect=lambda i, parts, kind: (i, parts, kind)):
        result = tmdb.get_metadata("kitsu:50634:5", "series")
        assert result == ("tt36034547:2:5", ["tt36034547", "2", "5"], "series")

with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping(imdb_season=1, tvdb_season=3))):
    assert tmdb._TMDB__convert_kitsu_to_imdb(["kitsu", "50634", "1"], "series") == [
        "tt36034547", "1", "1"
    ]
with patch("metdata.tmdb.requests.get", return_value=FakeResponse(mapping())):
    assert tmdb._TMDB__convert_kitsu_to_imdb(["kitsu", "50634", "1"], "series") is None
with patch("metdata.tmdb.requests.get") as network:
    assert tmdb.get_metadata("trakt:1234", "movie") is None
    network.assert_not_called()

service = JackettService({"jackettApiKey": "test", "jackettHost": "http://localhost"})
indexer = JackettIndexer()
indexer.id = "regular"
indexer.title = "Regular"
indexer.language = "ko"
indexer.movie_search_capatabilities = ["imdbid"]
indexer.tv_search_capatabilities = ["imdbid", "season", "ep"]
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
print("ID mapping, Trakt rejection, IMDb-only fallback and deduplication tests passed")
