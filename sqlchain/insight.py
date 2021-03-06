#
#  Insight compatible API module
#  
import urlparse, cgi, json

from bitcoinrpc.authproxy import AuthServiceProxy
from gevent import sleep
from string import hexdigits

from version import *
from util import *

RESULT_ROW_LIMIT = 1000

#main entry point for api calls
def do_API(env, send_resp):
    get,args,cur = urlparse.parse_qs(env['QUERY_STRING']), env['PATH_INFO'].split('/')[2:], sqc.dbpool.get().cursor()
    send_resp('200 OK', [('Content-Type', 'application/json')])
    if args[0] == 'auto' or env['REQUEST_METHOD'] == 'POST':
        form = cgi.FieldStorage(fp=env['wsgi.input'], environ=env, keep_blank_values=True)
        if args[0] == "auto":
            param = form['data'].value if 'data' in form else args[1]
            if param.isdigit() and int(param) <= sqc.cfg['block']:
                blkhash = apiHeader(cur, param, args[2:])
                return json.dumps(apiBlock(cur, blkhash['blockHash'])) if blkhash else None
            elif len(param) == 64:
                if param[:8] == '00000000':
                    return json.dumps(apiBlock(cur, param))
                else:
                    return json.dumps(apiTx(cur, param, args))
            elif is_address(param):
                return json.dumps(apiAddr(cur, [ param ], args[2:], get))
        if args[0] == "addrs":
            return json.dumps(apiAddr(cur, form['addrs'].value.split(','), args[2:]))
        if args[0] == "tx" and args[1] == "send":
            return apiRPC('send', form['rawtx'].value)
        return None
    if args[0] == "block-index":
        return json.dumps(apiHeader(cur, args[1], args[2:]))
    if args[0] == "block":
        if len(args[1]) == 64 and all(c in hexdigits for c in args[1]):
            return json.dumps(apiBlock(cur, args[1]))
    if args[0] in ["tx","raw"]:
        if len(args[1]) == 64 and all(c in hexdigits for c in args[1]):
            return json.dumps(apiTx(cur, args[1], args))
    if args[0] == "txs":
        return json.dumps({ 'pagesTotal':1, 'txs': apiTxs(cur, get) })
    if args[0] in ["addr","addrs"]:
        return json.dumps(apiAddr(cur, args[1].split(','), args[2:], get))
    if args[0] == "history":
        return json.dumps(addrHistory(cur, args[1], args[2:]))
    if args[0] == "status":
        return json.dumps(apiStatus(cur, *args[1:]))
    if args[0] == "merkle":
        return json.dumps(apiMerkle(cur, args[1]))
    if args[0] == "utils":
        return json.dumps(apiRPC(args[1], get['nbBlocks'][0] if 'nbBlocks' in get else args[2] if len(args) > 2 else 2))
    if args[0] == "sync":
        return json.dumps(apiSync(cur, *[int(x) if x.isdigit() else 0 for x in args[1:]]))
    if args[0] == "closure":
        return json.dumps(apiClosure(cur, args[1].split(',') ))
    return None

def apiHeader(cur, blk, args):
    if blk.isdigit():
        cur.execute("select id,hash from blocks where id=%s limit 1;", (blk,))
    else:
        cur.execute("select id,hash from blocks order by id desc limit 1;")
    for blkid,blkhash in cur:
        hdr = gethdr(int(blkid), None, sqc.cfg['path'])
        if 'electrum' in args:
            return { 'block_height':int(blkid), 'version':hdr['version'], 'time':hdr['time'], 'bits':hdr['bits'], 'nonce':hdr['nonce'],
                     'merkle_root':hdr['merkleroot'][::-1].encode('hex'), 'prev_block_hash':hdr['previousblockhash'][::-1].encode('hex') }
        elif 'bci' in args:
            from bci import bciBlock
            return { 'blocks': [ bciBlock(cur, blkhash[::-1].encode('hex')) ] }
        else:
            return { 'blockHash': blkhash[::-1].encode('hex') }
    return None
    
