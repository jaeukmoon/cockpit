"""The private assets tab must not persist decrypted state or expose orders."""
from pathlib import Path
import unittest

SOURCE = (Path(__file__).resolve().parents[1]/'dashboard.html').read_text(encoding='utf-8')


class AssetsShellTests(unittest.TestCase):
    def test_separate_ciphertext_and_sandbox(self):
        self.assertIn('data/assets.enc.json', SOURCE)
        self.assertIn('id="assets-frame"', SOURCE)
        self.assertIn('id="view-assets"', SOURCE)
        self.assertIn('id="view-research"', SOURCE)
        self.assertNotIn('allow-same-origin', SOURCE)

    def test_relock_and_refresh_boundary(self):
        self.assertIn('clearInterval(ASSETS_TIMER)', SOURCE)
        self.assertIn('KEY_MATERIAL!==assetsMaterial', SOURCE)
        self.assertIn('wbq-assets-update', SOURCE)
        self.assertNotIn('localStorage', SOURCE)
        self.assertNotIn('sessionStorage', SOURCE)


if __name__ == '__main__':
    unittest.main()
