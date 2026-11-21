import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pipeline
from social_media import batch, scraper
from social_media.label_social_media import update_preconditions


class PipelineTests(unittest.TestCase):
    def test_relocated_scripts_keep_project_root_and_progress_path(self):
        project_root = Path(__file__).resolve().parent.parent
        self.assertEqual(scraper.ROOT, project_root)
        self.assertEqual(batch.STATE, project_root / ".batch-state.sqlite3")

    @patch("pipeline.subprocess.run")
    @patch("pipeline.load_config", return_value={"MONGODB_DB": "SocialMediaDB"})
    def test_preview_only_runs_classifier_dry_run(self, config, run):
        run.return_value.returncode = 0
        self.assertEqual(pipeline.main([]), 0)
        self.assertEqual(run.call_count, 1)
        self.assertIn("--dry-run", run.call_args.args[0])

    @patch("pipeline.subprocess.run")
    @patch("pipeline.load_config", return_value={"MONGODB_DB": "SocialMediaDB"})
    def test_phase_one_failure_stops_phase_two(self, config, run):
        run.return_value.returncode = 1
        self.assertEqual(pipeline.main(["--write", "--skip-discovery"]), 1)
        self.assertEqual(run.call_count, 1)
        self.assertIn("--skip-discovery", run.call_args.args[0])

    @patch("pipeline.subprocess.run")
    @patch("pipeline.load_config", return_value={"MONGODB_DB": "SocialMediaDB"})
    def test_phases_run_in_order_and_propagate_failure(self, config, run):
        run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=2)]
        self.assertEqual(pipeline.main(["--write", "--account", "presidenciacr"]), 2)
        self.assertEqual(run.call_args_list[0].args[0][2:4], ["-m", "social_media.batch"])
        self.assertEqual(run.call_args_list[1].args[0][2:4], ["-m", "social_media.label_social_media"])
        self.assertNotIn("--dry-run", run.call_args_list[1].args[0])

    @patch("pipeline.subprocess.run")
    @patch("pipeline.load_config", return_value={"MONGODB_DB": "otra"})
    def test_rejects_different_databases_before_phase_one(self, config, run):
        self.assertEqual(pipeline.main(["--write"]), 1)
        run.assert_not_called()

    def test_classification_conditions_protect_source_and_manual_labels(self):
        record = {
            "_id": "id", "caption": "Caption", "rubro": "Cultura", "has_rubro": True,
            "platforms": {
                "facebook": {"comments": ["a"], "labels": [{"index": 0}], "has_labels": True},
                "instagram": {"comments": ["b"], "labels": None, "has_labels": False},
            },
        }
        query = update_preconditions(record)
        self.assertEqual(query["caption"], "Caption")
        self.assertEqual(query["rubro"], {"$eq": "Cultura"})
        self.assertEqual(query["facebook.comments"], {"$eq": ["a"]})
        self.assertEqual(query["facebook.comment_labels"], {"$eq": [{"index": 0}]})
        self.assertEqual(query["instagram.comment_labels"], {"$exists": False})


if __name__ == "__main__":
    unittest.main()