def apiBlock(cur, blkhash):
    data = { 'hash':blkhash, 'tx':[] }
    cur.execute("select id from blocks where hash=%s limit 1;", (blkhash.decode('hex')[::-1],))
    for blk, in cur:
        data['height'] = int(blk)
        data['confirmations'] = sqc.cfg['block'] - data['height'] + 1
        data.update(gethdr(data['height'], None, sqc.cfg['path']))
        data['previousblockhash'] = data['previousblockhash'][::-1].encode('hex')
        data['merkleroot'] = data['merkleroot'][::-1].encode('hex')
        data['difficulty'] = bits2diff(data['bits'])
        data['bits'] = '%08x' % data['bits']
        data['reward'] = float((50 * 100000000) >> (data['height'] / 210000))/1e8
        data['isMainChain'] = True
        cur.execute("select hash from trxs where block_id>=%s and block_id<%s;", (blk*MAX_TX_BLK, blk*MAX_TX_BLK+MAX_TX_BLK))
        for txhash, in cur:
            data['tx'].append(txhash[::-1].encode('hex'))
        cur.execute("select hash from blocks where id=%s limit 1;", (data['height']+1,))
        for txhash, in cur:
            data['nextblockhash'] = txhash[::-1].encode('hex')
        return data
    return None
                
def apiAddr(cur, addrs, args, get={}):
    data = []
    for addr in addrs:
        if is_address(addr):
            addr_id = addr2id(addr, cur)
            if addr_id:
                if 'utxo' in args:
                    data.append(addrUTXOs(cur, addr_id, addr))
                else:
                    data.append(addrTXs(cur, addr_id, addr, args, get))
    return data if len(data) != 1 else data[0]
    
                            
def addrTXs(cur, addr_id, addr, args, get):
    incTxs = 'noTxList' not in get or get['noTxList'][0] == '0'
    offset = int(get['from'][0]) if 'from' in get else 0
    limit = min(int(get['to'][0])-offset, RESULT_ROW_LIMIT) if 'to' in get else RESULT_ROW_LIMIT
    txs = []
    sums = [[0,0],[0,0]]
    untxs = 0
    count = 0
    cur.execute("select value,t.id,tx_id,hash,block_id from trxs t left join outputs o on t.id=floor(o.id/{0}) or t.id=o.tx_id where addr_id=%s order by block_id desc;".format(MAX_IO_TX), (addr_id,))    
    #cur.execute("select * from (select value,t.id,tx_id,hash,block_id from trxs t left join outputs o on t.id=floor(o.id/{0}) or t.id=o.tx_id where addr_id=%s) r order by block_id limit {1},{2};".format(MAX_IO_TX,offset,limit), (addr_id,))
    #cur.execute("select value,t.id,tx_id,hash,block_id from trxs t left join outputs o on t.id=floor(o.id/{0}) or t.id=o.tx_id where addr_id=%s order by block_id limit {1},{2};".format(MAX_IO_TX,offset,limit), (addr_id,))
    for value,tx_id,spend_id,txhash,blk in cur:
        uncfmd = 1 if blk < 0 else 0
        untxs += uncfmd
        spend = 1 if tx_id == spend_id else 0
        sums[uncfmd][spend] += value
        
        if spend == 0 and count >= offset and count < offset+limit:
            txhash = txhash[::-1].encode('hex')
            if incTxs and txhash not in txs:
                txs.append(txhash)
        if spend == 0:
            count += 1
            
    if 'balance' in args:
        return int(sums[0][0]-sums[0][1])
    if 'unconfirmedBalance' in args:
        return int(sums[1][0]-sums[1][1])
    if 'totalReceived' in args:
        return int(sums[0][0])
    if 'totalSent' in args:
        return int(sums[0][1])
    
    return { 'addStr':addr, 'balanceSat':int(sums[0][0]-sums[0][1]), 'balance':float(sums[0][0]-sums[0][1])/1e8, 'totalReceivedSat':int(sums[0][0]), 
             'totalReceived': float(sums[0][0])/1e8, 'totalSentSat':int(sums[0][1]), 'totalSent':float(sums[0][1])/1e8, 
             'unconfirmedBalanceSat':int(sums[1][0]-sums[1][1]), 'unconfirmedBalance':float(sums[1][0]-sums[1][1])/1e8,
             'txApperances':count, 'transactions':txs, 'unconfirmedTxApperances':untxs }   

