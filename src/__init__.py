"""CBU FX pipeline package.

ETL for Central Bank of Uzbekistan (CBU) currency exchange rates:
fetch from the public CBU JSON API, parse defensively, load into a
PostgreSQL star schema, and export a Parquet serving layer for Power BI.
"""

__version__ = "1.0.0"
