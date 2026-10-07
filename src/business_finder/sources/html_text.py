"""Turn small pieces of HTML (such as listing descriptions) into plain text."""

from html.parser import HTMLParser

# Tags that end a line of text when rendered, so their text should not run
# into the next paragraph or list item.
LINE_BREAKING_TAGS = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class PlainTextCollector(HTMLParser):
    """Collects the visible text of an HTML fragment, keeping line breaks."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in LINE_BREAKING_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in LINE_BREAKING_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)


def html_to_text(html: str) -> str:
    """Return the readable text of an HTML fragment.

    Entities such as `&amp;` are decoded, spaces inside each line are
    collapsed, and blank lines are removed.
    """
    collector = PlainTextCollector()
    collector.feed(html)
    collector.close()

    lines = []
    for line in "".join(collector.text_parts).splitlines():
        cleaned_line = " ".join(line.split())
        if cleaned_line:
            lines.append(cleaned_line)
    return "\n".join(lines)
