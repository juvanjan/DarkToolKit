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

# Dark's cylinder side-record phase. The geometric side that our cyl_local() builds at angular
# slot k does NOT carry record slot k: Dark's record order is rotated a fixed number of facets
# around the ring (and the world Y-reflection folds into it). Calibrated vs DromEd ground truth
# (mission 10, 14-gon: record slot 8 'clnbrik1' lands exactly at UE +X, slot 0 'hewn3' at UE -X),
# which reproduces the observed "+6 facet" shift: record slot j appears at physical position j+6.
# Expressed as a fraction of the ring so it scales with side count (8/14 here). If a cylinder with
# a different side count ever mismaps, this single knob is what to recalibrate.
CYL_SLOT_PHASE_FRAC = 7.0/14.0

def _cyl_slot(nl, sides):
    # Dark cylinder record order (verified vs DromEd): each SIDE has its own texture.
    #   top cap (+Z) = slot `sides`;  bottom cap (-Z) = slot `sides+1`.
    if abs(nl[2])>0.9:
        return sides if nl[2]>0 else sides+1
    step=2*math.pi/sides
    k=round((math.atan2(nl[1],nl[0])-math.pi/2)/step - 0.5)
    off=int(round(CYL_SLOT_PHASE_FRAC*sides))      # phase of Dark's record order vs our geometry
    return int((k - off) % sides)

def _slot_for(shape, nl):
    ax=int(np.argmax(np.abs(nl))); sgn=1 if nl[ax]>0 else -1
    if shape=="cylinder":
        if ax==2: return 4 if sgn>0 else 5     # (unused: cylinders routed to _cyl_slot in faces_for)
        return 0
    if shape=="wedge":
        # Dark wedge record order (verified vs DromEd): 0 hypotenuse(top slant), 1 -Z(bottom),
        #   2 -Y(vertical side), 3 +X cap, 4 -X cap.  (If the two triangle caps come out swapped,
        #   flip the 3/4 return.)
        if ax==0: return 3 if sgn>0 else 4           # +X cap -> 3, -X cap -> 4
        if nl[2]<-0.3 and abs(nl[1])<0.3: return 1   # -Z (bottom, horizontal rect)
        if nl[1]<-0.3 and abs(nl[2])<0.3: return 2   # -Y (side1, vertical rect)
        return 0                                     # hypotenuse (top, slanted; mixed +Y/+Z normal)
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
        if slot<0 or slot>=len(arr): return 0
        return int(arr[slot])                               # RAW texel offset; /texture_px in UE -> UV fraction
    Va=[np.array(v,dtype=float) for v in V]
    pos=np.array(b["pos"],dtype=float)
    def to_ue(v): return ((R@v+pos)*F_REFLECT)*SCALE          # local -> UE world (cm)
    groups={}
    for t in T:
        p,q,r=Va[t[0]],Va[t[1]],Va[t[2]]
        n=np.cross(q-p,r-p); L=np.linalg.norm(n)
        if L<1e-9: continue
        nl=n/L; key=tuple(np.round(nl,3))
        g=groups.get(key)
        if g is None: groups[key]=[nl,set(t)]
        else: g[1].update(t)
    nsides=int(b.get("sides",8))
    faces=[]
    for nl,vidx in groups.values():
        cap=0
        if b["shape"]=="cylinder":
            slot=_cyl_slot(nl, nsides); cap=1 if slot>=nsides else 0   # cap faces need a 180 in UE
        else:
            slot=_slot_for(b["shape"], nl)
        nue=F_REFLECT*(R@nl); nue=nue/ (np.linalg.norm(nue) or 1.0)
        poly=_order_poly([to_ue(Va[i]) for i in vidx], nue)   # face polygon in UE space (for extent test)
        dfit=float(np.dot(nue, np.mean(poly,axis=0)))         # plane offset  (n . p == d for p on face)
        cylside=1 if (b["shape"]=="cylinder" and not cap) else 0   # curved side: needs post-boolean UV re-projection
        solid=1 if int(b.get("op",1))==0 else 0                     # op 0 = fill SOLID (union); its faces are
                                                                    # front-facing -> opposite U-mirror vs air carves
        slant=1 if (b["shape"]=="wedge" and slot==0) else 0         # wedge hypotenuse (slot 0): half-tile V phase
        fd=dict(n=[round(float(x),4) for x in nue], d=round(dfit,3),
                poly=[[round(float(x),2) for x in p] for p in poly], cap=cap, cylside=cylside, solid=solid,
                slant=slant,
                tex=resolve(slot), sc=resolve_scale(slot), rot=round(resolve_rot(slot),3),
                uoff=resolve_off(fuof,slot), voff=resolve_off(fvof,slot))
        # ROTATED brushes, WORLD-HORIZONTAL (cap) faces only: build picks the cap U/V by a fixed world axis,
        # which ignores the brush rotation (a pitched wedge's triangle cap comes out mis-rotated). Vertical
        # faces are fine on the world-based path (that's how Dark works), so we leave them alone. For the
        # ambiguous cap we bake the texture axes from the LOCAL normal, transformed by R and the Y-reflection.
        if (abs(b["H"])>0.5 or abs(b["P"])>0.5 or abs(b["B"])>0.5) and abs(nue[2])>0.99 and not cylside and not slant:
            if abs(nl[2])>0.99: lu=np.array([1.0,0.0,0.0]); lv=np.array([0.0,1.0 if nl[2]>0 else -1.0,0.0])
            else:
                lu=np.cross([0.0,0.0,1.0],nl); lu=lu/(np.linalg.norm(lu) or 1.0)
                lv=np.cross(nl,lu);          lv=lv/(np.linalg.norm(lv) or 1.0)
            ua=F_REFLECT*(R@lu); va=F_REFLECT*(R@lv)
            # The build-side -90 cap correction assumes the extrude axis maps to world -Z. When it maps to
            # +Z (e.g. pitch 270 vs 90) the cap frame is flipped 180 deg; fold that in by rotating the axes.
            if float((R@np.array([1.0,0.0,0.0]))[2])>0.0: ua=-ua; va=-va
            # The build +90 cap correction is calibrated for caps that come from the EXTRUDE axis (local +/-X,
            # local U along Y). A cap that comes from the -Y leg (local U along X) is a further 90 deg off.
            if abs(nl[0])<0.9:
                fd["capleg"]=1
                # id5 (H=0) has its extrude axis along world X and comes out correct with the uniform build
                # +90 correction. id6 (H=90) has heading rotate the extrude axis onto world Y; that heading
                # rotation leaves its leg cap "rotated 90 + flipped" relative to correct. Flag the heading-
                # rotated leg cap so the builder can undo it (rotate -90 + mirror U).
                rx=R@np.array([1.0,0.0,0.0])
                if abs(float(rx[0]))<0.5: fd["capleg_rot"]=1
            fd["uaxis"]=[round(float(x),4) for x in ua]; fd["vaxis"]=[round(float(x),4) for x in va]
        faces.append(fd)
    return faces

def _order_poly(pts, n):
    """Order convex-polygon points CCW in their plane (for point-in-polygon tests)."""
    if len(pts)<3: return pts
    c=np.mean(pts,axis=0)
    ref=np.array([1.0,0,0]) if abs(n[0])<0.9 else np.array([0,1.0,0])
    u=np.cross(ref,n); u/=(np.linalg.norm(u) or 1.0); v=np.cross(n,u)
    return sorted(pts, key=lambda p: math.atan2(float(np.dot(p-c,v)), float(np.dot(p-c,u))))

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