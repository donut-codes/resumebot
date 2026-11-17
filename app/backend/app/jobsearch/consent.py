"""Cookie and consent banners are never a block (the owner, 2026-11-04).

Two pure helpers, used wherever the app's code reads a page for the words of a bot check, a sign-in wall or a refusal,
or clicks on one:

- `without_banners(html)`: the page with its cookie and consent banners taken out. A consent manager lists "reCAPTCHA",
  "security" and "bot management" among the cookies it explains, so those words in a banner are not a bot check, a
  sign-in wall or a refusal. What a
  real CAPTCHA, sign-in wall or refusal says is not in a banner, so it still stops. Nothing but the banner is dropped:
  never the page itself, and never an element that holds the page's own content. A banner is found by what it calls
  itself (the words of `_WORD_PARTS`, `_VENDOR_WORDS` and `_VENDOR_PHRASES`), so every limit errs toward the old
  reading, a possible false stop, never toward missing a real one: a banner named by none of them (a generic
  "privacy-notice", which a page may carry itself) stays in the page, and a banner a consent manager draws in a shadow
  root or an iframe is not in the page's HTML at all (nor can the clicking see it).
- `is_decline(label)`: whether a banner's control refuses the cookies that are not strictly needed ("Reject all",
  "Decline", "Necessary only", "Only essential"). That is the only kind of control the app's code ever clicks
  (`browser/recipes/interact.decline_consent`); it never accepts, and it clicks nothing else in a banner."""

import re
from html.parser import HTMLParser

#: The most characters of a page read; the callers read no more than this of a page themselves.
HTML_LIMIT = 400_000

#: What a banner's id, class or label says, matched against each word of it ("cookieBanner" is "cookie banner"; a word
#: that only contains "cookie" or "consent", such as "cookiebot" or "cookienotice", counts), and the words of the
#: consent managers that name their banners after themselves.
_WORD_PARTS = ("cookie", "consent")
_VENDOR_WORDS = frozenset(
    {"gdpr", "ccpa", "onetrust", "didomi", "usercentrics", "trustarc", "osano", "iubenda", "cybot", "termly", "truste"})
#: The names of consent managers whose banners carry no cookie or consent word and no one word of their own: Osano's
#: `cc-window`, Quantcast's `qc-cmp2-container`, Sourcepoint's `sp_message_container` (matched in the name with its
#: punctuation made into spaces, as `is_banner` makes it).
_VENDOR_PHRASES = re.compile(r"\bcc window\b|\bqc cmp\d*\b|\bsp message\b")
#: A page with none of these anywhere (scripts and styles included) has no banner to take out, so it is not parsed.
_HINT = re.compile(
    r"cookie|consent|gdpr|ccpa|onetrust|didomi|usercentrics|trustarc|osano|iubenda|cybot|termly|truste|"
    r"cc[-_ ]window|qc[-_ ]cmp|sp[-_ ]message", re.I)
#: What a heading says when it is a banner's title and not the page's: "We value your privacy", "Cookie settings".
_BANNER_TITLE = re.compile(r"cookie|consent|privacy|tracking|gdpr|ccpa", re.I)
#: Elements that are the page, never a banner on it, whatever class they carry ("cookie-banner-open" sits on a `body`).
_NEVER = frozenset({"html", "head", "body", "main", "article", "title", "script", "style"})
#: What makes an element a banner's container and not the page's: inside a banner these are the page's own content,
#: except an `h1` that is the banner's title (`_BANNER_TITLE`).
_OWN_CONTENT = frozenset({"main", "article", "h1"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track",
                   "wbr", "param"})


def is_banner(tag: str, attrs: dict[str, str]) -> bool:
    """Whether an element (its tag and attributes) is a cookie or consent banner's container, by what it calls
    itself."""
    if tag in _NEVER:
        return False
    named = " ".join(attrs.get(k) or "" for k in ("id", "class", "aria-label"))
    named = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", named)  # "cookieBanner" is "cookie banner"
    words = [word for word in re.split(r"[^a-z0-9]+", named.lower()) if word]
    if any(word in _VENDOR_WORDS or any(part in word for part in _WORD_PARTS) for word in words):
        return True
    return _VENDOR_PHRASES.search(" ".join(words)) is not None


