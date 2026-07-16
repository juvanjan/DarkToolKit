#!/usr/bin/env python3
# mis_to_geo.py  ---  Thief 2 .MIS -> precomputed world-space geometry JSON for Unreal.
#
#   single file : py mis_to_geo.py  mission.mis  mission_geo.json
#   whole folder: py mis_to_geo.py  --folder  INPUT_DIR  [OUTPUT_DIR]
#
# Bakes every brush's mesh with Dark's OWN rotation convention (verified against DromEd):
#   rotation matrix = Rz(Heading) @ Ry(Pitch) @ Rx(Bank),  angles = (az,ay,ax) from the record.
# Builds the mesh in Dark space (feet), then converts each vertex to Unreal (Y-negated, cm).
# Unreal never re-derives rotation -> wedges/cylinders land correctly at ANY angle.
#
# Wedge base: right-triangle in the Y-Z plane (y/hy + z/hz <= 0), extruded along X.
# Cylinder: n-gon prism, +90 deg facet phase.  Winding repaired to consistent outward.

import struct, math, json, sys, os
from collections import Counter, defaultdict
import numpy as np

SCALE   = 30.48
KEEP_OPS = (0,1,2,3,4,5,8)

def Rz(a): c,s=math.cos(a),math.sin(a); return np.array([[c,-s,0],[s,c,0],[0,0,1]])
def Ry(a): c,s=math.cos(a),math.sin(a); return np.array([[c,0,s],[0,1,0],[-s,0,c]])
def Rx(a): c,s=math.cos(a),math.sin(a); return np.array([[1,0,0],[0,c,-s],[0,s,c]])
def Mdark(H,P,B): return Rz(math.radians(H)) @ Ry(math.radians(P)) @ Rx(math.radians(B))

