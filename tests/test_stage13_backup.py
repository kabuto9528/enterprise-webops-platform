import io,tarfile,tempfile,unittest,importlib.util,os
from pathlib import Path
import sys
if sys.platform == 'win32':
    import pytest
    pytest.skip('Linux archive ownership and symlinks', allow_module_level=True)
spec=importlib.util.spec_from_file_location('backup',Path(__file__).resolve().parents[1]/'scripts/stage13-backup-restore.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Safety(unittest.TestCase):
    def test_roundtrip_permissions_and_link(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'source';source.mkdir();f=source/'config';f.write_bytes(b'example');f.chmod(0o640);(source/'link').symlink_to('config')
            archive=p/'b.tar.gz'
            with tarfile.open(archive,'w:gz',dereference=False) as t:t.add(source,arcname='source')
            target=p/'restore';target.mkdir();m.restore(archive,target)
            original=m.inventory([source]);restored=m.inventory([target/'source'])
            self.assertEqual(list(original.values()),list(restored.values()))
    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);archive=p/'bad.tar.gz'
            with tarfile.open(archive,'w:gz') as t:
                item=tarfile.TarInfo('../escaped');item.uid=os.getuid();item.gid=os.getgid();item.size=1;t.addfile(item,io.BytesIO(b'x'))
            with self.assertRaises(RuntimeError):m.restore(archive,p/'restore')
            self.assertFalse((p/'escaped').exists())
    def test_no_extraction_through_archive_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);archive=p/'bad.tar.gz';outside=p/'outside';outside.mkdir()
            with tarfile.open(archive,'w:gz') as t:
                item=tarfile.TarInfo('jump');item.uid=os.getuid();item.gid=os.getgid();item.type=tarfile.SYMTYPE;item.linkname=str(outside);t.addfile(item)
                item=tarfile.TarInfo('jump/file');item.uid=os.getuid();item.gid=os.getgid();item.size=1;t.addfile(item,io.BytesIO(b'x'))
            target=p/'restore';target.mkdir()
            with self.assertRaises((RuntimeError,FileExistsError)):m.restore(archive,target)
            self.assertFalse((outside/'file').exists())
if __name__=='__main__':unittest.main()