def addrUTXOs(cur, addr_id, addr):
    data = []
    cur.execute("select value,o.id,hash,block_id/{1} from trxs t left join outputs o on t.id=floor(o.id/{0}) and o.tx_id is null where addr_id=%s order by block_id;".format(MAX_IO_TX,MAX_TX_BLK), (addr_id,))
    #cur.execute("select value,o.id,t.hash,block_id/{1} from outputs o, trxs t, blocks b where tx_id is null and addr_id=%s and t.id=floor(o.id/{0}) and b.id=t.block_id/{0};".format(MAX_IO_TX,MAX_TX_BLK), (addr_id,))
    for value,out_id,txhash,blk in cur:
        data.append({ 'address':addr, 'txid':txhash[::-1].encode('hex'), 'vout':int(out_id)%MAX_IO_TX, 'amount':float(value)/1e8, 
                      'confirmations':sqc.cfg['block']-int(blk)+1 if blk>=0 else 0, 'ts':gethdr(int(blk), 'time', sqc.cfg['path']) if blk>=0 else 0 })
    return data

def addrHistory(cur, addr, args):
    txt = ''
    data = { 'cfmd':0, 'uncfmd':0 } if 'balance' in args else { 'txs':[] }
    addr_id = addr2id(addr, cur)
    if addr_id:
        cur.execute("select value,t.id,o.tx_id,hash,block_id,o.id%%{0} from outputs o, trxs t where addr_id=%s and (t.id=floor(o.id/{0}) or t.id=o.tx_id) order by block_id;".format(MAX_IO_TX), (addr_id,))
        for value,tx_id,spent_id,txhash,blk,n in cur:
            value = int(value)
            blk = blk/MAX_TX_BLK if blk >= 0 else 0
            if 'balance' in args:
                if blk == 0:
                    data['uncfmd'] += value if tx_id == spent_id else -value
                else:
                    data['cfmd'] += value if tx_id == spent_id else -value
            elif 'utxo' in args and not spent_id:
                tmp = { 'tx_hash':txhash[::-1].encode('hex'), 'height':int(blk), 'value':value, 'n':int(n) }
            else:
                tmp = { 'tx_hash':txhash[::-1].encode('hex'), 'height':int(blk) }
            if 'status' in args:
                txt += tmp['tx_hash'] + ":%d:" % tmp['height']
            elif ('uncfmd' not in args or tmp['height'] == 0) and 'balance' not in args:
                data['txs'].append(tmp)
    return (sha256(txt).digest().encode('hex') if txt else None) if 'status' in args else data

def apiTxs(cur, get):
    txs = []
    if 'block' in get:
        blkhash = get['block'][0]
        if len(blkhash) == 64 and all(c in hexdigits for c in blkhash):
            txhashes = apiBlock(cur, blkhash)
            txhashes = txhashes['tx'] if 'tx' in txhashes else []
    elif 'address' in get:
        txhashes = apiAddr(cur, [ get['address'][0] ], {}, {})
        txhashes = txhashes['transactions'] if 'transactions' in txhashes else []
    for txhash in txhashes:
        txs.append(apiTx(cur, txhash, []))
    return txs
    
def apiTx(cur, txhash, args):
    if 'output' in args:
        return txoAddr(cur, txhash, args[-1])
    if 'addrs' in args:
        return txAddrs(cur, txhash)
    data = { 'txid':txhash }
    txh = txhash.decode('hex')[::-1]
    cur.execute("select id,hash,txdata,block_id,ins,outs,txsize from trxs where id>=%s and hash=%s limit 1;", (txh2id(txh), txh))
    for tid,txh,blob,blkid,ins,outs,txsize in cur:
        if [i for i in ['raw','html'] if i in args]:
            return mkRawTx(cur, args, tid, txh, blob, blkid, ins, outs)
        data['confirmations'] = sqc.cfg['block'] - int(blkid)/MAX_TX_BLK + 1 if blkid >= 0 else 0
        data['version'],data['locktime'] = getBlobHdr(blob, sqc.cfg['path'])[4:6]
        data['valueIn'],data['vin'] = apiInputs(cur, blkid/MAX_TX_BLK, int(blob), ins)
        data['valueOut'],data['vout'] = apiOutputs(cur, int(tid), int(blob))
        data['fees'] = round(data['valueIn'] - data['valueOut'],8)
        data['size'] = txsize if txsize < 0xFF00 else (txsize&0xFF)<<16 + getBlobHdr(blob, sqc.cfg['path'])[3]
        cur.execute("select hash from blocks where id=%s limit 1;", (int(blkid)/MAX_TX_BLK,))
        for txhash, in cur:
            data['blockhash'] = txhash[::-1].encode('hex')
            data['time'] = data['blocktime'] = gethdr(int(blkid/MAX_TX_BLK), 'time', sqc.cfg['path'])
        if 'coinbase' in data['vin'][0]:
            del data['valueIn']
            del data['fees']
            data['isCoinBase'] = True
        return data
    return None
        
