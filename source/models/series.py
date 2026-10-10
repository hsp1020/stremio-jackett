from models.media import Media

class Series(Media):
    def __init__(self, id, titles, season, episode, languages, year=None, total_episodes=None):
        super().__init__(id, titles, languages, "series")
        self.season = season
        self.episode = episode
        self.seasonfile = None
        self.year = year
        self.total_episodes = total_episodes
