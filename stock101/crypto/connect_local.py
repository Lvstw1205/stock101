#!/usr/bin/env python3
"""Run interactively in the user's terminal. Never echo credentials."""
import json
from pathlib import Path
import shlex
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from stock101.crypto import credentials
from stock101.crypto.exchange import CoinbaseAdapter

def main():
    live="--live" in sys.argv
    if live:sys.argv.remove("--live")
    if sys.argv[1:]==['--stdin-json']:
        value=json.loads(sys.stdin.buffer.read(65537))
        path=None
    elif len(sys.argv)==3 and sys.argv[1]=='--json-file':
        path=Path(sys.argv[2]).expanduser()
    else:
        if not sys.stdin.isatty():raise ValueError('LOCAL_FILE_PICKER_REQUIRED')
        print('Coinbase에서 다운로드한 키 JSON 파일을 선택하세요.')
        paths=shlex.split(input('JSON 파일 경로: ').strip())
        if len(paths)!=1:raise ValueError('INVALID_PATH')
        path=Path(paths[0]).expanduser()
    if path is not None:
        if not path.is_file() or path.stat().st_size>65536:raise ValueError('INVALID_KEY_FILE')
        value=json.loads(path.read_text())
    pair=credentials.validate(value)
    if live:
        from stock101.crypto.live_exchange import LiveAdapter
        adapter=LiveAdapter(credential_provider=lambda:pair);adapter.permissions(True);adapter.balances()
    else:
        adapter=CoinbaseAdapter();adapter.credentials=lambda:pair;adapter.check_readonly_connection()
    credentials.save(value,"LIVE" if live else "READ_ONLY")
    print('연결 완료! 키를 이 Mac의 키체인에 안전하게 저장했습니다.')
    print('Stock101에서 연결 상태를 확인하세요. 키 등록은 자동매매 시작과 별도입니다.')
    
if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):print('\n등록을 취소했습니다.')
    except Exception as e:
        # Never print arbitrary parser/network exception bodies containing user inputs.
        safe={'ECDSA_KEY_JSON_REQUIRED':'키 형식이 맞지 않습니다. 전체 API Key Name과 ECDSA 비밀키(BEGIN/END 포함)를 확인하세요.','TRADE_PERMISSION_REQUIRED':'실거래용 키에는 View와 Trade가 필요합니다. Transfer는 꺼두세요.','VIEW_WITHOUT_TRANSFER_REQUIRED':'View를 켜고 Transfer는 꺼둔 키가 필요합니다.','VIEW_ONLY_KEY_REQUIRED':'조회 전용 키가 필요합니다. View만 켜고 Trade·Transfer는 끈 키를 만들어 주세요.','KEYCHAIN_SAVE_FAILED':'Mac 키체인에 저장하지 못했습니다. 키체인 잠금과 접근 허용을 확인하세요.','ACCESS_DENIED':'Coinbase 접근이 거절됐습니다. 키의 권한과 IP 허용 목록을 확인하세요.','AUTHENTICATION_FAILED':'Coinbase가 키를 확인하지 못했습니다. ECDSA 키인지 확인하세요.'}.get(str(e),'연결하지 못했습니다. 키 입력 내용과 인터넷 연결을 확인해 주세요.')
        print(safe);sys.exit(1)