def apiInputs(cur, height, blob, ins):
    total = 0
    data = []
    hdr = getBlobHdr(blob, sqc.cfg['path'])
    if ins >= 192:
        ins = (ins & 63)*256 + hdr[1]  
    if ins == 0: # no inputs, assume coinbase
        cur.execute("select coinbase from blocks where id=%s;", (height,))
        data.append({ 'n':0, 'coinbase':cur.fetchone()[0].encode('hex') })
    else:
        buf = readBlob(blob+hdr[0], ins*7, sqc.cfg['path'])
        if len(buf) < ins*7 or buf == '\0'*ins*7: # means missing blob data
            data.append({ 'error':'missing data' })
        else:
            for n in range(ins):
                in_id, = unpack('<Q', buf[n*7:n*7+7]+'\0')
                cur.execute("select value,addr,addr_id,hash from outputs o, address a, trxs t where o.id=%s and a.id=o.addr_id and t.id=%s limit 1;", (in_id, in_id/MAX_IO_TX))
                for value,addr,aid,txhash in cur:
                    btc = float(value)/1e8
                    data.append({ 'n':n, 'vout':in_id%MAX_IO_TX, 'value':round(btc,8), 'valueSat':int(value), 'txid':txhash[::-1].encode('hex'), 'addr':mkaddr(addr,aid) })
                    total += btc
    return round(total,8),data
    
def apiOutputs(cur, txid, blob):
    total = 0
    data = []
    cur.execute("select o.id,o.id%%{0},value,addr,addr_id,o.tx_id from outputs o, address a where o.id>=%s*{0} and o.id<%s*{0} and a.id=o.addr_id;".format(MAX_IO_TX), (txid,txid+1))
    for out_id,n,value,addr,aid,in_id, in cur:
        btc = float(value)/1e8
        total += btc
        vout = { 'n':int(n), 'value':"%1.8f" % btc }
        vout['scriptPubKey'] = { 'addresses':[ mkaddr(addr,aid) ] }
        if in_id:
            vout.update(apiSpent(cur, int(in_id), int(out_id)))
        data.append(vout)
    return round(total,8),data
    
def apiSpent(cur, txid, out_id):
    cur.execute("select txdata,hash,block_id/{0},ins from trxs where id=%s limit 1;".format(MAX_TX_BLK), (txid,))
    for blob,txh,blk,ins in cur:
        hdr = getBlobHdr(int(blob), sqc.cfg['path'])
        if ins >= 192:
            ins = (ins & 63)*256 + hdr[1]  
        buf = readBlob(int(blob)+hdr[0], ins*7, sqc.cfg['path'])
        if len(buf) < ins*7 or buf == '\0'*ins*7: # means missing blob data
            return { 'error':'missing data' }
        for n in range(ins):
            in_id, = unpack('<Q', buf[n*7:n*7+7]+'\0')
            if in_id == out_id:
                return { 'spentTxId':txh[::-1].encode('hex'), 'spentIndex':n, 'spentTs':gethdr(int(blk),'time', sqc.cfg['path']) }
    return {}

def txoAddr(cur, txhash, n):
    txid = txh2id(txhash.decode('hex')[::-1])
    cur.execute("select addr,addr_id from outputs o, address a where o.id>=%s*{0} and o.id<%s*{0} and o.id%%{0}=%s and a.id=o.addr_id limit 1;".format(MAX_IO_TX), (txid,txid+1,int(n)))
    row = cur.fetchone()
    return mkaddr(row[0],row[1]) if row else None
    
