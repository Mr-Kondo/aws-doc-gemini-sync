import pytest

from aws_doc_sync.domain.models import ContentKind
from aws_doc_sync.normalize.aws_docs import AwsDocsNormalizer
from aws_doc_sync.normalize.html_to_markdown import html_to_markdown
from tests.conftest import fixture_text, make_raw

PAGE = "https://docs.aws.amazon.com/example/latest/dg/page.html"


@pytest.fixture
def content():
    return AwsDocsNormalizer().normalize(
        make_raw(fixture_text("aws_page.html"), kind=ContentKind.HTML, url=PAGE, fetcher="html")
    ).content


def test_main_content_is_extracted_and_chrome_is_dropped(content):
    assert "Intro paragraph" in content
    for chrome in (
        "Footer chrome",
        "Documentation",           # breadcrumbs
        "Javascript is disabled",  # noscript notice
        "Copy",                    # copy-to-clipboard button title
    ):
        assert chrome not in content


def test_headings_become_markdown_headings(content):
    assert content.startswith("# Example Page")
    assert "## First section" in content
    assert "## Service quotas" in content


def test_code_block_is_fenced_with_its_language(content):
    assert "```json" in content
    block = content[content.index("```json") : content.index("```", content.index("```json") + 7)]
    assert '"Version": "2012-10-17",' in block
    assert '"Action": "s3:GetObject"' in block
    # The <span> wrappers AWS puts around braces must flatten, not leak.
    assert "<span>" not in block
    assert "{" in block and "}" in block


def test_copy_button_markup_never_lands_inside_the_code(content):
    assert "btn-copy-code" not in content
    assert "DEBUG:" not in content


def test_table_is_rendered_as_a_github_table(content):
    assert "| Resource | Default quota | Adjustable |" in content
    assert "| --- | --- | --- |" in content
    assert "| Endpoints per Region | 100 | Yes |" in content


def test_pipe_characters_inside_cells_are_escaped(content):
    # An unescaped pipe would silently split one cell into two.
    assert r"Pipe \| characters" in content


def test_admonition_label_becomes_bold_not_a_level_six_heading(content):
    assert "**Note**" in content
    assert "###### Note" not in content
    assert "Quotas apply per Region." in content


def test_nested_lists_keep_their_nesting(content):
    assert "- First bullet" in content
    assert "  - Nested bullet" in content


def test_links_are_absolutized_and_md_targets_rewritten(content):
    base = "https://docs.aws.amazon.com/example/latest/dg"
    assert f"[internal link]({base}/relative-page.html)" in content
    assert f"[markdown-suffixed link]({base}/other-page.html)" in content
    assert "[absolute link](https://example.com/x)" in content


def test_non_empty_custom_elements_are_unwrapped_not_deleted():
    """<awsdocs-tabs> wraps real content; only empty widgets may be dropped.

    Deleting every awsdocs-* element would silently remove the JSON and CLI
    example tabs from IAM pages.
    """
    html = (
        '<div id="main-col-body"><h1>T</h1>'
        "<awsdocs-tabs><p>Tabbed content that matters</p></awsdocs-tabs>"
        "<awsdocs-page-header></awsdocs-page-header></div>"
    )
    out = html_to_markdown(html, base_url=PAGE)
    assert "Tabbed content that matters" in out


def test_definition_bodies_keep_their_block_structure():
    html = (
        '<div id="main-col-body"><h1>T</h1><dl><dt>JSON</dt>'
        '<dd><pre><code class="json ">{"a": 1}</code></pre></dd></dl></div>'
    )
    out = html_to_markdown(html, base_url=PAGE)
    assert "**JSON**" in out
    assert "```json" in out
    assert '{"a": 1}' in out


def test_conversion_is_deterministic():
    html = fixture_text("aws_page.html")
    assert html_to_markdown(html, base_url=PAGE) == html_to_markdown(html, base_url=PAGE)


def test_html_and_markdown_paths_agree_on_the_substance():
    """The two acquisition paths must not disagree about what the page says."""
    normalizer = AwsDocsNormalizer()
    from_md = normalizer.normalize(make_raw(fixture_text("aws_page.md"))).content
    from_html = normalizer.normalize(
        make_raw(fixture_text("aws_page.html"), kind=ContentKind.HTML, fetcher="html")
    ).content

    for claim in (
        "Quotas apply per Region.",
        '"Action": "s3:GetObject"',
        "| Endpoints per Region | 100 | Yes |",
        "First bullet",
    ):
        assert claim in from_md, claim
        assert claim in from_html, claim
