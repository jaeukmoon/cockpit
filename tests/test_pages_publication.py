"""A provider outage must remain failed without blocking static UI deployment."""
from pathlib import Path
import unittest


class PublicationTests(unittest.TestCase):
    def test_package_is_independent_of_provider_success(self):
        source=(Path(__file__).resolve().parents[1]/'.github/workflows/research-monitor.yml').read_text()
        self.assertIn('  package-site:',source)
        package=source.split('  package-site:',1)[1].split('  deploy:',1)[0]
        self.assertIn('if: ${{ always() && !cancelled() }}',package)
        self.assertIn('needs: refresh',package)
        self.assertIn('ref: main',package)
        self.assertIn('test_assets_shell.py',package)
        self.assertNotIn('continue-on-error:',source)
        self.assertIn('needs: package-site',source.split('  deploy:',1)[1])


if __name__=='__main__':
    unittest.main()
