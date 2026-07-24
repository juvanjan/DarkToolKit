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
# Terrain media ops (editor/ged_csg.cpp:221 mediaop_names):
#   0 fill solid  1 fill air  2 fill water  3 flood  4 evaporate
#   5 solid->water  6 solid->air  7 air->solid  8 water->solid  9 blockable
# 6 and 7 used to be missing here, so those brushes were dropped at export and did nothing at all.
# 9 (blockable) is deliberately excluded: its media_op row only changes the PERSIST flags
# (SOLID->SOLID, AIR->AIR_PERSIST, WATER->WATER_PERSIST), so it makes no difference to shape.
KEEP_OPS = (0,1,2,3,4,5,6,7,8)

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
def cyl_local(h,sides,face_align=False):    # n-gon prism
    # ngon_base builds Dark's own ring. Vertex-aligned reproduces the old hardcoded phase exactly;
    # face-aligned rotates half a facet (pi/n) and scales 1/cos(pi/n) so the FACE, not the vertex,
    # touches the box (PRIMAL_ALIGN_FACE bit, primshap.c:182). The face-aligned mesh is built directly
    # from these verts (build-side mk_buffer), NOT re-fit by recover_cylinder, so mesh == geo exactly
    # and texturing stays in phase.
    hx,hy,hz=h; n=max(3,int(sides))
    ring=ngon_base(n, face_align)
    top=[(x*hx,y*hy, hz) for (x,y) in ring]
    bot=[(x*hx,y*hy,-hz) for (x,y) in ring]
    V=top+bot; T=[]
    for k in range(1,n-1): T.append((0,k,k+1)); T.append((n,n+k+1,n+k))
    for k in range(n):
        a,b=k,(k+1)%n; T.append((a,b,n+b)); T.append((a,n+b,n+a))
    return V,T

def ngon_base(n, face_align=False):
    """Dark's build_ngon_base (primshap.c:182), unit ring in the XY plane:
         ang = 2pi*(2i + face_mod)/(2n);   y = cos(ang)*sf;   x = -sin(ang)*sf
       face_mod = 1 for face-aligned rings (rotated half a facet), 0 for vertex-aligned. Face-aligned
       rings are also scaled by 1/cos(pi/n) so the FACE - not the vertex - touches the unit box; Dark
       computes that factor once at i==0 and reuses it for every vertex, which we reproduce."""
    fm = 1.0 if face_align else 0.0
    sf = 1.0/math.cos(math.pi/n) if face_align else 1.0
    out=[]
    for i in range(n):
        a = 2.0*math.pi*(2*i+fm)/(2.0*n)
        out.append((-math.sin(a)*sf, math.cos(a)*sf))
    return out

def pyr_local(h, sides, corner=False, face_align=False):
    """Dark's PrimShape_CreateNGonPyr (primshap.c:213). Base ring at z=-1 (points 0..n-1), apex at
       point n: centred for a pyramid, over base vertex 0 for a cornerpyramid. Faces: side i spans
       base verts i -> i+1 plus the apex (record slots 0..n-1), then the base ring (record slot n)."""
    hx,hy,hz=h; n=max(3,int(sides))
    ring=ngon_base(n, face_align)
    V=[(x*hx, y*hy, -hz) for (x,y) in ring]
    V.append((ring[0][0]*hx, ring[0][1]*hy, hz) if corner else (0.0,0.0,hz))
    # Dark's face_pts list is a POLYGON vertex list, not a triangle winding; the ring runs CCW seen
    # from +Z, so wind sides (i, i+1, apex) and the base fan backwards to get OUTWARD normals here.
    # (bake() re-runs orient() anyway, but faces_for() reads these normals raw.)
    # Also return the record slot of every triangle, rather than re-deriving it from the face normal's
    # azimuth. The azimuth trick assumes a side normal points along the midpoint angle of its two base
    # verts. That survives a corner apex (base edges are horizontal, so the normal is perpendicular to
    # its edge whatever the apex does) but it FAILS on an elliptical base: with hx != hy the edge is no
    # longer perpendicular to the midpoint radial, and a 2:1 ellipse mis-slots 4 of 14 sides. Emitting
    # the slot from the generator is exact for every variant and needs no phase term at all.
    ap=n; T=[]; tslot=[]
    for i in range(n): T.append((i, (i+1)%n, ap)); tslot.append(i)   # side i -> record slot i
    for k in range(1,n-1): T.append((0,k+1,k)); tslot.append(n)      # base fan -> record slot n
    return V,T,tslot

