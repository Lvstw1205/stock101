"""macOS login Keychain. No shell arguments, secret files, browser input or logging."""
import ctypes as C
import json
import sys
SERVICE=b'Stock101.Community.Coinbase.ReadOnly'
ACCOUNT=b'advanced-trade'
class CredentialError(ValueError):pass

def libraries():
    if sys.platform!='darwin':raise CredentialError('MACOS_KEYCHAIN_REQUIRED')
    sec=C.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec.SecKeychainFindGenericPassword.argtypes=[C.c_void_p,C.c_uint32,C.c_char_p,C.c_uint32,C.c_char_p,C.POINTER(C.c_uint32),C.POINTER(C.c_void_p),C.POINTER(C.c_void_p)]
    sec.SecKeychainFindGenericPassword.restype=C.c_int32
    sec.SecKeychainAddGenericPassword.argtypes=[C.c_void_p,C.c_uint32,C.c_char_p,C.c_uint32,C.c_char_p,C.c_uint32,C.c_void_p,C.POINTER(C.c_void_p)]
    sec.SecKeychainAddGenericPassword.restype=C.c_int32
    sec.SecKeychainItemModifyAttributesAndData.argtypes=[C.c_void_p,C.c_void_p,C.c_uint32,C.c_void_p]
    sec.SecKeychainItemModifyAttributesAndData.restype=C.c_int32
    sec.SecKeychainItemFreeContent.argtypes=[C.c_void_p,C.c_void_p]
    cf.CFRelease.argtypes=[C.c_void_p]
    return sec,cf

def read(scope="READ_ONLY"):
    service=SERVICE if scope=="READ_ONLY" else b"Stock101.Community.Coinbase.Live" if scope=="LIVE" else None
    if service is None:raise CredentialError("INVALID_CREDENTIAL_SCOPE")
    sec,cf=libraries();length=C.c_uint32();data=C.c_void_p();item=C.c_void_p()
    status=sec.SecKeychainFindGenericPassword(None,len(service),service,len(ACCOUNT),ACCOUNT,C.byref(length),C.byref(data),C.byref(item))
    if status==-25300:return None
    if status:raise CredentialError('KEYCHAIN_ACCESS_DENIED_OR_LOCKED')
    try:
        if length.value>65536:raise CredentialError('KEYCHAIN_ITEM_INVALID')
        value=json.loads(C.string_at(data,length.value))
        return validate(value)
    except CredentialError:raise
    except Exception:raise CredentialError('KEYCHAIN_ITEM_INVALID') from None
    finally:
        if data:sec.SecKeychainItemFreeContent(None,data)
        if item:cf.CFRelease(item)

def validate(value):
    try:
        import re
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey,SECP256R1
        name=value.get('name');secret=value.get('privateKey')
        if not isinstance(name,str) or not re.fullmatch(r'organizations/[A-Za-z0-9-]+/apiKeys/[A-Za-z0-9-]+',name):raise ValueError()
        if not isinstance(secret,str) or len(secret)>16384:raise ValueError()
        secret=secret.replace('\\n','\n').strip()
        match=re.fullmatch(r'-----BEGIN (EC PRIVATE KEY|PRIVATE KEY)-----\s*(.*?)\s*-----END \1-----',secret,re.S)
        if not match:raise ValueError()
        body=re.sub(r'\s+','',match.group(2))
        secret='-----BEGIN '+match.group(1)+'-----\n'+'\n'.join(body[i:i+64] for i in range(0,len(body),64))+'\n-----END '+match.group(1)+'-----\n'
        key=load_pem_private_key(secret.encode(),password=None)
        if not isinstance(key,EllipticCurvePrivateKey) or not isinstance(key.curve,SECP256R1):raise ValueError()
        return name,secret
    except Exception:raise CredentialError('ECDSA_KEY_JSON_REQUIRED') from None

def save(value,scope="READ_ONLY"):
    service=SERVICE if scope=="READ_ONLY" else b"Stock101.Community.Coinbase.Live" if scope=="LIVE" else None
    if service is None:raise CredentialError("INVALID_CREDENTIAL_SCOPE")
    name,secret=validate(value);sec,cf=libraries();item=C.c_void_p()
    raw=json.dumps({'name':name,'privateKey':secret}).encode();buffer=C.create_string_buffer(raw)
    status=sec.SecKeychainFindGenericPassword(None,len(service),service,len(ACCOUNT),ACCOUNT,None,None,C.byref(item))
    try:
        if status==0:status=sec.SecKeychainItemModifyAttributesAndData(item,None,len(raw),buffer)
        elif status==-25300:status=sec.SecKeychainAddGenericPassword(None,len(service),service,len(ACCOUNT),ACCOUNT,len(raw),buffer,None)
        if status:raise CredentialError('KEYCHAIN_SAVE_FAILED')
    finally:
        C.memset(buffer,0,len(buffer))
        if item:cf.CFRelease(item)
