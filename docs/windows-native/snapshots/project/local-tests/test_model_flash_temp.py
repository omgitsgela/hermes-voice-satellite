import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('provision_model',ROOT/'scripts/provision-device-model.py')
model=importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

class ModelFlashTempTest(unittest.TestCase):
    def test_flash_blob_is_readable_by_another_process_and_cleaned(self):
        real_run=subprocess.run
        paths=[]
        def inspect(command, **kwargs):
            path=Path(command[-1]); paths.append(path)
            result=real_run([sys.executable,'-c','import sys; assert open(sys.argv[1],"rb").read()==b"model-test"',str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return result
        with patch.object(model.shutil,'which',return_value=None),patch.object(model.subprocess,'run',side_effect=inspect):
            model.flash_port('COM-TEST',460800,b'model-test')
        self.assertFalse(paths[0].exists())

if __name__=='__main__': unittest.main()
