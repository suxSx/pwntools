from pwn.internal.shellcode_helper import *

@shellcode_reqs(arch=['i386', 'amd64'])
def pushstr(string, null = True, arch = None):
    '''Args: string [null = True]

    Pushes a string to the stack. If null is True, then it also
    null-terminates the string.

    On amd64 clobbers rax for most strings longer than 4 bytes.
'''

    if null:
        string += '\x00'

    if not string:
        return ''

    def fix(out):
        return '\n'.join('    ' + s for s in out)

    if arch == 'i386':
        return fix(_pushstr_i386(string))
    elif arch == 'amd64':
        return fix(_pushstr_amd64(string))
    bug("OS/arch combination (%s, %s) not supported for pushstr" % (os, arch))

def _pushstr_i386(string):
    out = []

    if ord(string[-1]) >= 128:
        extend = '\xff'
    else:
        extend = '\x00'

    string = string.ljust(align(4, len(string)), extend)

    for s in [flat(s) for s in group(string, 4)]:
        n = u32(s)
        sign = n - (2 * (n & 2**31))

        if n == 0:
            out.append('push 1 ; %s' % (repr(s)))
            out.append('dec byte [esp]')
        elif -128 <= sign < 128 or '\x00' not in s:
            out.append('push %s ; %s' % (hex(n), repr(s)))
        else:
            a,b = xor_pair(s, avoid = '\x00')
            out.append('push %s' % hex(u32(a)))
            out.append('xor dword [esp], %s ; %s' % (hex(u32(b)), repr(s)))
    return out

def _pushstr_amd64(string):
    out = []

    if ord(string[-1]) >= 128:
        extend = '\xff'
    else:
        extend = '\x00'

    string = string.ljust(align(8, len(string)), extend)

    for s in [flat(s) for s in group(string, 8)]:
        n = u64(s)
        sign = n - (2 * (n & 2**63))

        if n == 0:
            out.append('push 1 ; %s' % (repr(s)))
            out.append('dec byte [esp]')
        elif -128 <= sign < 128 or '\x00' not in s:
            out.append('push %s ; %s' % (hex(n), repr(s)))
        else:
            if s[4:] == '\xff' * 4 if sign<0 else '\x00'*4:
                a,b = xor_pair(s[:4], avoid = '\x00')
                a = u32(a)
                b = u32(b)
                a, b = max(a,b), min(a,b)
                out.append('push %s' % hex(a))
                out.append('xor dword [rsp], %s ; %s' % (hex(b), repr(s)))
            else:
                a,b = xor_pair(s, avoid = '\x00')
                out.append('mov rax %s' % hex(u64(a)))
                out.append('xor rax, %s ; %s' % (hex(u64(b)), repr(s)))
                out.append('push rax')
    return out