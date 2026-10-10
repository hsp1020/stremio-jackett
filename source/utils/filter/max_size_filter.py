from utils.filter.base_filter import BaseFilter
from utils.logger import setup_logger

logger = setup_logger(__name__)


class MaxSizeFilter(BaseFilter):
    def __init__(self, config, additional_config=None):
        super().__init__(config, additional_config)

    def filter(self, data):
        # The configuration uses GiB while Torznab reports file sizes in bytes.
        max_bytes = float(self.config['maxSize']) * (1024 ** 3)
        filtered_data = []
        for torrent in data:
            try:
                size = int(torrent.size or 0)
            except (TypeError, ValueError):
                size = 0  # Unknown size: do not silently discard the result.
            if size <= max_bytes:
                filtered_data.append(torrent)
        return filtered_data

    def can_filter(self):
        try:
            return self.item_type == 'movie' and float(self.config['maxSize']) > 0
        except (TypeError, ValueError):
            return False