# -- regular dodecahedron: Dark's hardcoded tables, verbatim from primshap.c:389 -------------------
# 20 points / 30 edges / 12 pentagonal faces. Unlike the other primals its raw points are NOT unit in
# each axis (max |x|,|y|,|z| = G, E, C below), and Dark just element-wise multiplies them by the
# brush's sz (primal.c:278 primalRawFull -> mx_elmul_vec), so a dodec brush's true half-extents are
# (0.934*sx, 0.982*sy, 0.795*sz). Reproduce that exactly rather than normalising.
_DODA,_DODB,_DODC,_DODD,_DODE = 0.0, 0.5773502692, 0.7946544723, 0.1875924741, 0.9822469464
_DODF,_DODG,_DODH,_DODI,_DODJ = 0.6070619982, 0.9341723590, 0.3568220898, 0.4911234732, 0.3035309991
DODEC_PTS=[
 ( _DODB, _DODD,-_DODC),( _DODA, _DODF,-_DODC),(-_DODB, _DODD,-_DODC),(-_DODH,-_DODI,-_DODC),( _DODH,-_DODI,-_DODC),
 ( _DODG, _DODJ,-_DODD),( _DODA, _DODE,-_DODD),(-_DODG, _DODJ,-_DODD),(-_DODB,-_DODC,-_DODD),( _DODB,-_DODC,-_DODD),
 ( _DODB,-_DODD, _DODC),( _DODA,-_DODF, _DODC),(-_DODB,-_DODD, _DODC),(-_DODH, _DODI, _DODC),( _DODH, _DODI, _DODC),
 ( _DODG,-_DODJ, _DODD),( _DODA,-_DODE, _DODD),(-_DODG,-_DODJ, _DODD),(-_DODB, _DODC, _DODD),( _DODB, _DODC, _DODD)]
# face_pts_list: index == record slot, five points per pentagon
DODEC_FACES=[
 [ 0, 1, 2, 3, 4],[ 0, 4, 9,15, 5],[ 1, 0, 5,19, 6],[ 2, 1, 6,18, 7],[ 3, 2, 7,17, 8],[ 4, 3, 8,16, 9],
 [10,11,12,13,14],[10,14,19, 5,15],[11,10,15, 9,16],[12,11,16, 8,17],[13,12,17, 7,18],[14,13,18, 6,19]]

def dodec_local(h):
    """Dark's PrimShape_CreateDodecahedron. Record slot == face index 0..11. As with the cube/wedge/
       pyramid tables, face_pts is a POLYGON vertex list wound the opposite way from our outward-normal
       convention, so fan-triangulate it reversed."""
    hx,hy,hz=h
    V=[(x*hx, y*hy, z*hz) for (x,y,z) in DODEC_PTS]
    T=[]; tslot=[]
    for si,poly in enumerate(DODEC_FACES):
        for k in range(1,len(poly)-1):
            T.append((poly[0], poly[k+1], poly[k])); tslot.append(si)
    return V,T,tslot

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
    """LEGACY fallback only. Face count is ambiguous - a 4-sided cylinder also has 6 faces, a 4-sided
       pyramid also has 5 - so this misreads ~16% of brushes in MISS15. Use classify_primal()."""
    if nf>=7: return "cylinder",nf-2
    if nf==6: return "box",0
    if nf==5: return "wedge",0
    return "box",0

# primal_id (brush record offset 4) encodes the shape exactly - src/editor/primal.h:28
#   primalID_Make(type,sides) = (type<<9) + (sides-3);  PRIMAL_ALIGN_FACE = 0x100
PRIMAL_TYPES={0:"special",1:"cylinder",2:"pyramid",3:"cornerpyr"}

def classify_primal(pid, nf):
    """(shape, sides, face_align) from primal_id, falling back to the face count if it looks bogus."""
    t=(pid>>9)&0x7; sides=(pid&0xff)+3; falign=bool(pid&0x100)
    kind=PRIMAL_TYPES.get(t)
    if kind=="cylinder" and 3<=sides<=64:  return "cylinder",sides,falign
    if kind=="pyramid"  and 3<=sides<=64:  return "pyramid",sides,falign
    if kind=="cornerpyr"and 3<=sides<=64:  return "cornerpyr",sides,falign
    if kind=="special":
        if sides==4:  return "box",0,falign
        if sides==10: return "wedge",0,falign
        if sides==9:  return "dodec",0,falign     # PRIMAL_DODEC_IDX (primal_id 6), 12 pentagons
    s,n=classify(nf)                                    # dodec/line/light or a corrupt id
    return s,n,falign