def txAddrs(cur, txhash):
    data = []
    txid = txh2id(txhash.decode('hex')[::-1])
    cur.execute("select addr,addr_id from outputs o, address a where o.id>=%s*{0} and o.id<%s*{0} and a.id=o.addr_id;".format(MAX_IO_TX), (txid,txid+1))
    for pkh,aid in cur:
        data.append(mkaddr(pkh,aid))
    cur.execute("select txdata,ins from trxs where id=%s limit 1;", (txid,))
    for blob,ins in cur:
        hdr = getBlobHdr(int(blob), sqc.cfg['path'])
        if hdr[3] == 0: # txsize = 0, means missing blob data
            return []
        if ins > 0:
            if ins >= 0xC0:
                ins = (ins&0x3F)<<8 + hdr[1] 
            buf = readBlob(int(blob)+hdr[0], ins*7, sqc.cfg['path'])
            if len(buf) < ins*7 or buf == '\0'*ins*7: # means missing blob data
                return [ 'missing-data' ]
            for n in range(ins):
                in_id, = unpack('<Q', buf[n*7:n*7+7]+'\0')
                cur.execute("select addr,addr_id from outputs o, address a where o.id=%s and a.id=o.addr_id limit 1;", (in_id,))
                for pkh,aid in cur:
                    data.append(mkaddr(pkh,aid))
    return data

def apiMerkle(cur, txhash):
    txh = txhash.decode('hex')[::-1]
    cur.execute("select block_id from trxs where id>=%s and hash=%s limit 1", (txh2id(txh), txh))
    for blkid, in cur:
        blk,pos = divmod(int(blkid), MAX_TX_BLK)
        cur.execute("select hash from trxs where block_id>=%s and block_id<%s order by block_id;", (blk*MAX_TX_BLK, blk*MAX_TX_BLK+MAX_TX_BLK))
        mkt = [ tx for tx, in cur ]
        mkb,t = [],pos
        while len(mkt) > 1:
            if len(mkt) % 2 == 1:
                mkt.append(mkt[-1])
            mkb.append(mkt[t-1][::-1].encode('hex') if t % 2 == 1 else mkt[t+1][::-1].encode('hex'))
            mkt = [ sha256(sha256(mkt[i]+mkt[i+1]).digest()).digest() for i in range(0,len(mkt),2) ]
            t //= 2
        if mkt[0] != gethdr(blk, 'merkleroot', sqc.cfg['path']):
            logts("Panic! Merkle tree failure, tx %s" % txhash )
        return { "block_height": blk, "merkle": mkb, "pos": pos }
    return None

rawTxHdr = [ 'version','# inputs','# outputs', 'locktime' ]
rawCbHdr = [ 'null txid','n','coinbase size','coinbase bytes','sequence' ]
rawInHdr = [ 'in txid #%d','n #%d','sigScript size #%d','sigScript bytes #%d','sequence #%d' ] 
rawOutHdr = [ 'out value #%d','scriptPK size #%d','scriptPK<br>bytes/asm #%d' ]

def rawHTML(out, vi, vo):
    outhex = [ x.encode('hex') for x in out ]
    tags = [ x for x in rawTxHdr ]
    for n in range(vo):
        tags[3:3] = [ s%(vo-n-1) for s in rawOutHdr ]
        outhex[3+5*vi+3*n+2] += "<br><span class='opcode'>"+mkOpCodeStr(out[3+5*vi+3*n+2]).replace('\n', '<br>')+"</span>"
    if vi == 0:
        tags[2:2] = rawCbHdr
    else:
        for n in range(vi):
            tags[2:2] = [ s%(vi-n-1) for s in rawInHdr ]
            #outhex[2+5*n+3] += "<br><span class='opcode'>"+mkOpCodeStr(out[2+5*n+3]).replace('\n', '<br>')+"</span>"
    return "<table class='rawtx'><tr>"+"</tr><tr>".join(['<td>%s</td><td>%s</td>' % (k,v) for k,v in zip(tags,outhex) ])+"</tr></table>"
    