class _Spans(HTMLParser):
    """Where each banner's element starts and ends in the page: (start, end) character offsets of the whole element."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=False)
        self.page = page
        self.lines = [0]
        for i, c in enumerate(page):
            if c == "\n":
                self.lines.append(i + 1)
        #: Open elements: [tag, start, is a banner, how many pieces of the page's own content it holds].
        self.stack: list[list] = []
        #: The `h1`s open: [their element, the elements their start counted as holding the page, the text so far].
        self.heads: list[list] = []
        self.spans: list[tuple[int, int]] = []

    def _here(self) -> int:
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        if tag in _VOID:
            return
        given = {k.lower(): v or "" for k, v in attrs}
        banner = is_banner(tag, given)
        entry: list = [tag, self._here(), banner, 0]
        if tag in _OWN_CONTENT and not banner:
            holders = list(self.stack)
            for open_ in holders:
                open_[3] += 1
            if tag == "h1":  # counted at once, taken back at its end when it proves to be a banner's title
                self.heads.append([entry, holders, []])
        self.stack.append(entry)

    def handle_data(self, data) -> None:
        for head in self.heads:
            head[2].append(data)

    def handle_startendtag(self, tag, attrs) -> None:
        pass  # `<div/>` holds nothing

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag in _VOID or all(open_[0] != tag for open_ in self.stack):
            return
        start = self._here()
        close = self.page.find(">", start)
        end = len(self.page) if close < 0 else close + 1
        while self.stack:
            entry = self.stack.pop()
            name, begins, banner, _ = entry
            if name == tag:
                for head in [h for h in self.heads if h[0] is entry]:  # an `h1` about cookies is the banner's title
                    self.heads.remove(head)
                    if _BANNER_TITLE.search(" ".join(head[2])):
                        for open_ in head[1]:
                            open_[3] -= 1
                if banner and entry[3] <= 0:
                    self.spans.append((begins, end))
                return


def without_banners(html: str | None, limit: int = HTML_LIMIT) -> str:
    """The page (at most `limit` characters of it) with its cookie and consent banners taken out; the page as it is
    when it has none. A banner that is never closed, or that holds the page's own content (a `main`, an `article`, an
    `h1` that is not about cookies or privacy), is left in: a real check is never missed for want of a closing tag."""
    page = (html or "")[:limit]
    if not _HINT.search(page):
        return page
    parser = _Spans(page)
    try:
        parser.feed(page)
        parser.close()
    except Exception:  # markup the parser gives up on: the page as it is
        return page
    if not parser.spans:
        return page
    out: list[str] = []
    at = 0
    for start, end in sorted(parser.spans):
        if start < at:  # inside a banner already taken out
            at = max(at, end)
            continue
        out.append(page[at:start])
        out.append("\n")
        at = end
    out.append(page[at:])
    return "".join(out)


#: What a banner's control says when it refuses the cookies that are not strictly needed, once lowercased with its
#: punctuation made into spaces: "Reject all", "Decline cookies", "Deny", "Necessary only", "Only essential cookies",
#: "Use necessary cookies only", "No thanks", "Continue without accepting". "Accept all", "Allow all", "OK" and
#: "Save preferences" are not (a saved choice is not known to refuse anything), and neither is a bare "Necessary",
#: "Essential" or "Strictly necessary cookies": in a preferences panel that is a category's tab, which only opens it,
#: and clicking it would end the look before the real Reject.
_DECLINE = re.compile(
    r"^(?:"
    r"(?:reject|decline|deny|refuse|disagree|disallow|opt out)(?: (?:of|from))?"
    r"(?: (?:all|the|any|non ?essential|optional|additional|unnecessary|non ?required|tracking|marketing|"
    r"advertising|analytics))*(?: (?:cookies|cookie|tracking|trackers|and close|and continue))?"
    r"|no thanks?"
    r"|continue without (?:accepting|agreeing|consenting)"
    r"|i (?:decline|do not accept|don t accept|disagree)"
    r"|(?:(?:accept|allow|use|continue with|only allow|only accept|only use|save|confirm) (?:(?:only|just) )?|"
    r"(?:only|just) )(?:the )?(?:strictly )?(?:necessary|essential|required)(?: (?:cookies|cookie))?(?: (?:only|just))?"
    r"|(?:the )?(?:strictly )?(?:necessary|essential|required)(?: (?:cookies|cookie))? (?:only|just)"
    r")$")


def is_decline(label: str | None) -> bool:
    """Whether a banner control's label refuses non-essential cookies (`_DECLINE`); never one that accepts them."""
    plain = " ".join(re.sub(r"[^a-z0-9]+", " ", (label or "").lower().replace("’", "'")).split())
    return bool(plain) and len(plain) <= 60 and _DECLINE.match(plain) is not None