def bake(b):
    h=b["half"]
    if   b["shape"]=="cylinder": V,T=cyl_local(h,b["sides"],b.get("falign",False))
    elif b["shape"]=="wedge":    V,T=wedge_local(h)
    elif b["shape"] in ("pyramid","cornerpyr"):
        V,T,_=pyr_local(h,b["sides"],b["shape"]=="cornerpyr",b.get("falign",False))
    elif b["shape"]=="dodec":    V,T,_=dodec_local(h)
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

def read_water_prefix(chunks, f):
    """The mission's water family prefix from the FAMILY chunk (render/family.c:900).

    family_name_block is [sky_name, water_name, <families>], each FAM_NAME_LEN=24 bytes; the engine
    loads `<prefix>in`/`<prefix>out` from fam\\waterhw\\ into the RESERVED slots WATERIN_IDX=247 and
    WATEROUT_IDX=248. A water surface NEVER takes a brush-face texture - get_texture_for_medium_
    transition (editor/cvtbrush.c:169) substitutes those two slots by crossing direction - so this
    chunk is the only record of which water look the mission uses. All our test missions say 'gr'.
    Layout: size_per(4), cnt(4) at +24, then cnt entries. NewDark's MAX_FAMILIES=32 makes cnt 34."""
    if "FAMILY" not in chunks: return ""
    o,_l=chunks["FAMILY"]
    try:
        per,n=struct.unpack_from("<II",f,o+24)
        if per<=0 or n<2: return ""
        base=o+32
        return f[base+per:base+2*per].split(b"\x00")[0].decode("latin1")
    except Exception:
        return ""

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
    tslot=None
    if   b["shape"]=="cylinder": V,T=cyl_local(h,b["sides"],b.get("falign",False))
    elif b["shape"]=="wedge":    V,T=wedge_local(h)
    elif b["shape"] in ("pyramid","cornerpyr"):
        V,T,tslot=pyr_local(h,b["sides"],b["shape"]=="cornerpyr",b.get("falign",False))
    elif b["shape"]=="dodec":    V,T,tslot=dodec_local(h)
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
    # Group triangles into faces. Normally the grouping key is the rounded local normal, but when the
    # generator handed us an explicit per-triangle slot map (pyramids) we key on the SLOT instead: it is
    # exact, and it keeps two coplanar-but-differently-textured faces apart (a cornerpyramid's two
    # vertical sides through the apex edge can share a plane).
    groups={}
    for ti,t in enumerate(T):
        p,q,r=Va[t[0]],Va[t[1]],Va[t[2]]
        n=np.cross(q-p,r-p); L=np.linalg.norm(n)
        if L<1e-9: continue                                   # degenerate (cornerpyr sides at the apex)
        nl=n/L
        key=tslot[ti] if tslot is not None else tuple(np.round(nl,3))
        g=groups.get(key)
        if g is None: groups[key]=[nl,set(t),(tslot[ti] if tslot is not None else None)]
        else: g[1].update(t)
    nsides=int(b.get("sides",8))
    ispyr=b["shape"] in ("pyramid","cornerpyr")
    faces=[]
    for nl,vidx,gslot in groups.values():
        cap=0; pyrside=0; dodside=0
        if b["shape"]=="cylinder":
            slot=_cyl_slot(nl, nsides); cap=1 if slot>=nsides else 0   # cap faces need a 180 in UE
        elif ispyr:
            slot=gslot                                  # exact, straight from pyr_local
            if slot>=nsides: cap=1                      # the -Z base is a world-horizontal cap
            else:            pyrside=1                  # tilted side -> Dark base-axis projection
        elif b["shape"]=="dodec":
            slot=gslot                                  # exact, straight from dodec_local
            dodside=1                                   # tilted pentagon -> Dark base-axis projection
            # faces 0 and 6 are exactly +/-Z; the builder's cap branch intercepts those on |n_z|>0.99
            # before the Dark branch is reached, so no special-casing is needed here.
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
                slant=slant, pyrside=pyrside, dodside=dodside,
                tex=resolve(slot), sc=resolve_scale(slot), rot=round(resolve_rot(slot),3),
                uoff=resolve_off(fuof,slot), voff=resolve_off(fvof,slot))
        # ROTATED brushes, WORLD-HORIZONTAL (cap) faces only: build picks the cap U/V by a fixed world axis,
        # which ignores the brush rotation (a pitched wedge's triangle cap comes out mis-rotated). Vertical
        # faces are fine on the world-based path (that's how Dark works), so we leave them alone. For the
        # ambiguous cap we bake the texture axes from the LOCAL normal, transformed by R and the Y-reflection.
        if (abs(b["H"])>0.5 or abs(b["P"])>0.5 or abs(b["B"])>0.5) and abs(nue[2])>0.99 and not cylside and not slant and not pyrside and not dodside:
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
    water=read_water_prefix(chunks, f)
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
            pid=struct.unpack_from("<i",d,p+4)[0]            # primal_id: shape type + side count + align bit
            deftex=struct.unpack_from("<h",d,p+8)[0]         # brush's DEFAULT texture (what -1 faces inherit)
            x,y,z,sx,sy,sz=struct.unpack_from("<6f",d,p+12)
            ax,ay,az=struct.unpack_from("<3H",d,p+36)
            q0=p+76
            ftex=[struct.unpack_from("<h",d,q0+k*10)[0]   for k in range(nf)]   # +0 texture id (-1=inherit)
            frot=[struct.unpack_from("<H",d,q0+k*10+2)[0] for k in range(nf)]   # +2 rotation (16-bit angle)
            fscl=[struct.unpack_from("<H",d,q0+k*10+4)[0] for k in range(nf)]   # +4 scale (16 = 1x)
            fuof=[struct.unpack_from("<H",d,q0+k*10+6)[0] for k in range(nf)]   # +6 U offset
            fvof=[struct.unpack_from("<H",d,q0+k*10+8)[0] for k in range(nf)]   # +8 V offset
            shape,sides,falign=classify_primal(pid,nf)
            if all(math.isfinite(v) for v in (x,y,z,sx,sy,sz)) and min(sx,sy,sz)>0.0:
                out.append(dict(id=bid,time=tm,op=int(op),shape=shape,sides=sides,falign=falign,deftex=deftex,
                                pos=(x,y,z),half=(sx,sy,sz),H=dg(az),P=dg(ay),B=dg(ax),
                                ftex=ftex,fscl=fscl,frot=frot,fuof=fuof,fvof=fvof))
        p+=76+nf*10
    out.sort(key=lambda b:b["time"])
    return out, names, water

