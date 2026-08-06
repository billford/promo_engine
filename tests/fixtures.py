"""Shared fixture data for the test suite."""

_ARTICLE_SENTENCE = (
    "A full-length test article body that comfortably exceeds the stub threshold "
    "so the catalog treats it as a promotable article rather than a Medium reply. "
)

# Long enough that classify_content_kind() treats fixtures as articles, not replies.
ARTICLE_BODY = _ARTICLE_SENTENCE * 6
