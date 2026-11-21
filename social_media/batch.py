"""Discover Instagram posts and process verified mirrors with resumable progress."""

import argparse
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from pymongo import MongoClient

from .scraper import (
    MONTHS, ROOT, filter_facebook, instagram_comments, matching_posts, normalized,
    read_facebook, read_instagram,
)
from .storage import collections, ensure_link_index, save_pair


ACCOUNTS = {
    "laura_fernandez_delgado": ("laurafernandezdelgado2026", date(2026, 2, 3)),
    "presidenciacr": ("CasaPresidencial", date(2026, 5, 10)),
}
LOCAL_TIME = ZoneInfo("America/Costa_Rica")
STATE = ROOT / ".batch-state.sqlite3"


def canonical_profile_link(path: str) -> str:
    match = re.fullmatch(r"/[^/]+/(?:p|reel)/([A-Za-z0-9_-]+)/?", path)
    if not match:
        raise ValueError(f"Unrecognized profile post path: {path}")
    return f"https://www.instagram.com/p/{match.group(1)}/"


def init_state(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS posts (
        account TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        detail TEXT, PRIMARY KEY (account, url)
    )""")
    db.commit()


def set_status(db: sqlite3.Connection, account: str, url: str, status: str, detail: str = "") -> None:
    db.execute("UPDATE posts SET status = ?, detail = ? WHERE account = ? AND url = ?",
               (status, detail, account, url))
    db.commit()


def discover(context, db: sqlite3.Connection, account: str, cutoff: date) -> None:
    profile, probe = context.new_page(), context.new_page()
    try:
        profile.goto(f"https://www.instagram.com/{account}/", wait_until="domcontentloaded", timeout=60000)
        profile.wait_for_timeout(5000)
        seen = set()
        since_probe = 0
        older_samples = 0
        stalled = 0
        last_position = -1
        for _ in range(2000):
            paths = profile.locator('a[href*="/p/"],a[href*="/reel/"]').evaluate_all(
                """es => [...new Set(es.map(e => new URL(e.href).pathname))]"""
            )
            urls = []
            for path in paths:
                try:
                    urls.append(canonical_profile_link(path))
                except ValueError:
                    continue
            new = [url for url in urls if url not in seen]
            seen.update(new)
            if new:
                db.executemany("INSERT OR IGNORE INTO posts (account,url) VALUES (?,?)",
                               [(account, url) for url in new])
                db.commit()
                since_probe += len(new)
                print(f"{account}: {len(seen)} enlaces encontrados", flush=True)
            if new and since_probe >= 12:
                probe.goto(new[-1], wait_until="domcontentloaded", timeout=60000)
                probe.locator("time[datetime]").first.wait_for(timeout=20000)
                timestamp = probe.locator("time[datetime]").first.get_attribute("datetime")
                if not timestamp:
                    raise ValueError(f"No se encontró fecha para {new[-1]}")
                sample_date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(LOCAL_TIME).date()
                print(f"{account}: fecha de muestra {sample_date}", flush=True)
                older_samples = older_samples + 1 if sample_date < cutoff else 0
                since_probe = 0
                if older_samples >= 2:
                    print(f"{account}: dos muestras consecutivas anteriores al límite; se detiene el recorrido.", flush=True)
                    return
            before = profile.evaluate("window.scrollY")
            profile.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            profile.wait_for_timeout(1300)
            after = profile.evaluate("window.scrollY")
            stalled = stalled + 1 if not new and after == before and after == last_position else 0
            last_position = after
            if stalled >= 5:
                if not seen:
                    raise ValueError(f"No se encontraron publicaciones en {account}; revisa la sesión.")
                print(f"{account}: no se cargaron más publicaciones; fin de la cuadrícula no confirmado.", flush=True)
                return
        raise ValueError(f"{account}: se alcanzó el límite de desplazamientos sin confirmar el final")
    finally:
        probe.close()
        profile.close()


def visible_facebook_dates(page) -> list[date]:
    actions = page.locator('[role="button"][aria-label^="Acciones en esta publicación"]')
    dates = []
    for index in range(actions.count()):
        header = actions.nth(index).locator("xpath=../../..").inner_text()
        match = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", header)
        if match and match.group(2).lower() in MONTHS:
            dates.append(date(int(match.group(3)), MONTHS.index(match.group(2).lower()) + 1,
                              int(match.group(1))))
    return dates


def find_facebook_post(page, account: str, caption: str, ig_day: datetime):
    # Facebook's date filter is an upper bound rather than an exact-day filter.
    filter_facebook(page, account, ig_day + timedelta(days=1))
    candidates = (ig_day, ig_day + timedelta(days=1), ig_day - timedelta(days=1))
    earliest = candidates[-1].date()
    previous_position = -1
    stalled = 0
    for _ in range(60):
        for day in candidates:
            posts = matching_posts(page, caption, day)
            if posts:
                return posts, day
        dates = visible_facebook_dates(page)
        if dates and min(dates) < earliest:
            return [], None
        before = page.evaluate("window.scrollY")
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        page.wait_for_timeout(1600)
        after = page.evaluate("window.scrollY")
        stalled = stalled + 1 if after == before and after == previous_position else 0
        previous_position = after
        if stalled >= 5:
            break
    return [], None


def process_post(context, collection, account: str, facebook: str, cutoff: date, url: str) -> tuple[str, str]:
    ig_page, fb_page = context.new_page(), context.new_page()
    try:
        try:
            ig = read_instagram(ig_page, url, include_comments=False)
        except ValueError as error:
            if str(error) == "Instagram metadata did not contain a recognizable caption and like count":
                return "unresolved", "Metadatos sin descripción o likes reconocibles"
            raise
        day = ig["published_at"].astimezone(LOCAL_TIME)
        if day.date() < cutoff:
            return "outside_range", day.date().isoformat()
        posts, facebook_day = find_facebook_post(fb_page, facebook, ig["caption"], day)
        if not posts:
            return "unresolved", "Ninguna coincidencia exacta encontrada en el muro examinado (+/- 1 día)"
        ig["comments"] = instagram_comments(ig_page)
        fb = read_facebook(fb_page, posts[0], facebook, facebook_day)
        if normalized(fb["caption"]) != normalized(ig["caption"]) or not fb["published_date_display"]:
            return "unresolved", "No se pudo confirmar la fecha y descripción de Facebook"
        pair = {
            "match_method": "local date within one day and exact normalized caption",
            "date_difference_days": (facebook_day.date() - day.date()).days,
            "matching_candidates_loaded": len(posts),
            "match_selection": "first displayed" if len(posts) > 1 else "single loaded match",
            "instagram": ig, "facebook": fb, "scraped_at": datetime.now(timezone.utc),
            "reference_profile": account,
        }
        save_pair(collection.database.client, pair)
        detail = f"comentarios IG={len(ig['comments'])}, FB={len(fb['comments'])}; candidatos={len(posts)}"
        return "saved", detail
    finally:
        ig_page.close()
        fb_page.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", choices=tuple(ACCOUNTS), action="append",
                        help="Limitar a un perfil; repetir para incluir ambos")
    parser.add_argument("--max-posts", type=int, default=0, help="Límite de pruebas; 0 procesa todos")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--retry-errors", action="store_true", help="Reintentar publicaciones con error")
    parser.add_argument("--retry-unresolved", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if args.max_posts < 0:
        parser.error("--max-posts debe ser mayor o igual a cero")
    load_dotenv(ROOT / ".env")
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise ValueError("MONGODB_URI no está configurado")
    accounts = args.account or list(ACCOUNTS)
    count = 0
    failed = False
    with sqlite3.connect(STATE) as db, MongoClient(uri) as client, sync_playwright() as playwright:
        init_state(db)
        mongo_db = client["SocialMediaDB"]
        if "SocialMediaDB" not in mongo_db.list_collection_names():
            raise ValueError("No se encontró la colección SocialMediaDB existente")
        collection = mongo_db["SocialMediaDB"]
        _, links = collections(client)
        ensure_link_index(links)
        context = playwright.chromium.launch_persistent_context(
            str(ROOT / ".browser-profile"), channel="msedge", headless=not args.headed,
        )
        try:
            for account in accounts:
                facebook, cutoff = ACCOUNTS[account]
                if not args.skip_discovery:
                    discover(context, db, account, cutoff)
                statuses = ["pending"]
                if args.retry_errors:
                    statuses.append("error")
                if args.retry_unresolved:
                    statuses.append("unresolved")
                placeholders = ",".join("?" for _ in statuses)
                rows = db.execute(
                    f"SELECT url FROM posts WHERE account=? AND status IN ({placeholders}) ORDER BY rowid",
                    (account, *statuses),
                ).fetchall()
                for (url,) in rows:
                    if args.max_posts and count >= args.max_posts:
                        break
                    try:
                        status, detail = process_post(context, collection, account, facebook, cutoff, url)
                    except (PlaywrightError, ValueError) as error:
                        status, detail = "error", f"{type(error).__name__}: {str(error)[:350]}"
                        failed = True
                    set_status(db, account, url, status, detail)
                    print(f"{account}: {status} {url} ({detail})", flush=True)
                    count += 1
                if args.max_posts and count >= args.max_posts:
                    break
        finally:
            context.close()
        for account in accounts:
            summary = db.execute("SELECT status, COUNT(*) FROM posts WHERE account=? GROUP BY status",
                                 (account,)).fetchall()
            print(f"{account}: {dict(summary)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PlaywrightError, ValueError) as error:
        print(f"Proceso interrumpido: {error}", file=sys.stderr)
        raise SystemExit(1)
