import struct, pathlib, json, math, sys

def align4(p): return (p+3)&~3

def cstring(b,p):
 q=b.find(b'\0',p); return b[p:q].decode('utf8','replace'),q+1

# known builtin string indices used in Unity typetree in this file
T={
 76:'bool',81:'char',161:'float',222:'int',814:'long',907:'short',928:'bool',934:'uint',
 981:'Array',840:'string',616:'PPtr',633:'PPtr',741:'Vector4',997:'Vector3',1051:'uint',0:'struct',
 19:'AnimationCurve',827:'Array',49:'ArrayItem',794:'', 564:'PPtr',
}

def parse_sf(raw):
 p=16; endian=raw[p]; p+=4
 meta=struct.unpack_from('>I',raw,p)[0];p+=4
 file_size=struct.unpack_from('>q',raw,p)[0];p+=8
 dataoff=struct.unpack_from('>q',raw,p)[0];p+=8
 p+=8
 unity,p=cstring(raw,p); target=struct.unpack_from('<i',raw,p)[0];p+=4
 ett=raw[p];p+=1; tc=struct.unpack_from('<i',raw,p)[0];p+=4
 types=[]
 for ti in range(tc):
  cid=struct.unpack_from('<i',raw,p)[0];p+=4; stripped=raw[p];p+=1; script=struct.unpack_from('<h',raw,p)[0];p+=2
  if cid==114:p+=16
  p+=16
  nn,sb=struct.unpack_from('<ii',raw,p);p+=8
  nodes=[]
  for i in range(nn):
   ver,lev,flags=struct.unpack_from('<HBB',raw,p);p+=4;to,no=struct.unpack_from('<II',raw,p);p+=8;bs,idx,mf=struct.unpack_from('<iii',raw,p);p+=12;rh=struct.unpack_from('<Q',raw,p)[0];p+=8
   nodes.append(dict(ti=ti,ver=ver,level=lev,flags=flags,to=to,no=no,bs=bs,idx=idx,mf=mf))
  sbuf=raw[p:p+sb];p+=sb
  def rs(off):
   if off & 0x80000000:return f'<b:{off&0x7fffffff}>'
   z=sbuf.find(b'\0',off); return sbuf[off:z].decode('utf8','replace')
  for n in nodes:n['type']=rs(n.pop('to'));n['name']=rs(n.pop('no'))
  dc=struct.unpack_from('<i',raw,p)[0];p+=4+dc*4
  types.append((cid,nodes))
 oc=struct.unpack_from('<i',raw,p)[0];p+=4; objs=[]
 for _ in range(oc):
  p=align4(p);pid=struct.unpack_from('<q',raw,p)[0];p+=8;bs=struct.unpack_from('<q',raw,p)[0];p+=8;sz=struct.unpack_from('<I',raw,p)[0];p+=4;ti=struct.unpack_from('<i',raw,p)[0];p+=4
  objs.append(dict(path=pid,off=dataoff+bs,size=sz,cid=types[ti][0],ti=ti,nodes=types[ti][1]))
 return unity,types,objs

class R:
 def __init__(self,b):self.b=b;self.p=0
 def rem(self):return len(self.b)-self.p
 def align(self,n=4):self.p=(self.p+n-1)&~(n-1)
 def raw(self,n):
  x=self.b[self.p:self.p+n];self.p+=n;return x
 def i32(self):return struct.unpack_from('<i',self.b,self.p)[0] if not self._adv(4) else None
 def _adv(self,n):
  old=self.p;self.p+=n;return False
 def u32(self):x=struct.unpack_from('<I',self.b,self.p)[0];self.p+=4;return x
 def i64(self):x=struct.unpack_from('<q',self.b,self.p)[0];self.p+=8;return x
 def u64(self):x=struct.unpack_from('<Q',self.b,self.p)[0];self.p+=8;return x
 def f32(self):x=struct.unpack_from('<f',self.b,self.p)[0];self.p+=4;return x
 def u16(self):x=struct.unpack_from('<H',self.b,self.p)[0];self.p+=2;return x
 def u8(self):x=self.b[self.p];self.p+=1;return x

