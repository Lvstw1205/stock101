"""Install the optional pinned TradingAgents research backend into its own venv."""
import subprocess,sys,venv
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REV='8b22d43d01d9ddda5d686d093d5385884622f3de'
def main():
 target=ROOT/'.runtime/tradingagents-venv'
 venv.EnvBuilder(with_pip=True).create(target)
 python=target/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
 subprocess.run([str(python),'-m','pip','install','git+https://github.com/TauricResearch/TradingAgents.git@'+REV],check=True)
 print('Optional research backend installed. Configure a compatible local model or your own provider before use.')
if __name__=='__main__':main()
