"""Extract a verified Instagram/Facebook mirror pair into an existing collection."""

import argparse
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from pymongo import MongoClient

from .storage import collections, ensure_link_index, save_pair


ROOT = Path(__file__).resolve().parent.parent
MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
MORE = "Ver más"
COMMENT_LIMIT = 31


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def parse_count(text: str) -> dict:
    """Keep the displayed count and its numeric interpretation."""
    display = text.strip().replace("\xa0", " ")
    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+|\d+", display):
        return {"display": display, "value": int(re.sub(r"[,.]", "", display))}
    abbreviated = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(mil|[kKmM])", display, re.IGNORECASE)
    if abbreviated:
        multiplier = 1_000_000 if abbreviated.group(2).lower() == "m" else 1_000
        value = int(Decimal(abbreviated.group(1).replace(",", ".")) * multiplier)
        return {"display": display, "value": value}
    raise ValueError(f"Unrecognized count: {display!r}")


def instagram_metadata(meta: str) -> tuple[str, str, dict]:
    match = re.match(
        r"(?P<likes>[\d,.]+(?:\s*(?:[kKmM]|mil))?) likes?,.*? - "
        r"(?P<account>[\w.]+) on .*?: [\"“](?P<caption>.*)[\"”]\.\s*$",
        meta,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise ValueError("Instagram metadata did not contain a recognizable caption and like count")
    return match.group("account"), match.group("caption"), parse_count(match.group("likes"))


def instagram_url(url: str) -> str:
    parsed = urlparse(url)
    match = re.fullmatch(r"/(?:[^/]+/)?(p|reel)/([A-Za-z0-9_-]+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname not in {"www.instagram.com", "instagram.com"} or not match:
        raise ValueError("Expected an Instagram post or reel URL")
    return f"https://www.instagram.com/p/{match.group(2)}/"


def scroll_nearest_comment_container(page: Page) -> bool:
    return page.locator('a[href*="/c/"]').first.evaluate(
        """e => {
            for (let p = e.parentElement; p; p = p.parentElement) {
                if (p.scrollHeight > p.clientHeight + 20 &&
                    ['auto', 'scroll'].includes(getComputedStyle(p).overflowY)) {
                    p.scrollTop = p.scrollHeight;
                    return true;
                }
            }
            return false;
        }"""
    )


def instagram_comments(page: Page) -> list[str]:
    found = {}
    stalled = 0
    for _ in range(25):
        items = page.locator('a[href*="/c/"]').evaluate_all(
            """links => links.map(a => {
                const block = a.parentElement?.parentElement?.parentElement;
                const lines = (block?.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                return {id: a.getAttribute('href'), text: lines.slice(2).join('\\n')};
            })"""
        )
        before = len(found)
        for item in items:
            if item["id"] and item["text"]:
                found.setdefault(item["id"], item["text"])
        if len(found) >= COMMENT_LIMIT:
            break
        stalled = stalled + 1 if len(found) == before else 0
        if stalled >= 3 or not items or not scroll_nearest_comment_container(page):
            break
        page.wait_for_timeout(1600)
    return list(found.values())[:COMMENT_LIMIT]


def read_instagram(page: Page, url: str, include_comments: bool = True) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.locator('time[datetime]').first.wait_for(timeout=20000)
    page.wait_for_timeout(2500)
    meta = page.locator('meta[property="og:description"]').get_attribute("content")
    if not meta:
        raise ValueError("Instagram did not expose publication metadata")
    account, caption, likes = instagram_metadata(meta)
    post_times = page.locator('time[datetime]').evaluate_all(
        """es => es.filter(e => !e.closest('a[href*="/c/"]')).map(e => e.dateTime)"""
    )
    if not post_times:
        raise ValueError("Instagram publication date not found")
    published = datetime.fromisoformat(post_times[0].replace("Z", "+00:00"))
    return {
        "account": account, "network": "instagram", "url": url,
        "published_at": published, "caption": caption, "likes": likes,
        "comments": instagram_comments(page) if include_comments else [],
    }


def filter_facebook(page: Page, account: str, day: datetime) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", account):
        raise ValueError("Invalid Facebook account identifier")
    page.goto(f"https://www.facebook.com/{account}", wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("button", name="Filtros", exact=True).click(timeout=20000)
    dialog = page.get_by_role("dialog", name="Filtros de publicaciones")
    for index, value in enumerate((str(day.year), MONTHS[day.month - 1], str(day.day))):
        dialog.get_by_role("combobox").nth(index).click()
        page.get_by_role("option", name=value, exact=True).click()
    dialog.get_by_role("button", name="Listo", exact=True).click()
    page.wait_for_timeout(5000)


def facebook_caption(message) -> str:
    text = message.inner_text().strip()
    less = message.get_by_role("button", name="Ver menos", exact=True)
    if less.count() and text.endswith("Ver menos"):
        text = text[:-len("Ver menos")].rstrip()
    return text


def matching_posts(page: Page, caption: str, day: datetime):
    expected_date = f"{day.day} de {MONTHS[day.month - 1]} de {day.year}"
    actions = page.locator('[role="button"][aria-label^="Acciones en esta publicación"]')
    matches = []
    for index in range(actions.count()):
        action = actions.nth(index)
        header = action.locator("xpath=../../..")
        post = action.locator("xpath=../../../../..")
        message = post.locator('[data-ad-preview="message"]').first
        if not message.count() or expected_date not in header.inner_text():
            continue
        more = message.get_by_role("button", name=MORE, exact=True)
        if more.count():
            more.first.click()
            page.wait_for_timeout(500)
        if normalized(facebook_caption(message)) == normalized(caption):
            matches.append(post)
    return matches


def facebook_reactions(page: Page, post) -> dict:
    reaction_buttons = post.locator('[role="button"][aria-label]')
    icon = reaction_buttons.filter(has_text="")  # narrowed below by the reaction label
    icon = icon.locator('xpath=self::*[starts-with(@aria-label,"Me gusta:") or '
                        'starts-with(@aria-label,"Me encanta:") or '
                        'starts-with(@aria-label,"Me importa:") or '
                        'starts-with(@aria-label,"Me divierte:") or '
                        'starts-with(@aria-label,"Me asombra:") or '
                        'starts-with(@aria-label,"Me entristece:") or '
                        'starts-with(@aria-label,"Me enoja:")]')
    if not icon.count():
        return {"total": None, "types": {}, "warning": "Reaction breakdown control not found"}
    icon.first.click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_role("tab").first.wait_for(timeout=10000)
    more = dialog.get_by_role("tab", name="Más", exact=True)
    if more.count():
        more.click()
    entries = dialog.locator('[role="tab"], [role="menuitemradio"]').evaluate_all(
        """es => es.map(e => ({label: e.getAttribute('aria-label'), text: e.innerText.trim()}))"""
    )
    types = {}
    total = None
    for entry in entries:
        label = entry["label"] or ""
        match = re.search(r"Mostrar ([\d.,]+)\s+personas que reaccionaron con (.+)$", label)
        if not match:
            continue
        name = match.group(2)
        if name == "Todas":
            total = parse_count(match.group(1))
        else:
            types[name] = parse_count(entry["text"])
    dialog.get_by_role("button", name="Cerrar", exact=True).click()
    return {"total": total, "types": types}


def facebook_comments(dialog) -> list[str]:
    result = []
    seen = set()
    stalled = 0
    for _ in range(25):
        comments = dialog.get_by_role("button", name="Responder", exact=True)
        for index in range(comments.count()):
            block = comments.nth(index).locator("xpath=../../../../../..")
            more = block.get_by_role("button", name=MORE, exact=True)
            if more.count():
                more.first.click()
            item = block.evaluate(
                """e => {
                    const lines = e.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
                    const reply = lines.indexOf('Responder');
                    const link = e.querySelector('a[href*="comment_id="]');
                    return {text: lines.slice(3, reply).join('\\n'),
                            id: link ? new URL(link.href).searchParams.get('comment_id') : null};
                }"""
            )
            if item["id"] and item["text"] and item["id"] not in seen:
                seen.add(item["id"])
                result.append(item["text"])
                if len(result) == COMMENT_LIMIT:
                    return result
        before = len(result)
        moved = dialog.evaluate(
            """e => {
                const s = [...e.querySelectorAll('*')].find(x =>
                    x.scrollHeight > x.clientHeight + 30 &&
                    ['auto', 'scroll'].includes(getComputedStyle(x).overflowY));
                if (!s) return false;
                s.scrollTop = s.scrollHeight;
                return true;
            }"""
        )
        if not moved:
            break
        dialog.page.wait_for_timeout(1600)
        stalled = stalled + 1 if len(result) == before and comments.count() <= len(result) else 0
        if stalled >= 3:
            break
    return result


def read_facebook(page: Page, post, account: str, day: datetime) -> dict:
    reactions = facebook_reactions(page, post)
    opener = post.get_by_role("button", name="Ver más comentarios", exact=True)
    if not opener.count():
        raise ValueError("Cannot safely open the publication's comments")
    opener.first.click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_text("Responder", exact=True).first.wait_for(timeout=15000)
    url = page.url.split("?")[0]
    caption = facebook_caption(dialog.locator('[data-ad-preview="message"]').first)
    comments = facebook_comments(dialog)
    return {
        "account": account, "network": "facebook", "url": url,
        "published_date_display": next(
            (line.strip() for line in dialog.inner_text().splitlines()
             if f"{day.day} de {MONTHS[day.month - 1]} de {day.year}" in line), None
        ),
        "caption": caption, "reactions": reactions, "comments": comments,
        "comment_order": "displayed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instagram-url", required=True)
    parser.add_argument("--facebook-account", required=True)
    parser.add_argument("--write", action="store_true", help="Upsert the verified pair to MongoDB")
    parser.add_argument("--headed", action="store_true", help="Show the browser")
    args = parser.parse_args()
    url = instagram_url(args.instagram_url)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(ROOT / ".browser-profile"), channel="msedge", headless=not args.headed,
        )
        try:
            ig = read_instagram(context.new_page(), url)
            day = ig["published_at"].astimezone(ZoneInfo("America/Costa_Rica"))
            fb_page = context.new_page()
            filter_facebook(fb_page, args.facebook_account, day)
            posts = matching_posts(fb_page, ig["caption"], day)
            if len(posts) != 1:
                print(f"No unique exact-caption match among loaded Facebook posts ({len(posts)} matches).", file=sys.stderr)
                return 2
            fb = read_facebook(fb_page, posts[0], args.facebook_account, day)
            if normalized(fb["caption"]) != normalized(ig["caption"]) or not fb["published_date_display"]:
                raise ValueError("Facebook caption or date could not be verified")
            pair = {
                "_id": url, "match_method": "same local date and exact normalized caption",
                "instagram": ig, "facebook": fb, "scraped_at": datetime.now(timezone.utc),
            }
            if args.write:
                load_dotenv(ROOT / ".env")
                uri = os.environ.get("MONGODB_URI")
                if not uri:
                    raise ValueError("MONGODB_URI is not configured in .env")
                with MongoClient(uri) as client:
                    db = client["SocialMediaDB"]
                    if "SocialMediaDB" not in db.list_collection_names():
                        raise ValueError("The existing SocialMediaDB collection was not found")
                    _, links = collections(client)
                    ensure_link_index(links)
                    save_pair(client, pair)
                print("Verified pair saved to SocialMediaDB.SocialMediaDB.")
            else:
                print("Verified pair found; use --write to save it.")
            print(f"Comments: Instagram {len(ig['comments'])}, Facebook {len(fb['comments'])}; limit {COMMENT_LIMIT}.")
            print(f"Reaction types found: {', '.join(fb['reactions']['types']) or 'none'}.")
            if len(ig["comments"]) < COMMENT_LIMIT or len(fb["comments"]) < COMMENT_LIMIT:
                print("Warning: fewer than 31 comments were retrieved on at least one network.", file=sys.stderr)
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, PlaywrightTimeoutError) as error:
        print(f"Extraction failed: {error}", file=sys.stderr)
        raise SystemExit(1)