def typekind(n):
 t=n['type']
 if t.startswith('<b:'):
  t = T.get(int(t[3:-1]), 'unknown')
 else:
  t = str(t)

 # Unity TypeTree files use several spellings for primitive numeric types
 # depending on the serialized-file/type-tree version. Normalize them here
 # so AnimationClip binding hashes (path/attribute) are read as UInt32/Int32
 # instead of falling through to the generic-struct reader and becoming 0.
 key = t.strip().casefold().replace(' ', '').replace('_', '')
 aliases = {
  'uint': 'uint', 'uint32': 'uint', 'unsignedint': 'uint',
  'unsignedinteger': 'uint', 'ulong32': 'uint',
  'int': 'int', 'int32': 'int', 'signedint': 'int',
  'short': 'short', 'int16': 'short', 'signedshort': 'short',
  'ushort': 'short', 'uint16': 'short',
  'long': 'long', 'int64': 'long', 'signedlong': 'long',
  'float': 'float', 'single': 'float',
  'bool': 'bool', 'boolean': 'bool',
  'char': 'char', 'byte': 'char', 'uint8': 'char', 'ubyte': 'char',
 }
 return aliases.get(key, t)

def make_tree(nodes):
 root={'type':'ROOT','name':'ROOT','level':-1,'children':[]}
 stack=[root]
 for n in nodes:
  nn={**n,'children':[]}
  while stack and stack[-1]['level']>=n['level']:stack.pop()
  stack[-1]['children'].append(nn);stack.append(nn)
 return root

def read_node(r,n,depth=0):
 k=typekind(n); ch=n['children']
 # Arrays: size followed by elements. Detect by type index 981/827 or name Array-ish with two children (size, data)
 if k=='Array':
  size=struct.unpack_from('<i',r.b,r.p)[0];r.p+=4
  r.align(4) if (n['mf'] & 0x4000) else None
  elem=ch[-1] if ch else None
  vals=[]
  if size<0 or size>2000000: raise ValueError(f'bad array size {size} at {r.p}')
  for _ in range(size): vals.append(read_node(r,elem,depth+1))
  return vals
 if k=='string':
  ln=struct.unpack_from('<i',r.b,r.p)[0];r.p+=4
  s=r.raw(ln).decode('utf8','replace') if ln>0 else ''
  r.align(4)
  return s
 if k in ('float',):
  v=r.f32()
 elif k in ('int',):v=r.i32()
 elif k in ('uint',):v=r.u32()
 elif k in ('short',):v=r.u16()
 elif k in ('long',):v=r.i64()
 elif k=='bool':v=bool(r.u8())
 elif k=='char':v=r.u8()
 elif k in ('Vector3','Vector4'):
  vals=[read_node(r,c,depth+1) for c in ch]
  v=vals
 elif k=='PPtr':
  fid=r.i32(); pid=r.i64(); v={'fileID':fid,'pathID':pid}
 elif k=='AnimationCurve':
  # recurse; this is struct containing curve array, pre/post, rotation order
  v={}
  for c in ch:v[c['name']]=read_node(r,c,depth+1)
 else:
  # struct/unknown
  v={}
  for c in ch:
   try:v[c['name']]=read_node(r,c,depth+1)
   except Exception as e:
    raise RuntimeError(f'while reading {n["name"]}/{c["name"]} at {r.p}: {e}')
 # align if AlignBytes flag
 if n['mf'] & 0x4000:r.align(4)
 return v



if __name__ == '__main__':
    # Optional standalone parser smoke-test only. Never execute during import.
    import argparse
    parser = argparse.ArgumentParser(description='Parse a Unity SerializedFile containing AnimationClip objects.')
    parser.add_argument('serialized_file', nargs='?', help='Path to a SerializedFile/.cab')
    args = parser.parse_args()
    if not args.serialized_file:
        raise SystemExit('Usage: python parse_anim.py <SerializedFile/.cab>')
    bundle = pathlib.Path(args.serialized_file)
    raw = bundle.read_bytes()
    unity, types, objs = parse_sf(raw)
    print('unity', unity, 'objs', len(objs))
    cands = [o for o in objs if o['cid'] == 74]
    print('anim clips', len(cands))
    for o in cands[:3]:
        d = raw[o['off']:o['off'] + o['size']]
        r = R(d)
        root = make_tree(o['nodes'])
        try:
            val = read_node(r, root['children'][0])
            print('PATH', o['path'], 'size', o['size'], 'consumed', r.p, 'name', val.get('m_Name'))
            print('keys', list(val.keys()))
        except Exception as e:
            print('ERR', e, 'pos', r.p, 'rem', r.rem())
