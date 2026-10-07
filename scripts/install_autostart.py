"""Opt-in per-user macOS launch agent; never changes another AIRI installation."""
import argparse,os,plistlib,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LABEL='community.'+ROOT.name
def main():
 p=argparse.ArgumentParser();p.add_argument('--remove',action='store_true');args=p.parse_args()
 if sys.platform!='darwin':p.error('Use your OS service manager on non-macOS systems.')
 plist=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist');domain='gui/'+str(os.getuid())
 if args.remove:
  subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=False)
  plist.unlink(missing_ok=True);return
 if plist.exists():p.error('This launcher is already installed; remove it before replacing it.')
 python=ROOT/'.venv/bin/python'
 if not python.exists():p.error('Run scripts/setup.sh first.')
 log=ROOT/'data/logs';log.mkdir(parents=True,exist_ok=True);plist.parent.mkdir(parents=True,exist_ok=True)
 body=dict(Label=LABEL,ProgramArguments=[str(python),str(ROOT/'run.py')],WorkingDirectory=str(ROOT),RunAtLoad=True,KeepAlive=True,ThrottleInterval=20,EnvironmentVariables={'AIRI_LOGIN_AUTOSTART':'1'},StandardOutPath=str(log/'service.log'),StandardErrorPath=str(log/'service-error.log'))
 with plist.open('xb') as f:plistlib.dump(body,f)
 subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
 print('Installed',LABEL,'for this user. The computer must be awake and logged in.')
if __name__=='__main__':main()
