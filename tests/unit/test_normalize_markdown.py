import pytest

from aws_doc_sync.domain.errors import NormalizationError
from aws_doc_sync.domain.models import ContentKind
from aws_doc_sync.normalize.aws_docs import AwsDocsNormalizer
from aws_doc_sync.normalize.common import compute_content_hash, extract_title
from tests.conftest import fixture_text, make_raw

PAGE = "https://docs.aws.amazon.com/example/latest/dg/page.html"


@pytest.fixture
def normalized():
    return AwsDocsNormalizer().normalize(
        make_raw(fixture_text("aws_page.md"), kind=ContentKind.MARKDOWN, url=PAGE)
    )


def test_title_comes_from_the_first_heading(normalized):
    assert normalized.title == "Example Page"
    assert normalized.source_url == PAGE


def test_navigation_anchors_are_removed(normalized):
    assert "<a name=" not in normalized.content


def test_empty_bold_rendering_artifacts_are_removed(normalized):
    assert "\n****\n" not in normalized.content


def test_code_block_is_preserved_byte_for_byte(normalized):
    start = normalized.content.index("```")
    end = normalized.content.index("```", start + 3)
    block = normalized.content[start:end]
    assert '"Action": "s3:GetObject"' in block
    # AWS emits trailing tabs inside JSON examples. Whitespace inside a code
    # block is content, and stripping it would corrupt the sample.
    assert '"Version": "2012-10-17",\t' in block


def test_table_rows_and_separator_survive(normalized):
    assert "| Resource | Default quota | Adjustable |" in normalized.content
    assert "| Endpoints per Region | 100 | Yes |" in normalized.content


def test_admonition_hard_break_is_kept(normalized):
    # "**Note**" + two trailing spaces is a hard line break; dropping it would
    # merge the label into the body paragraph.
    assert "**Note**  \nQuotas apply per Region." in normalized.content


def test_relative_links_become_absolute_html_urls(normalized):
    base = "https://docs.aws.amazon.com/example/latest/dg"
    assert f"[internal link]({base}/relative-page.html)" in normalized.content
    # A ".md" target is rewritten to ".html": that is the URL a human can open.
    assert f"[markdown-suffixed link]({base}/other-page.html)" in normalized.content
    assert "](relative-page.html)" not in normalized.content


def test_same_page_anchors_point_back_at_the_source_page(normalized):
    # Once bundled with other pages, a bare "#limits" resolves to nothing.
    assert f"[the anchor]({PAGE}#limits)" in normalized.content


def test_absolute_links_are_left_alone(normalized):
    assert "[absolute link](https://example.com/x)" in normalized.content


def test_leading_and_trailing_blank_lines_are_trimmed(normalized):
    assert not normalized.content.startswith("\n")
    assert normalized.content.endswith("\n")
    assert "\n\n\n" not in normalized.content


def test_normalization_is_deterministic():
    normalizer = AwsDocsNormalizer()
    raw = make_raw(fixture_text("aws_page.md"))
    first = normalizer.normalize(raw)
    second = normalizer.normalize(raw)
    assert first.content_hash == second.content_hash
    assert first.content == second.content


def test_hash_is_sha256_of_the_normalized_content(normalized):
    assert normalized.content_hash == compute_content_hash(normalized.content)
    assert normalized.content_hash.startswith("sha256:")


def test_line_endings_are_normalized():
    crlf = AwsDocsNormalizer().normalize(make_raw("# T\r\n\r\nBody line\r\n"))
    lf = AwsDocsNormalizer().normalize(make_raw("# T\n\nBody line\n"))
    assert crlf.content_hash == lf.content_hash


def test_chrome_lines_are_dropped():
    text = (
        "# T\n\nJavascript is disabled or is unavailable in your browser.\n\n"
        "Real content.\n\nDid this page help you?\n"
    )
    content = AwsDocsNormalizer().normalize(make_raw(text)).content
    assert "Javascript is disabled" not in content
    assert "Did this page help you" not in content
    assert "Real content." in content


def test_title_override_wins():
    raw = make_raw("# Upstream title\n\nBody.\n")
    raw = raw.model_copy(
        update={"source": raw.source.model_copy(update={"title_override": "Operator title"})}
    )
    assert AwsDocsNormalizer().normalize(raw).title == "Operator title"


def test_empty_result_is_an_error_not_an_empty_document():
    # Writing an empty document to Drive would destroy a knowledge source while
    # reporting success.
    with pytest.raises(NormalizationError, match="empty"):
        AwsDocsNormalizer().normalize(make_raw("\n\n   \n"))


def test_extract_title_ignores_headings_inside_code():
    assert extract_title("```\n# Not a title\n```\n\n# Real title\n") == "Real title"
