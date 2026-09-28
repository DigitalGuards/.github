#!/usr/bin/env python3
"""Posts a GitHub release to a Discord channel as one forwardable message.

The message is the release notes under a heading with the project name and
tag, followed by a link to the release. When the notes contain a block
between <!-- discord --> and <!-- /discord -->, that block is posted in
their place, so a release can carry a community announcement next to its
technical notes. Notes longer than Discord's 2000-character limit are cut
at a paragraph boundary and point to the release page for the rest.

Configuration comes from the environment (see action.yml):
  WEBHOOK_URL   Discord webhook; a trailing /github or /slack is removed
  PROJECT       display name for the heading
  TAG           announce this tag instead of the triggering release
  DRY_RUN       "true" prints the message and posts nothing
  PRERELEASES   "true" also announces prereleases from the release event
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LIMIT = 2000
PROJECT_MAX = 100
SUPPRESS_EMBEDS = 1 << 2
USER_AGENT = "DigitalGuards-release-discord (https://github.com/DigitalGuards/.github, 1)"
FALLBACK_USERNAME = "Release announcement"
MORE = "\n\n*Continued in the release notes.*"
WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
OPEN_MARKER = re.compile(r"<!--\s*discord\s*-->", re.I)
CLOSE_MARKER = re.compile(r"<!--\s*/discord\s*-->", re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK = re.compile(r"(\[[^\]]*\]\([^)\n]*\))")
INLINE_CODE = re.compile(r"(`[^`\n]*`)")
HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")
TOP_HEADING = re.compile(r"^#{1,2}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)
SUB_HEADING = re.compile(r"^#{3,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
CLOSER = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
GITHUB_REF_URL = re.compile(r"(?<![(<\[])https://github\.com/([\w.-]+/[\w.-]+)/(?:pull|issues)/(\d+)(?![\w/#?])")
SHORT_REF = re.compile(r"(?<![\w/&#\[])#(\d+)\b")
RESERVED_USERNAME = re.compile(r"discord|clyde", re.I)


class AnnounceError(Exception):
    pass


def normalize_webhook(url):
    url = url.strip()
    for suffix in ("/github", "/slack"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    if not WEBHOOK.match(url):
        # The URL carries the webhook token, so it is never echoed.
        raise AnnounceError("WEBHOOK_URL is not a Discord webhook URL")
    return url


def select_notes(body):
    body = (body or "").replace("\r\n", "\n")
    # Markers inside code blocks are examples, so they are looked up in a
    # copy with the code blanked out at the same offsets.
    prose = "".join(" " * len(part) if is_code else part for is_code, part in split_fences(body))
    opens = list(OPEN_MARKER.finditer(prose))
    closes = list(CLOSE_MARKER.finditer(prose))
    if not opens and not closes:
        return body
    if len(opens) == 1 and len(closes) == 1 and opens[0].end() <= closes[0].start():
        return body[opens[0].end() : closes[0].start()]
    # A half-written or repeated marker would otherwise post the technical
    # notes the block was meant to keep out of the channel.
    raise AnnounceError("the notes need exactly one <!-- discord --> ... <!-- /discord --> pair")


def _closes(line, fence):
    closer = CLOSER.match(line)
    return bool(closer) and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= len(fence)


def split_fences(markdown):
    """Returns (is_code, text) segments; an unclosed fence runs to the end."""
    segments = []
    current = []
    fence = None
    for line in markdown.splitlines(keepends=True):
        opener = FENCE.match(line)
        if fence is None and opener:
            if current:
                segments.append((False, "".join(current)))
            current = [line]
            fence = opener.group(1)
        elif fence is not None and _closes(line, fence):
            current.append(line)
            segments.append((True, "".join(current)))
            current = []
            fence = None
        else:
            current.append(line)
    if current:
        segments.append((fence is not None, "".join(current)))
    return segments


def _link_refs(text, repo):
    def ref_url(match):
        slug, number = match.group(1), match.group(2)
        label = f"#{number}" if slug.lower() == repo.lower() else f"{slug}#{number}"
        return f"[{label}]({match.group(0)})"

    text = SHORT_REF.sub(lambda m: f"[#{m.group(1)}](https://github.com/{repo}/issues/{m.group(1)})", text)
    return GITHUB_REF_URL.sub(ref_url, text)


def _convert_inline(text, repo):
    text = HTML_TAG.sub("", text)
    # Existing Markdown links keep their label and target as written.
    parts = LINK.split(text)
    return "".join(part if index % 2 else _link_refs(part, repo) for index, part in enumerate(parts))


def _convert_prose(text, repo):
    text = COMMENT.sub("", text)
    text = IMAGE.sub("", text)
    # Discord renders #, ## and ###; the message title is ##, so notes
    # headings sit one level below it.
    text = SUB_HEADING.sub(r"**\1**", text)
    text = TOP_HEADING.sub(r"### \1", text)
    # Inline code spans keep their text, tags and #numbers as written.
    parts = INLINE_CODE.split(text)
    return "".join(part if index % 2 else _convert_inline(part, repo) for index, part in enumerate(parts))


def to_discord(markdown, repo):
    # Fenced code stays verbatim; only the prose between fences is converted.
    text = "".join(part if is_code else _convert_prose(part, repo) for is_code, part in split_fences(markdown))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _open_fence_start(text):
    """Returns the offset of a fence the text leaves open, or None."""
    offset = 0
    fence = None
    start = None
    for line in text.splitlines(keepends=True):
        opener = FENCE.match(line)
        if fence is None and opener:
            fence, start = opener.group(1), offset
        elif fence is not None and _closes(line, fence):
            fence = None
        offset += len(line)
    return start if fence is not None else None


def _drop_open_fence(text):
    start = _open_fence_start(text)
    return text if start is None else text[:start].rstrip()


def _fit(body, budget):
    if len(body) <= budget:
        return body
    room = budget - len(MORE)
    if room <= 1:
        return ""
    kept = ""
    for paragraph in body.split("\n\n"):
        candidate = f"{kept}\n\n{paragraph}" if kept else paragraph
        if len(candidate) > room:
            break
        kept = candidate
    kept = _drop_open_fence(kept)
    if kept:
        return kept + MORE
    # The first paragraph alone is too long: cut it at a line or word break.
    cut = body[: room - 1]
    for separator in ("\n", " "):
        index = cut.rfind(separator)
        if index > room // 2:
            cut = cut[:index]
            break
    return _drop_open_fence(cut).rstrip() + "…" + MORE


def build_message(project, release, repo):
    title = f"## {project[:PROJECT_MAX]} {release['tag_name']}"
    footer = f"Release notes and downloads: {release['html_url']}"
    budget = LIMIT - len(title) - len(footer) - 4
    body = _fit(to_discord(select_notes(release.get("body")), repo), budget - 5)
    open_at = _open_fence_start(body)
    if open_at is not None:
        body += "\n" + FENCE.match(body[open_at:]).group(1)
    return "\n\n".join(part for part in (title, body, footer) if part)[:LIMIT]


def webhook_username(project):
    name = project.strip()[:80]
    if not name or RESERVED_USERNAME.search(name):
        return FALLBACK_USERNAME
    return name


def fetch_release(repo, tag, token):
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    url = f"{api}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise AnnounceError(f"no published release for tag {tag} in {repo} (HTTP {error.code})") from None
    except urllib.error.URLError as error:
        raise AnnounceError(f"GitHub unreachable: {error.reason}") from None


def _retry_after(detail):
    try:
        return min(max(float(json.loads(detail).get("retry_after", 2)), 0.5), 30)
    except (ValueError, AttributeError, TypeError):
        return 2.0


def post(webhook, payload, sleep=time.sleep, opener=urllib.request.urlopen):
    data = json.dumps(payload).encode()
    for attempt in range(3):
        request = urllib.request.Request(
            webhook + "?wait=true",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with opener(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode(errors="replace")
            if attempt < 2 and error.code == 429:
                sleep(_retry_after(detail))
                continue
            # A gateway error means Discord never handled the request, so a
            # retry cannot post the message twice.
            if attempt < 2 and error.code in (502, 503, 504):
                sleep(2)
                continue
            raise AnnounceError(f"Discord answered HTTP {error.code}: {detail}") from None
        except urllib.error.URLError as error:
            raise AnnounceError(f"Discord unreachable: {error.reason}") from None
    raise AnnounceError("Discord kept refusing the webhook call")


def main(env=os.environ, fetch=fetch_release, send=post, out=sys.stdout):
    repo = env.get("GITHUB_REPOSITORY", "")
    project = env.get("PROJECT", "").strip() or repo.split("/")[-1] or "Release"
    tag = env.get("TAG", "").strip()
    dry_run = env.get("DRY_RUN", "").strip().lower() == "true"

    if tag:
        release = fetch(repo, tag, env.get("GH_TOKEN", ""))
    elif env.get("GITHUB_EVENT_NAME") == "release":
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as event:
            release = json.load(event)["release"]
        if release.get("prerelease") and env.get("PRERELEASES", "").strip().lower() != "true":
            print(f"{release['tag_name']} is a prerelease; not announced", file=out)
            return 0
    else:
        raise AnnounceError("set a tag to announce; only release events carry one")

    if release.get("draft"):
        raise AnnounceError(f"{release['tag_name']} is a draft release")

    content = build_message(project, release, repo)
    if dry_run:
        print(f"--- dry run: {len(content)} of {LIMIT} characters ---", file=out)
        print(content, file=out)
        return 0

    if not env.get("WEBHOOK_URL", "").strip():
        raise AnnounceError("the webhook input is empty; set the repository secret DISCORD_RELEASE_WEBHOOK")
    webhook = normalize_webhook(env["WEBHOOK_URL"])
    if env.get("GITHUB_ACTIONS") == "true":
        # GitHub masks the secret as stored; the normalized form differs from
        # it, so it is masked on its own before anything else can print.
        print(f"::add-mask::{webhook}", file=out)
    message = send(
        webhook,
        {
            "content": content,
            "username": webhook_username(project),
            "allowed_mentions": {"parse": []},
            "flags": SUPPRESS_EMBEDS,
        },
    )
    print(f"Announced {release['tag_name']} as Discord message {message.get('id')} ({len(content)} characters)", file=out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AnnounceError as error:
        print(f"::error::{error}")
        sys.exit(1)
