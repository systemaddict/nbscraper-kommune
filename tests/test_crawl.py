from __future__ import annotations

from nbkommune import db
from nbkommune.crawl import discover_target
from nbkommune.records import ListedArticle
from nbkommune.settings import Settings
from nbkommune.targets import Target


class CountingConnection(db.SQLiteConnection):
    def __init__(self) -> None:
        super().__init__(":memory:")
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
        super().commit()


class ManyArticlesSource:
    channel = "listing"
    detail = "configured-json: https://testby.dk/api/news"
    resolved_config = {"listing_urls": ["https://testby.dk/nyheder"]}

    def list_articles(self) -> list[ListedArticle]:
        return [
            ListedArticle(
                url=f"https://testby.dk/nyheder/artikel-{number}",
                title=f"Artikel {number}",
                published_at="2026-08-20T10:00:00+00:00",
            )
            for number in range(45)
        ]


def test_large_discovery_commits_article_writes_in_batches(monkeypatch):
    conn = CountingConnection()
    db.init_schema(conn)
    baseline = conn.commits
    target = Target(
        key="testby",
        name="Testby",
        site_url="https://testby.dk",
        news_url="https://testby.dk/nyheder",
        channel="listing",
    )
    settings = Settings(
        _env_file=None,
        database_url="file::memory:",
        min_published_date="2026-01-01",
        discover_enqueue_cap=50,
    )
    monkeypatch.setattr(
        "nbkommune.crawl.make_source",
        lambda target, http, resolved=None: ManyArticlesSource(),
    )

    stats = discover_target(conn, target, None, settings)

    assert stats.seen == 45
    assert stats.new == 45
    assert stats.queued == 45
    # municipality + crawl-run + two full article batches + final partial batch
    # + queue/finalisation commits. The exact total is less important than
    # proving the 45 rows were not held in one transaction.
    assert conn.commits - baseline >= 7
    conn.close()
