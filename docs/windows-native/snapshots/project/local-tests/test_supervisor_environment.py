import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/windows'))
import project_supervisor as supervisor

class EnvironmentIsolationTest(unittest.TestCase):
    def test_child_does_not_inherit_other_python_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'PYTHONPATH':'unrelated-hermes-packages','PYTHONHOME':'unrelated-home','VIRTUAL_ENV':'unrelated-venv','HERMES_HOME': str(Path(tmp)/'custom-profile')}):
            job = Mock()
            spec = supervisor.service_specs(Path(tmp))[2]
            supervisor.launch_child(Path(tmp), spec, job)
            env=job.spawn.call_args.kwargs['env']
            self.assertEqual(env['HERMES_HOME'], str(Path(tmp)/'custom-profile'))
            self.assertEqual(job.spawn.call_args.kwargs['cwd'], str(Path(tmp)/'custom-profile/hermes-agent'))
            for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'):
                self.assertFalse(key in env, key + ' must not be inherited')
            self.assertEqual(env['PYTHONNOUSERSITE'],'1')
if __name__=='__main__': unittest.main()
