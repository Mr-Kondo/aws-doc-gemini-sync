"""HTML -> Markdown conversion for the AWS docs fallback path.

Only reached when a page has no Markdown rendition. It is written by hand rather
than delegated to a generic converter because the two things that must survive --
code blocks and tables -- are exactly what generic converters mangle, and because
AWS wraps its code listings in copy-to-clipboard UI that has to be removed
*before* the text is read.

Structure observed on docs.aws.amazon.com and handled explicitly:

* ``#main-col-body`` is the content root; everything else on the page is chrome.
* ``<pre class="programlisting">`` holds a ``<div class="code-btn-container">``
  (the copy button) followed by ``<code class="json ">`` -- the class carries the
  language, and nested ``<code class="userinput"><span>`` wrappers must flatten.
* Admonitions are ``<div class="awsdocs-note">`` with an ``<h6>`` label; rendering
  that label as a level-6 heading would be wrong, so it becomes bold text -- the
  same shape AWS's own Markdown rendition uses.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..domain.errors import NormalizationError

CONTENT_SELECTORS = ("#main-col-body", "#main-content", "main", "article", "body")

#: Tags that carry no document content.
DROP_TAGS = (
    "script", "style", "noscript", "nav", "header", "footer", "form",
    "button", "iframe", "svg", "template", "link", "meta",
)

#: Chrome containers, by CSS selector.
DROP_SELECTORS = (
    "#breadcrumbs", ".breadcrumb", "#page-toc-src", "#awsdocs-filter-selector",
    ".awsdocs-page-header-container", ".code-btn-container", ".btn-copy-code",
    ".awsdocs-copyright", ".awsdocs-language-banner", ".feedback-section",
    "#quick-feedback-yes", "#quick-feedback-no", ".awsui-util-hide",
    ".page-next-section", ".awsdocs-page-utilities", "#next-topic-link",
    "#previous-topic-link", ".icon-external-link", ".awsdocs-print-only",
)

#: AWS ships Angular-style custom elements. Most are client-rendered chrome and
#: arrive empty, but ``<awsdocs-tabs>`` really does wrap content -- the JSON/CLI
#: example tabs on IAM pages live inside it. So the rule is by emptiness, not by
#: name: empty custom elements are dropped, non-empty ones are unwrapped. That
#: survives AWS adding new widgets without silently deleting documentation.
CUSTOM_TAG_PREFIXES = ("awsdocs-", "awsui-")

BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "ul", "ol", "li", "table", "pre", "blockquote",
        "dl", "dt", "dd", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "figure", "figcaption", "details", "summary", "address",
    }
)

ADMONITION_CLASSES = {
    "awsdocs-note": "Note",
    "awsdocs-tip": "Tip",
    "awsdocs-important": "Important",
    "awsdocs-warning": "Warning",
    "awsdocs-caution": "Caution",
    "note": "Note",
    "tip": "Tip",
    "important": "Important",
    "warning": "Warning",
    "caution": "Caution",
}

_WS_RE = re.compile(r"[ \t\n\r\f\v]+")


def html_to_markdown(html: str, *, base_url: str = "") -> str:
    """Convert an AWS documentation HTML page to Markdown."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # pragma: no cover - lxml import/parse failure
        raise NormalizationError(f"could not parse HTML: {exc}") from exc

    root = _select_root(soup)
    if root is None:
        raise NormalizationError("no content root found in HTML document")

    _strip_chrome(root)
    blocks = _Converter(base_url).blocks(root)
    return "\n\n".join(b for b in blocks if b.strip())


def _select_root(soup: BeautifulSoup) -> Tag | None:
    for selector in CONTENT_SELECTORS:
        found = soup.select_one(selector)
        if found is not None:
            return found
    return soup


def _strip_chrome(root: Tag) -> None:
    """Remove non-content elements in place."""
    for tag_name in DROP_TAGS:
        for node in root.find_all(tag_name):
            node.decompose()

    for selector in DROP_SELECTORS:
        try:
            for node in root.select(selector):
                node.decompose()
        except Exception:  # pragma: no cover - malformed selector guard
            continue

    for node in list(root.find_all(True)):
        name = (node.name or "").lower()
        if not name.startswith(CUSTOM_TAG_PREFIXES):
            continue
        if node.get_text(strip=True):
            node.unwrap()
        else:
            node.decompose()

    for node in root.select('[aria-hidden="true"]'):
        node.decompose()

    # AWS leaves renderer breadcrumbs such as ``<!--DEBUG: cli (json)-->`` in the
    # markup; they would otherwise land inside code blocks.
    for comment in root.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()


