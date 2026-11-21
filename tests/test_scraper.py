import unittest
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

from social_media.batch import canonical_profile_link, init_state, set_status
from social_media.scraper import facebook_comments, instagram_comments, instagram_metadata, instagram_url, normalized, parse_count
from social_media.storage import ensure_link_index, save_pair


class ScraperTests(unittest.TestCase):
    def test_exact_and_rounded_counts(self):
        self.assertEqual(parse_count("36.012"), {"display": "36.012", "value": 36012})
        self.assertEqual(parse_count("6,4\u00a0mil"), {"display": "6,4 mil", "value": 6400})
        self.assertEqual(parse_count("19K")["value"], 19000)
        self.assertEqual(parse_count("1.2M")["value"], 1200000)
        with self.assertRaises(ValueError):
            parse_count("unknown")

    def test_instagram_metadata_preserves_multiline_caption(self):
        account, caption, likes = instagram_metadata(
            '7,492 likes, 398 comments - example on August 30, 2026: "First line.\nSecond line.". '
        )
        self.assertEqual((account, caption, likes["value"]), ("example", "First line.\nSecond line.", 7492))

    def test_only_instagram_post_urls_are_accepted(self):
        self.assertEqual(
            instagram_url("https://www.instagram.com/example/reel/ABC_123/?foo=bar"),
            "https://www.instagram.com/p/ABC_123/",
        )
        with self.assertRaises(ValueError):
            instagram_url("https://instagram.com.evil.example/p/ABC/")

    def test_match_ignores_spacing_but_not_words(self):
        self.assertEqual(normalized("Hola\n  mundo"), normalized("hola mundo"))
        self.assertNotEqual(normalized("Hola mundo"), normalized("Hola a todos"))

    def test_profile_reels_use_post_view(self):
        self.assertEqual(
            canonical_profile_link("/example/reel/ABC_123/"),
            "https://www.instagram.com/p/ABC_123/",
        )
        with self.assertRaises(ValueError):
            canonical_profile_link("/example/stories/ABC/")

    def test_progress_preserves_completed_status_when_rediscovering(self):
        with sqlite3.connect(":memory:") as db:
            init_state(db)
            db.execute("INSERT INTO posts (account,url) VALUES (?,?)", ("profile", "url"))
            set_status(db, "profile", "url", "saved", "ok")
            db.execute("INSERT OR IGNORE INTO posts (account,url) VALUES (?,?)", ("profile", "url"))
            self.assertEqual(db.execute("SELECT status,detail FROM posts").fetchone(), ("saved", "ok"))

    def test_instagram_comments_only_store_text_and_preserve_distinct_duplicates(self):
        page = Mock()
        links = page.locator.return_value
        links.evaluate_all.side_effect = [
            [{"id": "a", "text": "same"}, {"id": "b", "text": "same"}],
            [{"id": "a", "text": "same"}, {"id": "b", "text": "same"}],
        ]
        with patch("social_media.scraper.scroll_nearest_comment_container", side_effect=[True, False]):
            self.assertEqual(instagram_comments(page), ["same", "same"])

    def test_facebook_comments_only_store_text_and_preserve_distinct_duplicates(self):
        dialog = Mock()
        comments = dialog.get_by_role.return_value
        comments.count.return_value = 2
        blocks = []
        for identifier in ("a", "b"):
            block = Mock()
            block.get_by_role.return_value.count.return_value = 0
            block.evaluate.return_value = {"id": identifier, "text": "same"}
            blocks.append(block)
        comments.nth.side_effect = [Mock(locator=Mock(return_value=block)) for block in blocks]
        dialog.evaluate.return_value = False
        self.assertEqual(facebook_comments(dialog), ["same", "same"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.source = Mock()
        self.links = Mock()
        self.client = MagicMock()
        self.session = self.client.start_session.return_value.__enter__.return_value
        self.session.with_transaction.side_effect = lambda operation: operation(self.session)
        self.pair = {
            "_id": "https://www.instagram.com/p/example/",
            "instagram": {"url": "https://www.instagram.com/p/example/", "caption": "Example",
                          "published_at": datetime(2026, 8, 30, tzinfo=timezone.utc), "comments": ["a"]},
            "facebook": {"url": "https://www.facebook.com/reel/example/", "caption": "Example",
                         "published_date_display": "30 de agosto de 2026", "comments": ["b"]},
            "extra": {"unchanged": True},
        }

    def test_new_pair_stores_urls_only_in_link_database(self):
        self.source.find_one.return_value = None
        self.links.find_one.return_value = None
        with patch("social_media.storage.collections", return_value=(self.source, self.links)):
            save_pair(self.client, self.pair)
        publication = self.source.insert_one.call_args.args[0]
        query, update = self.links.update_one.call_args.args
        self.assertEqual(query["_id"], publication["_id"])
        self.assertEqual(update["$setOnInsert"]["id_original"], self.pair["_id"])
        self.assertEqual(update["$set"]["facebook_url"], self.pair["facebook"]["url"])
        self.assertEqual(publication["caption"], "Example")
        self.assertEqual(publication["published_at"], self.pair["instagram"]["published_at"])
        self.assertNotIn("url", publication["instagram"])
        self.assertNotIn("caption", publication["facebook"])
        self.assertNotIn("rubro", publication)

    def test_existing_pair_keeps_id_and_updates_links(self):
        self.source.find_one.side_effect = [None, {
            "caption": "Manual", "rubro": "Cultura",
            "instagram": {"comments": ["a"], "comment_labels": [{"index": 0}]},
            "facebook": {"comments": ["old"], "comment_labels": [{"index": 0}]},
        }]
        self.links.find_one.return_value = {"_id": "existing"}
        with patch("social_media.storage.collections", return_value=(self.source, self.links)):
            save_pair(self.client, self.pair)
        query, update = self.source.update_one.call_args.args
        self.assertEqual(query, {"_id": "existing"})
        self.assertEqual(update["$unset"], {"facebook.comment_labels": ""})
        self.assertNotIn("caption", update["$set"])
        self.assertNotIn("rubro", update["$set"])
        self.assertNotIn("instagram.comment_labels", update["$unset"])
        self.source.insert_one.assert_not_called()
        self.links.update_one.assert_called_once()
        metadata = self.links.update_one.call_args.args[1]["$set"]
        self.assertEqual(metadata["original_publication_fields.facebook.caption"], "Example")
        self.assertNotIn("original_publication_fields", metadata)
        self.assertNotIn("original_publication_fields.instagram.rubro", metadata)

    def test_requires_existing_unique_link_index_without_creating_it(self):
        self.links.index_information.return_value = {"_id_": {"key": [("_id", 1)]}}
        with self.assertRaises(ValueError):
            ensure_link_index(self.links)
        self.links.create_index.assert_not_called()
        self.links.index_information.return_value["instagram_url_unique"] = {
            "key": [("instagram_url", 1)], "unique": True,
        }
        ensure_link_index(self.links)

    def test_legacy_or_orphan_mapping_stops_write(self):
        self.source.find_one.return_value = {"_id": self.pair["_id"]}
        with patch("social_media.storage.collections", return_value=(self.source, self.links)):
            with self.assertRaises(ValueError):
                save_pair(self.client, self.pair)
        self.source.update_one.assert_not_called()
        self.source.find_one.side_effect = [None, None]
        self.links.find_one.return_value = {"_id": "orphan"}
        with patch("social_media.storage.collections", return_value=(self.source, self.links)):
            with self.assertRaises(ValueError):
                save_pair(self.client, self.pair)
        self.links.update_one.assert_not_called()

if __name__ == "__main__":
    unittest.main()
