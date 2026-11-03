import importlib.util
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class TaskDefinitionTest(unittest.TestCase):
    def test_logon_task_is_limited_interactive_and_restarts_without_time_limit(self):
        path=ROOT/'scripts/windows/task_definition.py'
        self.assertTrue(path.is_file(),'Missing scheduled-task renderer')
        spec=importlib.util.spec_from_file_location('task_definition',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        xml=module.render_task(ROOT,Path('C:/Python/pythonw.exe'),'S-1-5-21-123')
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-16"?>'), 'Task Scheduler consumes a UTF-16 Windows string')
        doc=ET.fromstring(xml); ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        get=lambda p:doc.find(p,ns).text
        self.assertEqual(get('t:Principals/t:Principal/t:UserId'),'S-1-5-21-123')
        self.assertEqual(get('t:Principals/t:Principal/t:LogonType'),'InteractiveToken')
        self.assertEqual(get('t:Principals/t:Principal/t:RunLevel'),'LeastPrivilege')
        self.assertEqual(get('t:Settings/t:RestartOnFailure/t:Interval'),'PT1M')
        self.assertEqual(get('t:Settings/t:RestartOnFailure/t:Count'),'999')
        self.assertEqual(get('t:Settings/t:ExecutionTimeLimit'),'PT0S')
        self.assertEqual(get('t:Settings/t:DisallowStartIfOnBatteries'),'false')
        self.assertEqual(get('t:Settings/t:StopIfGoingOnBatteries'),'false')
        self.assertEqual(get('t:Settings/t:MultipleInstancesPolicy'),'IgnoreNew')
        self.assertEqual(Path(get('t:Actions/t:Exec/t:Command')),Path('C:/Python/pythonw.exe'))
        self.assertIn('supervise.pyw',get('t:Actions/t:Exec/t:Arguments'))
        self.assertEqual(get('t:Actions/t:Exec/t:WorkingDirectory'),str(ROOT))
        self.assertNotIn('Password',xml)
if __name__=='__main__': unittest.main()