def mkRawTx(cur, args, txid, txhash, txdata, blkid, ins, outs):
    hdr = getBlobHdr(txdata, sqc.cfg['path'])
    out = [ pack('<I', hdr[4]) ]
    if ins >= 0xC0:
        ins = (ins&0x3F)<<8 + hdr[1] 
    if outs >= 0xC0:
        outs = (outs&0x3F)<<8 + hdr[2] 
    if ins == 0:
        cur.execute("select coinbase from blocks where id=%s;", (blkid,))
        cb = cur.fetchone()[0]
        out += [ '\x01', '\0'*32, '\xff'*4, encodeVarInt(len(cb)), cb, '\0'*4 ]
        vpos = 0
    else:
        out += encodeVarInt(ins)
        vpos = int(txdata) + hdr[0]
        buf = readBlob(vpos, ins*7, sqc.cfg['path'])
        if len(buf) < ins*7 or buf == '\0'*ins*7: # means missing blob data
            for n in range(ins):
                out += [ '\0'*32, '\0'*4, '', '', '' ]
        else:
            vpos += ins*7
            for n in range(ins):
                in_id, = unpack('<Q', buf[n*7:n*7+7]+'\0')
                cur.execute("select hash from trxs where id=%s limit 1;", (in_id / MAX_IO_TX,))
                out += [ cur.fetchone()[0][:32], pack('<I', in_id % MAX_IO_TX) ]
                vsz,off = decodeVarInt(readBlob(vpos, 9, sqc.cfg['path'])) if not hdr[7] else (0,0) # no-sigs flag
                sigbuf = readBlob(vpos, off+vsz+(0 if hdr[6] else 4), sqc.cfg['path']) 
                out += [ sigbuf[:off], sigbuf[off:off+vsz], ('\xFF'*4 if hdr[6] else sigbuf[off+vsz:]) ] 
                vpos += off+vsz+(0 if hdr[6] else 4)
    out += encodeVarInt(outs)
    for n in range(outs):
        cur.execute("select value,addr,addr_id from outputs o, address a where o.id=%s and a.id=o.addr_id limit 1;", (txid*MAX_IO_TX + n,))
        value,addr,addr_id = cur.fetchone()
        out += [ pack('<Q', int(value)) ]
        vsz,off = decodeVarInt(readBlob(vpos, 9, sqc.cfg['path']))
        pkbuf = readBlob(vpos, off+vsz, sqc.cfg['path'])
        out += [ pkbuf[:off], pkbuf[off:] ] if vsz > 0 else mkSPK(addr, addr_id)
    out += [ pack('<I', hdr[5]) ]
    return rawHTML(out, ins, outs) if 'html' in args else ''.join(out).encode('hex') 

def apiRPC(cmd, arg):
    rpc = AuthServiceProxy(sqc.cfg['rpc'])
    if cmd =='estimatefee':
        return int(rpc.estimatefee(int(arg)))
    if cmd =='send':
        return rpc.sendrawtransaction(arg)
    
def apiSync(cur, sync_req=0, timeout=30):
    from bci import bciTxWS
    if sync_req >= sqc.sync_id:
        with sqc.sync:
            sqc.sync.wait(timeout) # long polling support for sync connections
        if sync_req >= sqc.sync_id:
            return None # timeout
    if sync_req == 0 or sync_req == sqc.sync_id:
        utxs = sqc.syncTxs
    else:
        utxs = []
        cur.execute("select hash from mempool m, trxs t where m.sync_id > %s and t.id=m.id;", (sync_req,))
        for txhash, in cur:
            utxs.append(bciTxWS(cur, txhash[::-1].encode('hex')))
    cur.execute("select min(block_id) from orphans where sync_id > %s;", (sync_req if sync_req > 0 else sqc.sync_id,))
    orphan = cur.fetchone()[0]
    return { 'block':sqc.cfg['block'] if orphan == None else orphan, 'orphan':orphan != None, 'txs':utxs, 'sync_id':sqc.sync_id }

