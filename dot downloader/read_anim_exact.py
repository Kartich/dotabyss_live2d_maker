import struct, pathlib, json
from parse_anim import parse_sf
class R:
 def __init__(self,b):self.b=b;self.p=0
 def align(self):self.p=(self.p+3)&~3
 def i(self):x=struct.unpack_from('<i',self.b,self.p)[0];self.p+=4;return x
 def u(self):x=struct.unpack_from('<I',self.b,self.p)[0];self.p+=4;return x
 def f(self):x=struct.unpack_from('<f',self.b,self.p)[0];self.p+=4;return x
 def b1(self):x=self.b[self.p];self.p+=1;return x
 def i64(self):x=struct.unpack_from('<q',self.b,self.p)[0];self.p+=8;return x
 def str(self):
  n=self.i(); s=self.b[self.p:self.p+n].decode('utf8','replace');self.p+=n;self.align();return s
 def arr(self,fn):
  n=self.i();
  if n<0 or n>2000000: raise ValueError(('bad arr',n,self.p))
  return [fn() for _ in range(n)]
 def vec3(self):return (self.f(),self.f(),self.f())
 def vec4(self):return (self.f(),self.f(),self.f(),self.f())
 def xform(self):return (self.vec3(),self.vec4(),self.vec3())
 def pptr(self):return (self.i(),self.i64())

def read_human(r):
 r.xform();r.vec3();r.vec4()
 goals=r.arr(lambda: (r.xform(),r.f(),r.f(),r.vec3(),r.f()))
 # hand pose
 for _ in range(2):
  r.xform();r.arr(r.f);r.f();r.f();r.f();r.f()
 r.arr(r.f)
 r.arr(r.vec3)
 return len(goals)

def read_anim(d):
 r=R(d); name=r.str(); legacy=r.b1(); comp=r.b1();hq=r.b1();r.align()
 for label,fn in [('rotation',lambda: (r.arr(lambda: read_curve(r, r.vec4)), r.str()))]:pass
 def animcurve(reader, readval):
  return reader.arr(lambda: (reader.f(),readval(),readval(),readval(),reader.i(),readval(),readval())) and None
 # arrays - use dedicated to consume
 nrot=r.i()
 for _ in range(nrot):
  read_animation_curve(r, r.vec4);r.str()
 ncr=r.i()
 for _ in range(ncr): read_compressed(r)
 ne=r.i()
 for _ in range(ne): read_animation_curve(r,r.vec3);r.str()
 np=r.i()
 for _ in range(np): read_animation_curve(r,r.vec3);r.str()
 ns=r.i()
 for _ in range(ns): read_animation_curve(r,r.vec3);r.str()
 nf=r.i()
 floats=[]
 for _ in range(nf):
  curve=read_animation_curve(r,r.f)
  # Unity AnimationClip.m_FloatCurves binding fields are UInt32 path/attribute
  # hashes in the serialized binary, not strings. Reading them as strings
  # corrupts the following fields and makes the exact FloatCurve bindings
  # unusable (notably the path=0 Model tracks Opacity/EyeBlink/LipSync).
  attr=r.u(); path=r.u(); cid=r.i(); script=r.pptr()
  floats.append({
      "attribute": int(attr),
      "path": int(path),
      "typeID": int(cid),
      "curve": curve,
  })
 npp=r.i()
 for _ in range(npp):
  kc=r.i()
  for j in range(kc): r.f();r.pptr()
  r.str();r.str();r.i();r.pptr()
 sr=r.f(); wrap=r.i(); r.vec3();r.vec3() # bounds center extent
 msize=r.u(); start_mc=r.p
 # ClipMuscleConstant
 goals=read_human(r)
 for _ in range(4):r.xform()
 avg=r.vec3()
 # Clip
 streamed=r.arr(r.u); curveCount=r.u()
 frameCount=r.i(); dcurve=r.u(); dsr=r.f(); begin=r.f(); dense=r.arr(r.f)
 const=r.arr(r.f)
 start=r.f(); stop=r.f(); ori=r.f(); level=r.f(); cyc=r.f(); avgang=r.f()
 idxarr=r.arr(r.i)
 nd=r.i(); deltas=[(r.f(),r.f()) for _ in range(nd)]
 refpose=r.arr(r.f)
 bools=[r.b1() for _ in range(10)]; r.align()
 mclip_end=r.p
 nb=r.i(); bindings=[]
 for _ in range(nb):
  path=r.u(); attr=r.u(); script=r.pptr(); typeid=r.i(); custom=r.b1(); isp=r.b1(); isint=r.b1(); isser=r.b1(); r.align(); bindings.append((path,attr,typeid))
 nm=r.i(); mappings=[r.pptr() for _ in range(nm)]
 hasroot=r.b1(); hasfloat=r.b1(); # no explicit align? next array size likely aligned? type tree mf indicates m_HasMotionFloatCurves 16400 aligns after it
 r.align()
 nevt=r.i(); events=[]
 for _ in range(nevt):
  t=r.f();fn=r.str();dat=r.str();obj=r.pptr();fp=r.f();ip=r.i();mo=r.i();events.append((t,fn,dat))
 return locals()

def read_animation_curve(r,readval):
 n=r.i()
 keys=[]
 for _ in range(n):
  t=r.f()
  value=readval()
  in_slope=readval()
  out_slope=readval()
  weighted_mode=r.i()
  in_weight=readval()
  out_weight=readval()
  keys.append({
      "time": float(t),
      "value": float(value) if isinstance(value, (int,float)) else value,
      "inSlope": float(in_slope) if isinstance(in_slope, (int,float)) else in_slope,
      "outSlope": float(out_slope) if isinstance(out_slope, (int,float)) else out_slope,
      "weightedMode": int(weighted_mode),
      "inWeight": float(in_weight) if isinstance(in_weight, (int,float)) else in_weight,
      "outWeight": float(out_weight) if isinstance(out_weight, (int,float)) else out_weight,
  })
 pre=r.i(); post=r.i(); rotation_order=r.i()
 return {
     "keys": keys,
     "preInfinity": int(pre),
     "postInfinity": int(post),
     "rotationOrder": int(rotation_order),
 }

def read_compressed(r):
 r.str();
 # PackedIntVector
 r.u();n=r.i();r.raw(n) if False else None
 # implement bytes with align
 if n<0 or n>10000000: raise ValueError('bad compressed')
 r.p+=n;r.align();r.b1();r.align()
 # PackedQuatVector
 r.u();n=r.i();r.p+=n;r.align();r.b1();r.align()
 # PackedFloatVector
 r.u();r.f();r.f();n=r.i();r.p+=n;r.align();r.b1();r.align();
 r.i();r.i()


if __name__ == "__main__":
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("serialized_file")
    a=ap.parse_args()
    raw=pathlib.Path(a.serialized_file).read_bytes()
    unity,types,objs=parse_sf(raw)
    cands=[x for x in objs if x["cid"]==74]
    print("unity",unity,"objs",len(objs),"anim clips",len(cands))
    for o in cands[:3]:
        try:
            v=read_anim(raw[o["off"]:o["off"]+o["size"]])
            print("OK",v["name"],o["size"])
        except Exception as e:
            print("ERR",o["size"],e)