def convert(inp, outp):
    B,names,water=extract(inp)
    allfaces=[faces_for(b,names) for b in B]
    resolve_inheritance(B, allfaces)          # Dark rule: -1 faces inherit the solid they carve into
    brushes=[]; allv=[]
    for b,faces in zip(B,allfaces):
        V,T=bake(b)
        # Emit the brush's ORIENTATION and half-extents explicitly. The builder used to recover a box's
        # frame from its 8 vertices by eigen-decomposition, which is ambiguous the moment two extents
        # are equal: the eigenvalues are degenerate so the eigenvectors are arbitrary within that
        # subspace. Cubes and square-plan boxes therefore came out spuriously rotated (MISS5 id531
        # 24x24x24 -> 69.7 deg, id2846 36x36x16 -> 45 deg) even though H=P=B=0 in the record. We know
        # the exact rotation here, so hand it over instead of making UE guess.
        R=Mdark(b["H"],b["P"],b["B"])
        axes=[(F_REFLECT*(R@e)).tolist() for e in np.eye(3)]
        # F_REFLECT has determinant -1, so the reflected triple is LEFT-handed; negate one axis to get
        # a proper rotation. Harmless for these shapes - they are symmetric about every local axis.
        if float(np.dot(np.cross(axes[0],axes[1]),axes[2]))<0: axes[1]=[-x for x in axes[1]]
        brushes.append(dict(id=b["id"],time=b["time"],op=b["op"],shape=b["shape"],
                            falign=bool(b.get("falign",False)),   # PRIMAL_ALIGN_FACE bit (build-side rot)
                            verts=V,tris=T,faces=faces,
                            axes=[[round(float(x),6) for x in a] for a in axes],
                            ext=[round(float(h)*SCALE,4) for h in b["half"]]))
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
                   # Water look for THIS mission. `in` is the surface seen from the air side, `out`
                   # the one seen from underwater (cvtbrush.c:169 picks by crossing direction).
                   water=dict(prefix=water, tex_in=(water+"in") if water else "",
                              tex_out=(water+"out") if water else ""),
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