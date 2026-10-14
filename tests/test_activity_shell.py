"""Encrypted activity is an independent, memory-only, read-only view."""
from pathlib import Path
import unittest
from scripts.site_deploy.static_site import FILES

SOURCE=(Path(__file__).resolve().parents[1]/'dashboard.html').read_text(encoding='utf-8')


class ActivityShellTests(unittest.TestCase):
    def test_ciphertext_is_packaged_and_frame_is_sandboxed(self):
        self.assertIn('data/activity.enc.json',FILES)
        self.assertIn('id="activity-frame"',SOURCE)
        self.assertIn('id="view-activity"',SOURCE)
        self.assertNotIn('allow-same-origin',SOURCE)
        self.assertIn('payload.data.orders_enabled!==false',SOURCE)

    def test_relock_invalidates_pending_fetch_and_clears_content(self):
        self.assertIn('clearInterval(ACTIVITY_TIMER)',SOURCE)
        self.assertIn('KEY_MATERIAL!==material',SOURCE)
        self.assertIn('byId("activity-frame").srcdoc=""',SOURCE)
        self.assertIn('wbq-activity-error',SOURCE)
        self.assertNotIn('localStorage',SOURCE)


if __name__=='__main__':unittest.main()
