from .base import CandleFeed
from .csv_feed import CsvFeed
from .synthetic import SyntheticFeed

__all__ = ["CandleFeed", "CsvFeed", "SyntheticFeed"]

# PocketOptionFeed is imported lazily by name to avoid a hard dependency on
# the optional binaryoptionstoolsv2 package for users who only backtest.


def get_pocket_option_feed(*args, **kwargs):
    from .pocket_option import PocketOptionFeed

    return PocketOptionFeed(*args, **kwargs)