# -- local shapes in Dark coords, half-extents h=(hx,hy,hz) --------------------------------------
def box_local(h):
    hx,hy,hz=h
    V=[(-hx,-hy,-hz),(hx,-hy,-hz),(hx,hy,-hz),(-hx,hy,-hz),(-hx,-hy,hz),(hx,-hy,hz),(hx,hy,hz),(-hx,hy,hz)]
    T=[(0,3,2),(0,2,1),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    return V,T
def wedge_local(h):                       # triangle in Y-Z (y/hy+z/hz<=0), extruded along X
    hx,hy,hz=h
    V=[(-hx,-hy,-hz),(-hx,hy,-hz),(-hx,-hy,hz),(hx,-hy,-hz),(hx,hy,-hz),(hx,-hy,hz)]
    T=[(2,1,0),(3,4,5),(0,1,4),(0,4,3),(1,2,5),(1,5,4),(2,0,3),(2,3,5)]
    return V,T
def cyl_local(h,sides):                    # n-gon prism, +90 deg phase
    hx,hy,hz=h; n=max(3,int(sides))
    ang=[2*math.pi*k/n+math.pi/2 for k in range(n)]
    top=[(hx*math.cos(a),hy*math.sin(a), hz) for a in ang]
    bot=[(hx*math.cos(a),hy*math.sin(a),-hz) for a in ang]
    V=top+bot; T=[]
    for k in range(1,n-1): T.append((0,k,k+1)); T.append((n,n+k+1,n+k))
    for k in range(n):
        a,b=k,(k+1)%n; T.append((a,b,n+b)); T.append((a,n+b,n+a))
    return V,T

def signed_volume(V,T):
    s=0.0
    for a,b,c in T:
        x,y,z=V[a],V[b],V[c]
        s+=x[0]*(y[1]*z[2]-y[2]*z[1])-x[1]*(y[0]*z[2]-y[2]*z[0])+x[2]*(y[0]*z[1]-y[1]*z[0])
    return s/6.0
def orient(V,T):
    e2t=defaultdict(list)
    for ti,t in enumerate(T):
        a,b,c=t
        for u,v in ((a,b),(b,c),(c,a)): e2t[frozenset((u,v))].append(ti)
    T=[list(t) for t in T]; seen=[False]*len(T)
    def de(t): a,b,c=t; return {(a,b),(b,c),(c,a)}
    for st in range(len(T)):
        if seen[st]: continue
        seen[st]=True; stack=[st]
        while stack:
            ti=stack.pop()
            for ed in de(T[ti]):
                for tj in e2t[frozenset(ed)]:
                    if seen[tj]: continue
                    seen[tj]=True
                    if ed in de(T[tj]): T[tj]=[T[tj][0],T[tj][2],T[tj][1]]
                    stack.append(tj)
    if signed_volume(V,T)<0: T=[[a,c,b] for a,b,c in T]
    return [tuple(t) for t in T]

def classify(nf):
    if nf>=7: return "cylinder",nf-2
    if nf==6: return "box",0
    if nf==5: return "wedge",0
    return "box",0

def bake(b):
    h=b["half"]
    if   b["shape"]=="cylinder": V,T=cyl_local(h,b["sides"])
    elif b["shape"]=="wedge":    V,T=wedge_local(h)
    else:                        V,T=box_local(h)
    R=Mdark(b["H"],b["P"],b["B"]); pos=np.array(b["pos"])
    Wd=(R@np.array(V,dtype=float).T).T + pos            # Dark space (feet)
    Wue=Wd*np.array([1.0,-1.0,1.0])*SCALE               # -> Unreal (Y-negated, cm)
    Wue=Wue.tolist()
    T=orient(Wue,T)                                     # outward winding (Y-flip inverts; repair)
    return [[round(x,3) for x in p] for p in Wue], [list(t) for t in T]

def read_txlist(chunks, f):
    """TXLIST -> list of texture names (index -> name). index 0 is usually 'null'."""
    if "TXLIST" not in chunks: return []
    o,l=chunks["TXLIST"]; d=f[o+24:o+24+l]
    _,ntex,nfam=struct.unpack_from("<III",d,0)
    p=12+nfam*16; names=[]
    for _ in range(ntex):
        names.append(d[p+4:p+20].split(b"\x00")[0].decode("latin1")); p+=20
    return names

# -- per-face texture assignment ------------------------------------------------------------------
# Dark stores one texture id per brush face, in a fixed per-shape record order. We map each record
# slot to a LOCAL face normal, then bake that normal into UE space (F @ R @ n) so the Unreal side can
# assign the material to whichever built face matches - independent of vertex/triangle ordering.
#   *** FACE-ORDER CONVENTION (verified vs DromEd, mission 09 brush 2) ***
#   box slots:  0:+Y(N) 1:+X(E) 2:-Y(S) 3:-X(W) 4:+Z(Up) 5:-Z(Down)
#   cylinder:   sides use slot 0 (side template); +Z cap = slot 4, -Z cap = slot 5
#   wedge:      0:+X cap 1:-X cap 2:-Y leg 3:-Z leg 4:hypotenuse
F_REFLECT=np.array([1.0,-1.0,1.0])

def _slot_for(shape, nl):
    ax=int(np.argmax(np.abs(nl))); sgn=1 if nl[ax]>0 else -1
    if shape=="cylinder":
        if ax==2: return 4 if sgn>0 else 5     # cap
        return 0                                # any side -> side template slot
    if shape=="wedge":
        if ax==0: return 0 if sgn>0 else 1      # +X / -X caps
        if nl[1]<-0.3 and abs(nl[2])<0.3: return 2   # -Y leg
        if nl[2]<-0.3 and abs(nl[1])<0.3: return 3   # -Z leg
        return 4                                # hypotenuse (mixed +Y/+Z normal)
    # box: Dark cube face order (verified vs DromEd COORDINATE readout, 4-distinct-wall brush):
    #   0:-X  1:-Y  2:+X  3:+Y  4:+Z  5:-Z
    return {(0,-1):0,(1,-1):1,(0,1):2,(1,1):3,(2,1):4,(2,-1):5}[(ax,sgn)]

def faces_for(b, names):
    """Return [{n:[ux,uy,uz], tex:name|None}] - one per distinct face, in UE space."""
    h=b["half"]
    if   b["shape"]=="cylinder": V,T=cyl_local(h,b["sides"])
    elif b["shape"]=="wedge":    V,T=wedge_local(h)
    else:                        V,T=box_local(h)
    R=Mdark(b["H"],b["P"],b["B"])
    ftex=b.get("ftex",[]); fscl=b.get("fscl",[]); frot=b.get("frot",[]); fuof=b.get("fuof",[]); fvof=b.get("fvof",[])
    def texname(idx):
        if 0<=idx<len(names):
            nm=names[idx]
            return None if nm.lower()=="null" else nm
        return None
    bdef=texname(b.get("deftex",-1))                        # brush default texture (offset 8)
    def resolve(slot):
        if slot<0 or slot>=len(ftex): return bdef
        return texname(ftex[slot]) or bdef                  # -1 / invalid face -> the brush default
    def resolve_scale(slot):
        # RAW Dark scale value (power-of-2 exponent). World tile = pixels * 2^(scale-20) feet.
        if slot<0 or slot>=len(fscl): return 16
        v=fscl[slot]
        return int(v) if 0<v<=64 else 16
    def resolve_rot(slot):
        if slot<0 or slot>=len(frot): return 0.0
        return (frot[slot]*360.0/65536.0)                   # 16-bit angle -> degrees
    def resolve_off(arr,slot):
        if slot<0 or slot>=len(arr): return 0.0
        return arr[slot]/64.0                               # texel offset -> UV fraction (calibratable)
    groups={}
    for t in T:
        p,q,r=(np.array(V[t[i]],dtype=float) for i in range(3))
        n=np.cross(q-p,r-p); L=np.linalg.norm(n)
        if L<1e-9: continue
        nl=n/L; key=tuple(np.round(nl,3))
        groups.setdefault(key, nl)
    faces=[]
    for nl in groups.values():
        slot=_slot_for(b["shape"], nl)
        nue=F_REFLECT*(R@nl); nue=nue/ (np.linalg.norm(nue) or 1.0)
        faces.append(dict(n=[round(float(x),4) for x in nue], tex=resolve(slot),
                          sc=resolve_scale(slot), rot=round(resolve_rot(slot),3),
                          uoff=round(resolve_off(fuof,slot),4), voff=round(resolve_off(fvof,slot),4)))
    return faces

# -- Dark texture inheritance ---------------------------------------------------------------------
# A face texture id of -1 means "inherit the BRUSH'S DEFAULT texture", which Dark stores in the brush
# header at offset 8 (verified against DromEd). faces_for() already applies that default per face, so
# here we only need a last-resort fill for the rare brush whose default itself is invalid (e.g. 249).
def resolve_inheritance(B, allfaces):
    cnt=Counter(f["tex"] for fs in allfaces for f in fs if f.get("tex"))
    global_def=cnt.most_common(1)[0][0] if cnt else None
    for faces in allfaces:
        for f in faces:
            if f.get("tex") is None: f["tex"]=global_def

def extract(path):
    f=open(path,"rb").read()
    toc=struct.unpack_from("<I",f,0)[0]; cnt=struct.unpack_from("<I",f,toc)[0]
    p=toc+4; chunks={}
    for _ in range(cnt):
        nm=f[p:p+12].split(b"\x00")[0].decode("latin1"); off,ln=struct.unpack_from("<II",f,p+12); chunks[nm]=(off,ln); p+=20
    names=read_txlist(chunks, f)
    o,l=chunks["BRLIST"]; d=f[o+24:o+24+l]; N=len(d)
    # BRLIST is a SEQUENTIAL list of brush records (76-byte header + trailing data). Terrain brushes
    # (media/op 0-8) carry a face array -> record = 76 + nfaces*10. Non-terrain brushes (objects,
    # lights, rooms, flow, area; media 255 etc.) have NO faces -> flat 76 bytes; their byte-67 must be
    # ignored. Walking with this rule lands exactly on the chunk end and captures EVERY brush.
    def dg(a): return a*360.0/65536.0
    out=[]; p=0
    while p+76<=N:
        op=d[p+10]; nf=d[p+67]
        terrain = (op<=8) and (4<=nf<=64)         # terrain fill op + a plausible face count
        if not terrain:
            p+=76; continue                        # non-terrain brush -> skip, fixed 76 bytes
        if p+76+nf*10>N: break                     # truncated / misaligned safety
        if op in KEEP_OPS:
            bid,tm=struct.unpack_from("<hh",d,p)
            deftex=struct.unpack_from("<h",d,p+8)[0]         # brush's DEFAULT texture (what -1 faces inherit)
            x,y,z,sx,sy,sz=struct.unpack_from("<6f",d,p+12)
            ax,ay,az=struct.unpack_from("<3H",d,p+36)
            q0=p+76
            ftex=[struct.unpack_from("<h",d,q0+k*10)[0]   for k in range(nf)]   # +0 texture id (-1=inherit)
            frot=[struct.unpack_from("<H",d,q0+k*10+2)[0] for k in range(nf)]   # +2 rotation (16-bit angle)
            fscl=[struct.unpack_from("<H",d,q0+k*10+4)[0] for k in range(nf)]   # +4 scale (16 = 1x)
            fuof=[struct.unpack_from("<H",d,q0+k*10+6)[0] for k in range(nf)]   # +6 U offset
            fvof=[struct.unpack_from("<H",d,q0+k*10+8)[0] for k in range(nf)]   # +8 V offset
            shape,sides=classify(nf)
            if all(math.isfinite(v) for v in (x,y,z,sx,sy,sz)) and min(sx,sy,sz)>0.0:
                out.append(dict(id=bid,time=tm,op=int(op),shape=shape,sides=sides,deftex=deftex,
                                pos=(x,y,z),half=(sx,sy,sz),H=dg(az),P=dg(ay),B=dg(ax),
                                ftex=ftex,fscl=fscl,frot=frot,fuof=fuof,fvof=fvof))
        p+=76+nf*10
    out.sort(key=lambda b:b["time"])
    return out, names

def convert(inp, outp):
    B,names=extract(inp)
    allfaces=[faces_for(b,names) for b in B]
    resolve_inheritance(B, allfaces)          # Dark rule: -1 faces inherit the solid they carve into
    brushes=[]; allv=[]
    for b,faces in zip(B,allfaces):
        V,T=bake(b)
        brushes.append(dict(id=b["id"],time=b["time"],op=b["op"],shape=b["shape"],
                            verts=V,tris=T,faces=faces))
        allv+=V
    if not allv: raise ValueError("no terrain brushes found")
    # world solid box (encloses everything) as brush 0
    a=np.array(allv); mn=a.min(0); mx=a.max(0); pad=SCALE*8
    c=(mn+mx)/2; hh=(mx-mn)/2+pad
    Vw=[(c[0]+sx*hh[0],c[1]+sy*hh[1],c[2]+sz*hh[2]) for sx in(-1,1) for sy in(-1,1) for sz in(-1,1)]
    def boxtris():
        idx={}
        for i,k in enumerate([(sx,sy,sz) for sx in(-1,1) for sy in(-1,1) for sz in(-1,1)]): idx[k]=i
        faces=[]
        for ax in range(3):
            for sgn in(-1,1):
                quad=[k for k in idx if k[ax]==sgn]
                o1,o2=[i for i in range(3) if i!=ax]
                quad.sort(key=lambda k:(k[o1],k[o2]))
                a2,b2,c2,d2=quad[0],quad[1],quad[3],quad[2]
                faces.append((idx[a2],idx[b2],idx[c2])); faces.append((idx[a2],idx[c2],idx[d2]))
        return faces
    Tw=orient(Vw,boxtris())
    world=dict(id=0,time=0,op=0,shape="box",verts=[[round(x,3) for x in v] for v in Vw],tris=[list(t) for t in Tw],faces=[])
    brushes=[world]+brushes
    json.dump(dict(units="cm",note="world-space verts baked with Dark rotation Rz(H)Ry(P)Rx(B), Y-negated to UE",
                   brushes=brushes),open(outp,"w"))
    return len(brushes), dict(sorted(Counter(b['op'] for b in brushes).items()))

def main():
    a=sys.argv[1:]
    folder = bool(a) and (a[0] in ("--folder","-f") or os.path.isdir(a[0]))
    if not a or (not folder and len(a)<2):
        print("usage:\n"
              "  single file : py mis_to_geo.py  input.mis  output_geo.json\n"
              "  whole folder: py mis_to_geo.py  --folder  INPUT_DIR  [OUTPUT_DIR]")
        return
    if folder:
        idir = a[1] if a[0] in ("--folder","-f") else a[0]
        rest = a[2:] if a[0] in ("--folder","-f") else a[1:]
        odir = rest[0] if rest else idir
        os.makedirs(odir, exist_ok=True)
        files=[f for f in sorted(os.listdir(idir)) if f.lower().endswith(".mis")]
        if not files: print("no .mis files found in", idir); return
        ok=0
        for f in files:
            stem=os.path.splitext(f)[0]
            out=os.path.join(odir, stem+"_geo.json")
            try:
                n,ops=convert(os.path.join(idir,f), out)
                print("  OK   %-34s -> %s  (%d brushes, ops %s)"%(f, stem+"_geo.json", n, ops)); ok+=1
            except Exception as e:
                print("  FAIL %-34s : %s"%(f, e))
        print("done: %d/%d converted -> %s"%(ok, len(files), odir))
    else:
        n,ops=convert(a[0], a[1])
        print("%s -> %s"%(a[0].split('/')[-1], a[1].split('/')[-1]))
        print("  brushes: %d   ops: %s"%(n, ops))

if __name__=="__main__":
    main()