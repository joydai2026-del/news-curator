"""Config validation. A silent misparse is worse than a loud crash."""

from __future__ import annotations

import pytest

from curator.config import ConfigError, load_config, load_topics


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestTopics:
    def test_scalar_keywords_is_a_loud_error(self, tmp_path):
        # Regression: "keywords: AI" used to be iterated into ["A", "I"],
        # silently turning the page into a firehose.
        path = write(tmp_path, "topics.yaml", "topics:\n  - name: X\n    keywords: AI\n")
        with pytest.raises(ConfigError, match="must be a LIST"):
            load_topics(path)

    def test_list_keywords_parse(self, tmp_path):
        path = write(tmp_path, "topics.yaml", "topics:\n  - name: X\n    keywords:\n      - AI\n")
        assert load_topics(path)[0].keywords == ["AI"]

    def test_missing_name(self, tmp_path):
        path = write(tmp_path, "topics.yaml", "topics:\n  - keywords:\n      - AI\n")
        with pytest.raises(ConfigError, match="missing a 'name'"):
            load_topics(path)

    def test_no_keywords_is_rejected(self, tmp_path):
        path = write(tmp_path, "topics.yaml", "topics:\n  - name: X\n    keywords: []\n")
        with pytest.raises(ConfigError, match="no keywords"):
            load_topics(path)

    def test_duplicate_names_rejected(self, tmp_path):
        path = write(
            tmp_path,
            "topics.yaml",
            "topics:\n  - name: X\n    keywords: [AI]\n  - name: x\n    keywords: [ML]\n",
        )
        with pytest.raises(ConfigError, match="duplicate topic"):
            load_topics(path)

    def test_invalid_yaml(self, tmp_path):
        path = write(tmp_path, "topics.yaml", "topics: [unclosed\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_topics(path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="Missing"):
            load_topics(tmp_path / "nope.yaml")

    def test_empty_topics_is_allowed(self, tmp_path):
        assert load_topics(write(tmp_path, "topics.yaml", "topics: []\n")) == []


class TestSources:
    def _topics(self, tmp_path):
        write(tmp_path, "topics.yaml", "topics:\n  - name: X\n    keywords:\n      - AI\n")

    def test_bad_feed_url_rejected(self, tmp_path):
        self._topics(tmp_path)
        write(tmp_path, "sources.yaml", "rss:\n  - {id: a, url: 'javascript:alert(1)'}\n")
        with pytest.raises(ConfigError, match="absolute http"):
            load_config(tmp_path)

    def test_duplicate_source_id_rejected(self, tmp_path):
        self._topics(tmp_path)
        write(
            tmp_path,
            "sources.yaml",
            "rss:\n  - {id: a, url: 'https://a.com/f'}\n  - {id: a, url: 'https://b.com/f'}\n",
        )
        with pytest.raises(ConfigError, match="duplicate rss id"):
            load_config(tmp_path)

    def test_negative_max_age_rejected(self, tmp_path):
        self._topics(tmp_path)
        write(tmp_path, "sources.yaml", "settings:\n  max_age_hours: -5\nrss: []\n")
        with pytest.raises(ConfigError, match="finite positive"):
            load_config(tmp_path)

    def test_valid_config_loads(self, tmp_path):
        self._topics(tmp_path)
        write(
            tmp_path,
            "sources.yaml",
            "settings:\n  max_age_hours: 24\n"
            "rss:\n  - {id: a, name: A, url: 'https://a.com/f', weight: 1.2}\n",
        )
        cfg = load_config(tmp_path)
        assert cfg.max_age_hours == 24
        assert cfg.rss[0].weight == 1.2

    def test_repo_url_must_be_safe(self, tmp_path):
        self._topics(tmp_path)
        write(tmp_path, "sources.yaml", "settings:\n  repo_url: 'javascript:x'\nrss: []\n")
        assert load_config(tmp_path).repo_url is None
