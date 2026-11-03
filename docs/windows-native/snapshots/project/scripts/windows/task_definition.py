"""Render a limited, passwordless interactive logon/recovery task; no installation side effects."""
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

NS='http://schemas.microsoft.com/windows/2004/02/mit/task'

def render_task(root, pythonw, sid):
    root=Path(root)
    ET.register_namespace('',NS)
    node=ET.Element('{'+NS+'}Task',version='1.4')
    def child(parent,name,text=None,**attributes):
        item=ET.SubElement(parent,'{'+NS+'}'+name,attributes)
        if text is not None:item.text=str(text)
        return item
    registration=child(node,'RegistrationInfo')
    child(registration,'Description','Hermes voice satellite stack: user-account logon startup with process supervision and scheduler crash recovery.')
    triggers=child(node,'Triggers'); logon=child(triggers,'LogonTrigger')
    child(logon,'Enabled','true');child(logon,'UserId',sid);child(logon,'Delay','PT15S')
    principals=child(node,'Principals');principal=child(principals,'Principal',id='CurrentUser')
    child(principal,'UserId',sid);child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
    settings=child(node,'Settings')
    for name,value in [('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),('StopIfGoingOnBatteries','false'),('AllowHardTerminate','true'),('StartWhenAvailable','true'),('RunOnlyIfNetworkAvailable','false')]:child(settings,name,value)
    idle=child(settings,'IdleSettings');child(idle,'StopOnIdleEnd','false');child(idle,'RestartOnIdle','false')
    for name,value in [('AllowStartOnDemand','true'),('Enabled','true'),('Hidden','false'),('RunOnlyIfIdle','false'),('WakeToRun','false'),('ExecutionTimeLimit','PT0S'),('Priority','7')]:child(settings,name,value)
    restart=child(settings,'RestartOnFailure');child(restart,'Interval','PT1M');child(restart,'Count','999')
    actions=child(node,'Actions',Context='CurrentUser');execute=child(actions,'Exec')
    child(execute,'Command',str(pythonw))
    child(execute,'Arguments',subprocess.list2cmdline([str(root/'scripts/windows/supervise.pyw')]))
    child(execute,'WorkingDirectory',str(root))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(node,encoding='unicode',xml_declaration=False)
