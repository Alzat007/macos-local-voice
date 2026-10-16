import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('release_audit',ROOT/'scripts/audit_release.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

class ReleaseAuditTests(unittest.TestCase):
    def test_working_cleanup_cannot_hide_sensitive_staged_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git','init','--quiet',str(root)],check=True)
            (root/'source.txt').write_text('gh'+'p_'+'b'*30)
            subprocess.run(['git','-C',str(root),'add','source.txt'],check=True)
            (root/'source.txt').write_text('now clean')
            self.assertEqual(audit.audit(root)['status'],'fail')

    def test_media_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'voice.wav').write_bytes(b'RIFF')
            self.assertTrue(audit.inspect_file(root,Path('voice.wav')))

    def test_private_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'voice_profile.json').write_text('{}')
            self.assertTrue(audit.inspect_file(root,Path('voice_profile.json')))

    def test_credentials_are_not_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = 'gh'+'p_'+'a'*30
            (root/'accidental.txt').write_text(secret)
            issues = audit.inspect_file(root,Path('accidental.txt'))
            self.assertIn('GitHub credential',issues)
            self.assertNotIn(secret,str(issues))

    def test_generic_source_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'source.py').write_text('print("local voice")\n')
            self.assertEqual(audit.inspect_file(root,Path('source.py')),[])

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'source.py').write_text('pass\n')
            (root/'link.py').symlink_to(root/'source.py')
            self.assertIn('symbolic link is not a portable source file',audit.inspect_file(root,Path('link.py')))

if __name__=='__main__':
    unittest.main()
