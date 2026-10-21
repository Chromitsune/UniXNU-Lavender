import json, socket, sys, time
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])
f = s.makefile('rw')
f.readline()
def send(c, **kw):
    f.write(json.dumps({'execute': c, 'arguments': kw})+'\n'); f.flush()
def recv():
    while True:
        r = json.loads(f.readline())
        if 'return' in r or 'error' in r: return r
send('qmp_capabilities'); recv()
send('human-monitor-command', **{'command-line':'info registers'})
r = recv()
print(json.dumps(r)[:6000])
