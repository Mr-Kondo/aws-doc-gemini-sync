from aws_doc_sync.domain.models import source_id_for
from aws_doc_sync.domain.urls import (
    canonical_page_url,
    guide_root,
    html_url_for,
    markdown_url_for,
)

BASE = "https://docs.aws.amazon.com/sagemaker/latest/dg/how-it-works-training"


def test_markdown_url_is_the_md_sibling():
    assert markdown_url_for(f"{BASE}.html") == f"{BASE}.md"


def test_markdown_url_is_none_for_non_page_urls():
    # Nothing to rewrite: issuing a request for these could only waste time.
    assert markdown_url_for("https://docs.aws.amazon.com/sagemaker/latest/dg/") is None
    assert markdown_url_for("https://example.com/page.html") is None


def test_canonical_form_ignores_fragment_query_and_md_suffix():
    assert canonical_page_url(f"{BASE}.md#section") == f"{BASE}.html"
    assert canonical_page_url(f"{BASE}.html?x=1") == f"{BASE}.html"
    assert html_url_for(f"{BASE}.md") == f"{BASE}.html"


def test_guide_path_case_is_preserved():
    # AWS guide paths are case sensitive: /IAM/latest/UserGuide/ is not /iam/...
    url = "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles.html"
    assert canonical_page_url(url) == url


def test_guide_root_is_the_directory():
    assert guide_root(f"{BASE}.html") == "https://docs.aws.amazon.com/sagemaker/latest/dg/"


def test_source_id_is_stable_across_url_spellings():
    assert source_id_for(f"{BASE}.html") == source_id_for(f"{BASE}.md#anchor")


def test_source_id_distinguishes_same_basename_in_different_guides():
    a = source_id_for("https://docs.aws.amazon.com/a/latest/dg/security-iam.html")
    b = source_id_for("https://docs.aws.amazon.com/b/latest/dg/security-iam.html")
    assert a != b