# based on the closure code from
# https://github.com/sharkcrayon/bitcoin-closure
def apiClosure(cur, addrs):
    closure,balance = [],0
    txDone = []
    while len(addrs) > 0:
        sleep(0)
        addr = addrs.pop(0)
        closure.append(addr)
        txs = apiTxs(cur, { 'address':[ addr ] })
        for tx in txs:
            if not tx['txid'] in txDone:
                if len(tx['vin']) == 1:
                    txDone.append(tx['txid'])
                else:
                    in_addrs = [ vin['addr'] for vin in tx['vin'] ]
                    if addr in in_addrs:
                        txDone.append(tx['txid'])
                        for ain in in_addrs:
                            if not ain in closure and not ain in addrs:
                                addrs.append(ain)
                            
    utxos = apiAddr(cur, closure, 'utxo', {})
    for addr in utxos:
        for utxo in addr:
            balance += utxo['amount']
    return { 'closure':closure, 'balance':balance }
    
def apiStatus(cur, cls='info', *args):
    data = {}
    cur.execute("select value from info where `class`='sys' and `key`='updated';")
    row = cur.fetchone()
    if not row or (datetime.now() - datetime.strptime(row[0],'%Y-%m-%d %H:%M:%S')).total_seconds() > 60:
        cur.execute("replace into info (class,`key`,value) values('info','block',%s);", (sqc.cfg['block'], ))
        cur.execute("replace into info (class,`key`,value) values('info','version',%s);", (version, ))
        cur.execute("replace into info (class,`key`,value) values('sys','updated',now());")
        if cls == 'db':
            total_bytes = 0
            cur.execute("show table status;")
            for tbl in cur:
                if tbl[0] not in ['blocks','trxs','address','outputs']: continue
                if tbl[6]+tbl[8] < 1e9:
                    cur.execute("replace into info (class,`key`,value) values('db','{0}:rows',%s),('db','{0}:data-MB',%s),('db','{0}:idx-MB',%s),('db','{0}:total-MB',%s),('db','{0}:total-bytes',%s);".format(tbl[0]), 
                        (tbl[4], float("%.1f"%float(tbl[6]/1e6)), float("%.1f"%float(tbl[8]/1e6)), float("%.1f"%float(tbl[6]/1e6+tbl[8]/1e6)), tbl[6]+tbl[8]))
                else:
                    cur.execute("replace into info (class,`key`,value) values('db','{0}:rows',%s),('db','{0}:data-GB',%s),('db','{0}:idx-GB',%s),('db','{0}:total-GB',%s),('db','{0}:total-bytes',%s);".format(tbl[0]), 
                        (tbl[4], float("%.1f"%float(tbl[6]/1e9)), float("%.1f"%float(tbl[8]/1e9)), float("%.1f"%float(tbl[6]/1e9+tbl[8]/1e9)), tbl[6]+tbl[8]))
                total_bytes += tbl[6]+tbl[8]
            blobs_size = getBlobsSize(sqc.cfg['path'])
            cur.execute("replace into info (class,`key`,value) values('db','outputs:max-io-tx',%s);", (MAX_IO_TX, ))
            cur.execute("replace into info (class,`key`,value) values('db','blocks:hdr-data',%s);", (os.stat(sqc.cfg['path']+'/hdrs.dat').st_size, ))
            cur.execute("replace into info (class,`key`,value) values('db','trxs:blob-data',%s);", (blobs_size, ))
            cur.execute("replace into info (class,`key`,value) values('db','trxs:blob-GB',%s);", (float("%.1f"%float(blobs_size/1e9)), ))
            cur.execute("replace into info (class,`key`,value) values('db','trxs:max-tx-block',%s);", (MAX_TX_BLK, ))
            cur.execute("replace into info (class,`key`,value) values('db','all:total-bytes',%s);", (total_bytes, ))
            cur.execute("replace into info (class,`key`,value) values('db','all:total-GB',%s);", (float("%.1f"%float(total_bytes/1e9)), ))

    cur.execute("select `key`,value from info where class=%s;", (cls, ))
    for k,v in cur:
        if ':' in k:
            k1,k2 = k.split(':', 1)
            if k1 in data:
                data[k1].update({ k2:v })
            else: 
                data[k1] = { k2:v }
        else:
            data[k] = v
    if 'html' in args:
        pass # todo wrap data as html table
    return data
    
    
