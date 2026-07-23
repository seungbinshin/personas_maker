from __future__ import annotations

from datetime import datetime, timedelta

from pipelines.reporter_pipeline import ReporterPipeline


class _Runtime:
    def __init__(self):
        self.request = None

    def run_json(self, request, **_kwargs):
        self.request = request
        return None, {
            "date": "2026-07-23",
            "sections": [{"category": "technology", "articles": []}],
            "rumors": [],
        }


def test_reporter_prompt_and_filter_use_the_configured_one_week_window(tmp_path):
    pipeline = ReporterPipeline.__new__(ReporterPipeline)
    pipeline.search_queries = ["AI hardware news"]
    pipeline.lookback_hours = 168
    pipeline.digests_dir = tmp_path
    pipeline.status_channel = ""
    pipeline.runtime = _Runtime()

    pipeline._gather_and_curate()

    assert "LAST 168 HOURS ONLY" in pipeline.runtime.request.prompt

    now = datetime.now().date()
    digest = {
        "sections": [{"category": "technology", "articles": [
            {"title": "recent", "published_date": str(now - timedelta(days=6))},
            {"title": "old", "published_date": str(now - timedelta(days=8))},
        ]}],
    }
    filtered = pipeline._filter_by_freshness(digest, hours=168)
    assert [article["title"] for article in filtered["sections"][0]["articles"]] == ["recent"]
