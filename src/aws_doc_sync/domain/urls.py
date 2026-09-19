"""URL handling for AWS documentation pages.

Isolated from the fetchers because both change detection (matching RSS entries to
registered sources) and identity (``source_id_for``) depend on agreeing about what
"the same page" means.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

AWS_DOCS_HOST = "docs.aws.amazon.com"


def canonical_page_url(url: str) -> str:
    """Return a comparable form of an AWS docs URL.

    Drops the fragment and query, collapses a ``.md`` suffix back to ``.html``, and
    lowercases the scheme/host. The path case is preserved: AWS guide paths are
    case sensitive (``/IAM/latest/UserGuide/`` vs ``/sagemaker/latest/dg/``).
    """
    parts = urlsplit(url.strip())
    path = parts.path
    if path.endswith(".md"):
        path = path[: -len(".md")] + ".html"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def markdown_url_for(url: str) -> str | None:
    """Best-effort ``.md`` sibling of an AWS docs page URL.

    Returns ``None`` when the URL is not a page-shaped AWS docs URL, so that the
    caller does not issue a request that cannot possibly succeed. The result is a
    *candidate* only -- ``.html`` -> ``.md`` does not hold for every page, so the
    response must still be validated.
    """
    parts = urlsplit(url.strip())
    if parts.netloc.lower() != AWS_DOCS_HOST:
        return None
    path = parts.path
    if path.endswith(".md"):
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    if not path.endswith(".html"):
        return None
    md_path = path[: -len(".html")] + ".md"
    return urlunsplit((parts.scheme, parts.netloc, md_path, "", ""))


def html_url_for(url: str) -> str:
    """The ``.html`` form of an AWS docs page URL."""
    return canonical_page_url(url)


def guide_root(url: str) -> str:
    """Directory portion of a docs URL, e.g. ``.../sagemaker/latest/dg/``.

    Used to attribute an RSS feed to the sources it can possibly cover.
    """
    parts = urlsplit(canonical_page_url(url))
    path = parts.path.rsplit("/", 1)[0] + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