class _Converter:
    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url

    # -- blocks ------------------------------------------------------------------

    def blocks(self, node: Tag) -> list[str]:
        """Convert ``node``'s children into a list of Markdown blocks."""
        out: list[str] = []
        pending: list[str] = []

        def flush() -> None:
            text = _squeeze("".join(pending)).strip()
            pending.clear()
            if text:
                out.append(text)

        for child in node.children:
            if isinstance(child, NavigableString):
                pending.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            if child.name in BLOCK_TAGS or child.name == "table":
                flush()
                out.extend(self.block_tag(child))
            else:
                pending.append(self.inline(child))

        flush()
        return out

    def block_tag(self, node: Tag) -> list[str]:
        name = (node.name or "").lower()

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _squeeze(self.inline_children(node)).strip()
            return [f"{'#' * int(name[1])} {text}"] if text else []

        if name == "pre":
            return [self.code_block(node)]

        if name == "hr":
            return ["---"]

        if name in ("ul", "ol"):
            return [self.list_block(node, ordered=(name == "ol"))]

        if name == "table":
            return [self.table_block(node)]

        if name == "blockquote":
            inner = self.blocks(node)
            quoted = "\n".join(
                f"> {line}" if line.strip() else ">"
                for block in inner
                for line in block.split("\n")
            )
            return [quoted] if quoted.strip() else []

        if name == "dl":
            return self.definition_list(node)

        if name in ("dt", "dd"):  # only reached if orphaned
            text = _squeeze(self.inline_children(node)).strip()
            return [text] if text else []

        if name == "p":
            text = _squeeze(self.inline_children(node)).strip()
            return [text] if text else []

        admonition = self.admonition_label(node)
        if admonition:
            body = self.blocks(node)
            body = [b for b in body if b.strip() and b.strip("#* ").lower() != admonition.lower()]
            return [f"**{admonition}**", *body]

        # Generic containers (div/section/li wrappers) are transparent.
        return self.blocks(node)

    def admonition_label(self, node: Tag) -> str | None:
        classes = {c.lower() for c in (node.get("class") or [])}
        for css_class, label in ADMONITION_CLASSES.items():
            if css_class in classes:
                return label
        return None

    def code_block(self, node: Tag) -> str:
        """Render ``<pre>`` as a fenced block, preserving content byte for byte."""
        language = ""
        code = node.find("code")
        if isinstance(code, Tag):
            for css_class in code.get("class") or []:
                candidate = css_class.strip().lower()
                if candidate and candidate not in ("userinput", "code", "programlisting"):
                    language = candidate
                    break

        text = node.get_text()
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")

        # Use a fence long enough that backticks inside the sample cannot close it.
        longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}{language}\n{text}\n{fence}"

    def list_block(self, node: Tag, *, ordered: bool, depth: int = 0) -> str:
        lines: list[str] = []
        try:
            index = int(str(node.get("start", "1")))
        except ValueError:
            index = 1

        for item in node.find_all("li", recursive=False):
            marker = f"{index}. " if ordered else "- "
            index += 1
            indent = "  " * depth
            pad = " " * len(marker)

            item_blocks: list[str] = []
            inline_parts: list[str] = []
            for child in item.children:
                if isinstance(child, NavigableString):
                    inline_parts.append(str(child))
                elif isinstance(child, Tag) and child.name in ("ul", "ol"):
                    text = _squeeze("".join(inline_parts)).strip()
                    inline_parts = []
                    if text:
                        item_blocks.append(text)
                    item_blocks.append(
                        self.list_block(child, ordered=(child.name == "ol"), depth=depth + 1)
                    )
                elif isinstance(child, Tag) and child.name in BLOCK_TAGS:
                    text = _squeeze("".join(inline_parts)).strip()
                    inline_parts = []
                    if text:
                        item_blocks.append(text)
                    item_blocks.extend(self.block_tag(child))
                elif isinstance(child, Tag):
                    inline_parts.append(self.inline(child))

            text = _squeeze("".join(inline_parts)).strip()
            if text:
                item_blocks.append(text)

            if not item_blocks:
                continue

            first, *rest = item_blocks
            first_lines = first.split("\n")
            lines.append(f"{indent}{marker}{first_lines[0]}")
            lines.extend(f"{indent}{pad}{line}" for line in first_lines[1:])
            for block in rest:
                if block.lstrip().startswith(("- ", "1. ")) or re.match(r"^\s*\d+\.\s", block):
                    lines.extend(block.split("\n"))  # nested list keeps its own indent
                else:
                    lines.append("")
                    lines.extend(f"{indent}{pad}{line}" for line in block.split("\n"))

        return "\n".join(lines)

    def definition_list(self, node: Tag) -> list[str]:
        """Render ``<dl>`` as bold terms followed by their bodies.

        ``<dd>`` is converted as *blocks*, not inline text: AWS nests tabbed code
        examples inside definition bodies, and flattening them would turn an IAM
        policy listing into a run-on paragraph.
        """
        out: list[str] = []
        for child in node.find_all(["dt", "dd"], recursive=False):
            if child.name == "dt":
                text = _squeeze(self.inline_children(child)).strip()
                if text:
                    out.append(f"**{text}**")
            else:
                out.extend(block for block in self.blocks(child) if block.strip())
        return out

    def table_block(self, node: Tag) -> str:
        """Render a GFM table.

        Cell content is flattened to a single line: GFM has no way to express a
        multi-line cell, and a broken table is worse than a dense one.
        """
        rows: list[list[str]] = []
        header: list[str] | None = None

        thead = node.find("thead")
        if isinstance(thead, Tag):
            head_rows = [self.table_row(tr) for tr in thead.find_all("tr")]
            head_rows = [r for r in head_rows if r]
            if head_rows:
                header = head_rows[0]
                rows.extend(head_rows[1:])

        body_containers = node.find_all("tbody") or [node]
        for container in body_containers:
            for tr in container.find_all("tr"):
                if isinstance(thead, Tag) and tr.find_parent("thead") is thead:
                    continue
                row = self.table_row(tr)
                if row:
                    rows.append(row)

        if header is None:
            if not rows:
                return ""
            # AWS emits header cells as <th> in the first <tr> without a <thead>.
            first_tr = node.find("tr")
            if isinstance(first_tr, Tag) and first_tr.find("th") is not None:
                header = rows.pop(0)
            else:
                header = [""] * len(rows[0])

        width = max(len(header), *(len(r) for r in rows)) if rows else len(header)
        header = _pad(header, width)
        rows = [_pad(r, width) for r in rows]

        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
        ]
        lines.extend("| " + " | ".join(r) + " |" for r in rows)

        caption = node.find("caption")
        if isinstance(caption, Tag):
            text = _squeeze(self.inline_children(caption)).strip()
            if text:
                return f"**{text}**\n\n" + "\n".join(lines)
        return "\n".join(lines)

    def table_row(self, tr: Tag) -> list[str]:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            return []
        return [self.table_cell(c) for c in cells]

    def table_cell(self, cell: Tag) -> str:
        blocks = self.blocks(cell)
        text = " ".join(b.replace("\n", " ") for b in blocks)
        return _squeeze(text).strip().replace("|", "\\|")

    # -- inline ------------------------------------------------------------------

    def inline_children(self, node: Tag) -> str:
        parts: list[str] = []
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                parts.append(self.inline(child))
        return "".join(parts)

    def inline(self, node: Tag) -> str:
        name = (node.name or "").lower()

        if name == "br":
            return "  \n"
        if name in ("strong", "b"):
            text = _squeeze(self.inline_children(node)).strip()
            return f"**{text}**" if text else ""
        if name in ("em", "i", "var"):
            text = _squeeze(self.inline_children(node)).strip()
            return f"*{text}*" if text else ""
        if name == "code":
            if node.find_parent("pre") is not None:
                return node.get_text()
            text = _squeeze(node.get_text()).strip()
            if not text:
                return ""
            fence = "`" * (max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
            return f"{fence}{text}{fence}"
        if name == "a":
            return self.link(node)
        if name == "img":
            alt = _squeeze(str(node.get("alt", ""))).strip()
            src = self.absolute(str(node.get("src", "")))
            return f"![{alt}]({src})" if src else alt
        if name in ("sup", "sub"):
            return _squeeze(self.inline_children(node))
        if name in BLOCK_TAGS:
            # A block element reached through an inline path: flatten it.
            return " ".join(self.blocks(node))
        return self.inline_children(node)

    def link(self, node: Tag) -> str:
        text = _squeeze(self.inline_children(node)).strip()
        href = str(node.get("href", "")).strip()
        if not href:
            return text
        if href.startswith("#"):
            return text  # intra-page anchors are meaningless once bundled
        url = self.absolute(href)
        if not text:
            return url
        return f"[{text}]({url})"

    def absolute(self, url: str) -> str:
        if not url:
            return ""
        scheme = urlsplit(url).scheme
        if scheme:
            return url
        if not self.base_url:
            return url
        joined = urljoin(self.base_url, url)
        return joined[: -len(".md")] + ".html" if joined.endswith(".md") else joined


def _squeeze(text: str) -> str:
    """Collapse HTML whitespace, but keep the two-space Markdown hard break."""
    text = text.replace("\u00a0", " ")  # NBSP -> plain space
    parts = text.split("  \n")
    return "  \n".join(_WS_RE.sub(" ", part) for part in parts)


def _pad(row: list[str], width: int) -> list[str]:
    return row + [""] * (width - len(row))
