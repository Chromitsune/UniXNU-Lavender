import json, socket, sys, base64
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])
f = s.makefile('rw')
f.readline()  # greeting
def cmd(c, **kw):
    f.write(json.dumps({'execute': c, 'arguments': kw})+'\n'); f.flush()
    while True:
        r = json.loads(f.readline())
        if 'return' in r or 'error' in r: return r
print(cmd('qmp_capabilities'))
print(cmd('screendump', filename='/tmp/esp-lavender/fb.ppm'))
