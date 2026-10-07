"""Lossless compression of repeated holdings arrays, retaining scalar SQL fields.

Only new snapshots/decision rows are encoded. Orders, balances, fills and all
existing records remain unchanged. Repository readers restore the exact arrays.
"""
import base64
import json
import zlib

KEY='_stock101_positions_zlib_v1'
MAX_BYTES=8_000_000


def pack_portfolio(value):
    if not isinstance(value,dict) or not isinstance(value.get('positions'),list):return value
    raw=json.dumps(value['positions'],allow_nan=False,separators=(',',':')).encode()
    if len(raw)<8192 or len(raw)>MAX_BYTES:return value
    packed=base64.b64encode(zlib.compress(raw,6)).decode('ascii')
    if len(packed)>=len(raw)*.8:return value
    return {**value,'positions':{KEY:packed}}


def unpack_portfolio(value):
    if not isinstance(value,dict):return value
    packed=value.get('positions')
    if not isinstance(packed,dict) or set(packed)!={KEY}:return value
    try:
        encoded=base64.b64decode(packed[KEY],validate=True)
        decoder=zlib.decompressobj()
        raw=decoder.decompress(encoded,MAX_BYTES+1)
        if len(raw)>MAX_BYTES or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError('INVALID_COMPRESSED_HOLDINGS')
        positions=json.loads(raw)
        if not isinstance(positions,list):raise ValueError('INVALID_COMPRESSED_HOLDINGS')
        return {**value,'positions':positions}
    except Exception as error:
        raise ValueError('INVALID_COMPRESSED_HOLDINGS') from error


def encode(table,value):
    if table=='portfolio_snapshots':return pack_portfolio(value)
    if table=='trade_decisions':
        return {k:pack_portfolio(v) if k in ('portfolio_before','portfolio_after') else v for k,v in value.items()}
    return value


def decode(raw):
    value=unpack_portfolio(json.loads(raw))
    if isinstance(value,dict):
        for key in ('portfolio_before','portfolio_after'):
            if key in value:value[key]=unpack_portfolio(value[key])
    return value
