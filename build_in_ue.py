# build_in_ue.py  ---  Dark .MIS mission -> Unreal Engine 5, no voxels.
#
# Geometry is PRECOMPUTED in world space (the *_geo.json from mis_to_geo.py) so Unreal never
# re-derives rotation. For every BOX brush we recover an oriented primitive (center + rotation +
# extents) straight from the baked verts and build it with GeometryScript's append_box. Primitives
# carry real normals, so Unreal's boolean treats them as *solids* and produces a proper
# "box carved into a box" - unlike raw buffer meshes (append_buffers_to_mesh with no normals),
# which Unreal boolean treats as open shells and collapses to "one box".
#
# STRATEGY: start from the enclosing world solid (brush 0) and, in creation order, UNION every
# solid brush and SUBTRACT every air/water brush directly onto that mesh.
#
#   medium op:  0 solid  1 air  2 water  3 flood(air->water)  4 evaporate(water->air)
#               5 solid->water   8 water->solid
#     0 solid        result += B ;  WATER -= B
#     1 air          result -= B ;  WATER -= B
#     2 water        result -= B ;  WATER += B
#     3 flood        t = B - result ; WATER += t
#     4 evaporate    WATER -= B
#     5 solid->water t = B & result ; WATER += t ; result -= B
#     8 water->solid t = B & WATER  ; result += t ; WATER -= B
#
# SETUP: Edit > Plugins > enable "Geometry Script" + "Python Editor Script Plugin", restart.
#        Set GEO_PATH. Tools > Execute Python Script.
# FIRST RUN: TEST_LIMIT=0 builds the whole mission. Set TEST_LIMIT=60 for a fast preview.

import unreal, json, math

GEO_PATH    = r"C:\Nex\DarkSimProject\DarkSimToolkit\test_missions\09_geo.json"   # <-- SET (the *_geo.json)
ASSET_PATH  = r"/Game/Mission/SM_Mission"
BUILD_WATER = True        # also bake the water volume as a separate static mesh (SM_..._Water)
BUILD_COLLISION = True    # give the mesh collision (complex-as-simple: the triangles ARE the collision)
TEST_LIMIT  = 0           # 0 = full mission; >0 = world solid + first N brushes (quick preview)
UV_TILE_CM  = 64.0        # world-space texture tile size (one texture repeat per this many cm)
                          #   calibrated to Dark scale 16 (~2.1 ft/tile). Halve it -> texture bigger.

RES_SCALING = False       # per-texture material tiling (superseded by per-face UV formula below)
TEXEL_REF_PX = 64.0       # fallback texture width if a texture's size is unknown
FEET_CM = 30.48           # Dark world tile (feet) = pixels * 2^(scale-20); this converts feet -> cm
BUILD_TEXTURES = True     # assign real Dark textures (needs the *_geo.json with per-face "faces" data)
TEX_DIR   = r"C:/Nex/DarkSimProject/DarkSimToolkit/textures"   # folder with the PNGs + textures_manifest.json
MAT_ROOT  = r"/Game/Mission/Materials"                     # where imported textures + materials are created

# ---------------------------------------------------------------- resolve GeometryScript by capability
_GS=[n for n in dir(unreal) if n.startswith("GeometryScript_")]
unreal.log("GeometryScript libs: "+", ".join(_GS))
def lib_with(m, opt=False):
    for n in _GS:
        o=getattr(unreal,n)
        if hasattr(o,m):
            unreal.log("  '%s' -> unreal.%s"%(m,n)); return o
    if opt: unreal.log_warning("  no lib exposes '%s'"%m); return None
    raise AttributeError("no lib exposes '%s'"%m)
def method_like(*c):
    for n in _GS:
        o=getattr(unreal,n)
        for m in dir(o):
            if all(x in m.lower() for x in c) and not m.startswith("_"): return o,m
    return None,None
def _type(exact,*must,opt=False):
    if hasattr(unreal,exact): return getattr(unreal,exact)
    for n in dir(unreal):
        if not n.startswith("GeometryScript_") and all(x.lower() in n.lower() for x in must):
            return getattr(unreal,n)
    if opt: return None
    raise AttributeError("no type %s"%exact)
GPRIM = lib_with("append_box")
GCYL  = lib_with("append_cylinder", opt=True)          # for cylinders
GCUT  = lib_with("apply_mesh_plane_cut", opt=True)     # for wedges (slice a solid box)
GEDIT = lib_with("append_buffers_to_mesh", opt=True)   # fallback for non-box shapes
GB    = lib_with("apply_mesh_boolean")
WEDGE_CUT_FLIP = False    # if a wedge fills the wrong diagonal half, flip this
GASSET,ASSET_FN = method_like("static_mesh_asset","from_mesh")
GCOPY,COPY_FN = method_like("copy","mesh","to","static")     # overwrite an existing SM in place (no dialog)
_TCLIB,_TCFN = method_like("triangle","count")
def CopyOpts():   t=_type("GeometryScriptCopyMeshToAssetOptions","CopyMeshToAsset","Options",opt=True); return t() if t else None
def BoolOpts():   t=_type("GeometryScriptMeshBooleanOptions","BooleanOptions",opt=True); return t() if t else None
def CutOpts():    t=_type("GeometryScriptMeshPlaneCutOptions","PlaneCut","Options",opt=True); return t() if t else None
def PrimOpts():   t=_type("GeometryScriptPrimitiveOptions","PrimitiveOptions",opt=True); return t() if t else None
def MeshBuffers():t=_type("GeometryScriptSimpleMeshBuffers","SimpleMeshBuffers",opt=True); return t() if t else None
def AssetOpts():  t=_type("GeometryScriptCreateNewStaticMeshAssetOptions","StaticMeshAsset","Options",opt=True); return t() if t else None
_BO=_type("GeometryScriptBooleanOperation","BooleanOperation")
UNION,SUBTRACT,INTERSECT=_BO.UNION,_BO.SUBTRACT,_BO.INTERSECTION
_PO=_type("GeometryScriptPrimitiveOriginMode","PrimitiveOriginMode",opt=True)
ORIGIN_CENTER=getattr(_PO,"CENTER",None) if _PO else None
_NLIB,_NFN=method_like("recompute","normal"); _UVLIB,_UVFN=method_like("uv","projection")
I=unreal.Transform()

def new_mesh():
    try: return unreal.new_object(unreal.DynamicMesh)
    except Exception: return unreal.DynamicMesh()
def tri_count(m):
    for attr in ("get_triangle_count","triangle_count"):
        f=getattr(m,attr,None)
        if callable(f):
            try: return f()
            except Exception: pass
    if _TCLIB:
        try: return getattr(_TCLIB,_TCFN)(m)
        except Exception: pass
    return -1
def ensure_uv_normals(m):
    if _NLIB:
        try: getattr(_NLIB,_NFN)(m)
        except Exception: pass
    if _UVLIB:
        for a in ((m,0,I),(m,0,None,I),(m,I),(m,0)):
            try: getattr(_UVLIB,_UVFN)(*a); break
            except Exception: continue

# ---------------------------------------------------------------- recover an oriented box from 8 verts
def _eig_sym3(A):
    a=[row[:] for row in A]; V=[[1.0,0,0],[0,1.0,0],[0,0,1.0]]
    for _ in range(60):
        p,q=0,1
        if abs(a[0][2])>abs(a[p][q]): p,q=0,2
        if abs(a[1][2])>abs(a[p][q]): p,q=1,2
        if abs(a[p][q])<1e-12: break
        app,aqq,apq=a[p][p],a[q][q],a[p][q]
        phi=0.5*math.atan2(2*apq, aqq-app) if (aqq-app)!=0 else math.pi/4
        c,s=math.cos(phi),math.sin(phi)
        for k in range(3):
            akp,akq=a[k][p],a[k][q]; a[k][p]=c*akp-s*akq; a[k][q]=s*akp+c*akq
        for k in range(3):
            apk,aqk=a[p][k],a[q][k]; a[p][k]=c*apk-s*aqk; a[q][k]=s*apk+c*aqk
        for k in range(3):
            vkp,vkq=V[k][p],V[k][q]; V[k][p]=c*vkp-s*vkq; V[k][q]=s*vkp+c*vkq
    vecs=[[V[r][cidx] for r in range(3)] for cidx in range(3)]
    return vecs

def recover_box(verts):
    n=len(verts)
    C=[sum(v[i] for v in verts)/n for i in range(3)]
    cov=[[0.0]*3 for _ in range(3)]
    for v in verts:
        d=[v[i]-C[i] for i in range(3)]
        for i in range(3):
            for j in range(3): cov[i][j]+=d[i]*d[j]
    axes=_eig_sym3(cov)
    ax=[]
    for a in axes:
        l=math.sqrt(sum(x*x for x in a)) or 1.0
        ax.append([x/l for x in a])
    # right-handed: X x Y = Z
    cx=[ax[0][1]*ax[1][2]-ax[0][2]*ax[1][1],
        ax[0][2]*ax[1][0]-ax[0][0]*ax[1][2],
        ax[0][0]*ax[1][1]-ax[0][1]*ax[1][0]]
    if cx[0]*ax[2][0]+cx[1]*ax[2][1]+cx[2]*ax[2][2] < 0:
        ax[2]=[-x for x in ax[2]]
    ext=[]
    for a in ax:
        m=max(abs(sum((verts[k][i]-C[i])*a[i] for i in range(3))) for k in range(n))
        ext.append(m)
    return C, ax, ext

def _quat_from_axes(ax):
    # columns of R are ax[0],ax[1],ax[2]
    m00,m01,m02=ax[0][0],ax[1][0],ax[2][0]
    m10,m11,m12=ax[0][1],ax[1][1],ax[2][1]
    m20,m21,m22=ax[0][2],ax[1][2],ax[2][2]
    tr=m00+m11+m22
    if tr>0:
        s=math.sqrt(tr+1.0)*2; w=0.25*s; x=(m21-m12)/s; y=(m02-m20)/s; z=(m10-m01)/s
    elif m00>m11 and m00>m22:
        s=math.sqrt(1.0+m00-m11-m22)*2; w=(m21-m12)/s; x=0.25*s; y=(m01+m10)/s; z=(m02+m20)/s
    elif m11>m22:
        s=math.sqrt(1.0+m11-m00-m22)*2; w=(m02-m20)/s; x=(m01+m10)/s; y=0.25*s; z=(m12+m21)/s
    else:
        s=math.sqrt(1.0+m22-m00-m11)*2; w=(m10-m01)/s; x=(m02+m20)/s; y=(m12+m21)/s; z=0.25*s
    return unreal.Quat(x,y,z,w)

def mk_box(b):
    C,ax,ext=recover_box(b["verts"])
    m=new_mesh()
    t=unreal.Transform()
    t.set_editor_property("translation", unreal.Vector(C[0],C[1],C[2]))
    t.set_editor_property("rotation", _quat_from_axes(ax))
    dx,dy,dz=2*ext[0],2*ext[1],2*ext[2]
    po=PrimOpts()
    args=[m,po,t,dx,dy,dz]
    kw={}
    if ORIGIN_CENTER is not None: kw["origin"]=ORIGIN_CENTER
    try:
        GPRIM.append_box(*args,**kw)
    except Exception:
        GPRIM.append_box(m,po,t,dx,dy,dz)
    return m

# ---------------------------------------------------------------- recover a wedge (right-tri prism)
def _sub(a,b): return [a[i]-b[i] for i in range(3)]
def _cross(a,b): return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
def _dot(a,b): return sum(a[i]*b[i] for i in range(3))
def _norm(a):
    l=math.sqrt(_dot(a,a)) or 1.0; return [x/l for x in a]

def recover_wedge(verts,tris):
    # group coplanar tris into faces; triangular caps = single tri, rectangles = 2 tris
    groups=[]
    for t in tris:
        p,q,r=verts[t[0]],verts[t[1]],verts[t[2]]
        n=_norm(_cross(_sub(q,p),_sub(r,p))); d=_dot(n,p)
        placed=False
        for g in groups:
            if abs(_dot(n,g['n']))>0.999 and abs(d - g['d']*(1 if _dot(n,g['n'])>0 else -1))<1.0:
                g['tris'].append(t); placed=True; break
        if not placed: groups.append({'n':n,'d':d,'tris':[t]})
    caps =[g for g in groups if len(g['tris'])==1]
    rects=[g for g in groups if len(g['tris'])>=2]
    extr=_norm(caps[0]['n'])
    legs=None
    for i in range(len(rects)):
        for j in range(i+1,len(rects)):
            if abs(_dot(rects[i]['n'],rects[j]['n']))<0.05:
                legs=(_norm(rects[i]['n']),_norm(rects[j]['n'])); break
        if legs: break
    ax=[legs[0],legs[1],extr]
    if _dot(_cross(ax[0],ax[1]),ax[2])<0: ax[2]=[-x for x in ax[2]]
    lo=[min(_dot(v,ax[k]) for v in verts) for k in range(3)]
    hi=[max(_dot(v,ax[k]) for v in verts) for k in range(3)]
    C=[sum((lo[k]+hi[k])/2*ax[k][i] for k in range(3)) for i in range(3)]
    ext=[(hi[k]-lo[k])/2 for k in range(3)]
    def sgn(v,k): return 1 if _dot(v,ax[k])>(lo[k]+hi[k])/2 else -1
    combos=set((sgn(v,0),sgn(v,1)) for v in verts)
    s0,s1=list({(1,1),(1,-1),(-1,1),(-1,-1)}-combos)[0]
    # hypotenuse runs corner-to-corner, so its normal uses the SWAPPED half-extents (aspect-correct),
    # NOT a fixed 45 deg. n along ax0 ~ s1*ext1, along ax1 ~ s0*ext0; flip to point at removed corner.
    n=[s1*ext[1]*ax[0][i] + s0*ext[0]*ax[1][i] for i in range(3)]
    rc=[s0*ax[0][i]+s1*ax[1][i] for i in range(3)]           # direction to removed corner
    if _dot(n,rc)<0: n=[-x for x in n]
    md=_norm(n)
    return C, ax, ext, md

def mk_wedge(b):
    if not GCUT: return mk_buffer(b)
    try:
        C,ax,ext,md=recover_wedge(b["verts"], b["tris"])
        m=new_mesh()
        t=unreal.Transform()
        t.set_editor_property("translation", unreal.Vector(C[0],C[1],C[2]))
        t.set_editor_property("rotation", _quat_from_axes(ax))
        dx,dy,dz=2*ext[0],2*ext[1],2*ext[2]
        po=PrimOpts(); kw={}
        if ORIGIN_CENTER is not None: kw["origin"]=ORIGIN_CENTER
        try: GPRIM.append_box(m,po,t,dx,dy,dz,**kw)
        except Exception: GPRIM.append_box(m,po,t,dx,dy,dz)
        nx,ny,nz=md              # plane normal points to the half to REMOVE
        if WEDGE_CUT_FLIP: nx,ny,nz=-nx,-ny,-nz
        rz=unreal.MathLibrary.make_rot_from_z(unreal.Vector(nx,ny,nz))
        frame=unreal.Transform()
        frame.set_editor_property("translation", unreal.Vector(C[0],C[1],C[2]))
        try: frame.set_editor_property("rotation", rz.quaternion())
        except Exception: frame.set_editor_property("rotation", rz)
        GCUT.apply_mesh_plane_cut(m, frame, CutOpts())
        return m
    except Exception as e:
        unreal.log_warning("  wedge primitive failed (%s); buffer fallback"%e)
        return mk_buffer(b)

# ---------------------------------------------------------------- recover a cylinder (n-gon prism)
def _solve3(M,y):
    A=[row[:]+[y[i]] for i,row in enumerate(M)]
    for c in range(3):
        p=max(range(c,3),key=lambda r:abs(A[r][c])); A[c],A[p]=A[p],A[c]
        pv=A[c][c] or 1e-9
        for r in range(3):
            if r!=c:
                f=A[r][c]/pv
                for k in range(4): A[r][k]-=f*A[c][k]
    return [A[i][3]/(A[i][i] or 1e-9) for i in range(3)]

def recover_cylinder(verts,tris):
    groups=[]
    for t in tris:
        p,q,r=verts[t[0]],verts[t[1]],verts[t[2]]
        n=_norm(_cross(_sub(q,p),_sub(r,p))); d=_dot(n,p)
        placed=False
        for g in groups:
            if abs(_dot(n,g['n']))>0.999 and abs(d - g['d']*(1 if _dot(n,g['n'])>0 else -1))<1.0:
                g['tris'].append(t); placed=True; break
        if not placed: groups.append({'n':n,'d':d,'tris':[t]})
    axis=_norm(sorted(groups,key=lambda g:-len(g['tris']))[0]['n'])   # caps have the most tris
    C=[sum(v[i] for v in verts)/len(verts) for i in range(3)]
    proj=[_dot(_sub(v,C),axis) for v in verts]
    height=max(proj)-min(proj)
    tmp=[1.0,0,0] if abs(axis[0])<0.9 else [0,1.0,0]
    e1=_norm(_cross(axis,tmp)); e2=_norm(_cross(axis,e1))
    pts=[(_dot(_sub(v,C),e1),_dot(_sub(v,C),e2)) for v in verts]
    # least-squares conic A x^2 + B xy + C y^2 = 1  (removes n-gon phase error)
    M=[[0.0]*3 for _ in range(3)]; Y=[0.0]*3
    for x,y in pts:
        bb=[x*x,x*y,y*y]
        for i in range(3):
            Y[i]+=bb[i]
            for j in range(3): M[i][j]+=bb[i]*bb[j]
    A,Bc,Cc=_solve3(M,Y); H=Bc/2.0
    tr=A+Cc; disc=max(0.0,(tr/2)**2-(A*Cc-H*H))
    l1=tr/2+math.sqrt(disc); l2=tr/2-math.sqrt(disc)   # l2 = smaller eigenvalue -> MAJOR axis
    a=1.0/math.sqrt(l2); bmin=1.0/math.sqrt(l1)
    # eigenvector of the conic matrix [[A,H],[H,C]] for l2 gives the TRUE major direction
    # (the old atan2 angle mis-assigned major vs minor for eccentric ellipses -> wrong rotation).
    if abs(H)<1e-9:
        v2=[1.0,0.0] if A<=Cc else [0.0,1.0]
    else:
        vv=[H, l2-A]; ln=math.hypot(vv[0],vv[1]) or 1.0; v2=[vv[0]/ln, vv[1]/ln]
    xw=[v2[0]*e1[i]+v2[1]*e2[i] for i in range(3)]
    yw=[-v2[1]*e1[i]+v2[0]*e2[i] for i in range(3)]
    ax=[_norm(xw),_norm(yw),axis]
    if _dot(_cross(ax[0],ax[1]),ax[2])<0: ax[1]=[-x for x in ax[1]]
    sides=len(verts)//2
    return C, ax, a, bmin, height, sides

def mk_cylinder(b):
    if not GCYL: return mk_buffer(b)
    try:
        C,ax,a,bmin,height,sides=recover_cylinder(b["verts"], b["tris"])
        m=new_mesh()
        t=unreal.Transform()
        t.set_editor_property("translation", unreal.Vector(C[0],C[1],C[2]))
        t.set_editor_property("rotation", _quat_from_axes(ax))
        t.set_editor_property("scale3d", unreal.Vector(a, bmin, height))   # unit cyl -> ellipse
        po=PrimOpts(); kw={}
        if ORIGIN_CENTER is not None: kw["origin"]=ORIGIN_CENTER
        # unit cylinder: radius 1, height 1, centered
        try: GCYL.append_cylinder(m, po, t, 1.0, 1.0, sides, 1, True, **kw)
        except Exception: GCYL.append_cylinder(m, po, t, 1.0, 1.0, sides, 1, True)
        return m
    except Exception as e:
        unreal.log_warning("  cylinder primitive failed (%s); buffer fallback"%e)
        return mk_buffer(b)

def mk_buffer(b):
    if GEDIT is None: return mk_box(b)  # last resort
    m=new_mesh(); buf=MeshBuffers()
    V=b["verts"]; T=b["tris"]
    buf.set_editor_property("vertices",  [unreal.Vector(v[0],v[1],v[2]) for v in V])
    buf.set_editor_property("triangles", [unreal.IntVector(t[0],t[1],t[2]) for t in T])
    GEDIT.append_buffers_to_mesh(m, buf)
    return m

# ---------------------------------------------------------------- textures (all behind BUILD_TEXTURES)
# Every step is capability-detected: if the engine doesn't expose a needed GeometryScript function we
# log it and skip texturing, so geometry always still builds. Material IDs are GLOBAL (a texture name
# always maps to the same id) so they survive the boolean and map to the right material at the end.
import os as _os, json as _json
_MANIFEST={}; _MANIFEST_LC={}; _MATS={}; _NEXTID=[1]   # id 0 reserved for the default/inherit material
_DEFAULT_MAT=[None]
_TEX_OK=[BUILD_TEXTURES]

# find a lib+method by exact-name candidates first, then substring fallback
def _find(cands, *substr):
    for n in _GS:
        o=getattr(unreal,n)
        for c in cands:
            if hasattr(o,c): return o,c
    if substr:
        for n in _GS:
            o=getattr(unreal,n)
            for m in sorted(dir(o)):
                if not m.startswith("_") and all(x in m.lower() for x in substr): return o,m
    return None,None

# per-triangle normal + per-triangle material id (works on fresh primitives with compact triangle ids)
GQ_NORM, _TRINORM = _find(["get_triangle_face_normal"], "triangle","face","normal")
GMAT_TRI,_SETMATTRI = _find(["set_triangle_material_id","set_material_id_on_triangle"])
# selection-by-normal + material-for-selection (preferred if present)
GSEL_N, _SELN = _find(["select_mesh_elements_by_normal_angle","select_mesh_faces_by_normal_angle"],"select","normal")
GMAT_SEL,_SETMATSEL = _find(["set_material_id_for_mesh_selection","set_material_i_ds_for_mesh_selection"],"material","selection")
# world-scale UVs   (note: real name is set_mesh_u_vs_... with u_vs)
GUV_BOX, _UVBOX = _find(["set_mesh_u_vs_from_box_projection"],"u_vs","box","projection")
GUV_PLN, _UVPLN = _find(["set_mesh_u_vs_from_planar_projection"],"u_vs","planar")
GMAT_EN,_ENMATID = _find(["enable_material_i_ds","enable_material_ids"],"enable","material")
_PERFACE_UV=[BUILD_TEXTURES]   # per-face planar UVs at exact Dark scale (px * 2^(sc-20) ft per tile)
_PERFACE_LOGGED=[False]

def _dump(libname, *contains):
    o=getattr(unreal,libname,None)
    if not o: return
    fns=[m for m in sorted(dir(o)) if not m.startswith("_") and (not contains or any(c in m.lower() for c in contains))]
    unreal.log("  [%s] %s"%(libname, ", ".join(fns)))

def _tex_report():
    unreal.log("Texturing capability (resolved):")
    unreal.log("  triangle normal  : %s.%s"%(GQ_NORM and GQ_NORM.__name__, _TRINORM))
    unreal.log("  set matid (tri)  : %s.%s"%(GMAT_TRI and GMAT_TRI.__name__, _SETMATTRI))
    unreal.log("  select by normal : %s.%s"%(GSEL_N and GSEL_N.__name__, _SELN))
    unreal.log("  set matid (sel)  : %s.%s"%(GMAT_SEL and GMAT_SEL.__name__, _SETMATSEL))
    unreal.log("  uv projection    : %s.%s"%(GUV_BOX and GUV_BOX.__name__, _UVBOX))
    unreal.log("Available functions (for locking names if any above are None):")
    _dump("GeometryScript_Materials","material","selection")
    _dump("GeometryScript_MeshQueries","normal","triangle")
    _dump("GeometryScript_MeshSelection","select","normal")
    _dump("GeometryScript_UVs","projection","box","plane")
    have_tri = _TRINORM and _SETMATTRI
    have_sel = _SELN and _SETMATSEL
    if not (have_tri or have_sel):
        unreal.log_warning("  -> no usable material-tagging path; textures disabled (geometry still builds)")
        _TEX_OK[0]=False

_TEX_ROOT=[TEX_DIR]
def _load_manifest():
    here=_os.path.dirname(GEO_PATH)
    cands=[TEX_DIR, _os.path.join(TEX_DIR,"textures"),
           _os.path.join(here,"textures"), here,
           _os.path.join(_os.path.dirname(TEX_DIR),"textures")]
    for c in cands:
        mp=_os.path.join(c,"textures_manifest.json")
        if _os.path.isfile(mp):
            _TEX_ROOT[0]=c
            data=_json.load(open(mp)); _MANIFEST.update(data.get("textures",{}))
            _MANIFEST_LC.update({k.lower():v for k,v in _MANIFEST.items()})   # case-insensitive lookup
            unreal.log("Loaded manifest: %d textures from %s"%(len(_MANIFEST),c)); return
    unreal.log_warning("no textures_manifest.json found (looked in: %s); textures disabled"%" | ".join(cands))
    _TEX_OK[0]=False

def _existing(pkg, name):
    p=pkg.rstrip("/")+"/"+name
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(p): return unreal.load_asset(p)
    except Exception: pass
    return None

def _import_texture(name):
    entry=_MANIFEST.get(name) or _MANIFEST_LC.get(name.lower())   # texture names vary in case per mission
    if not entry: return None
    stem=_os.path.splitext(entry["png"])[0]
    ex=_existing(MAT_ROOT, stem)                       # reuse if already imported (re-run safe)
    if ex is not None: return ex
    png=_os.path.join(_TEX_ROOT[0], entry["png"])
    if not _os.path.isfile(png): unreal.log_warning("  missing PNG %s"%png); return None
    task=unreal.AssetImportTask()
    task.set_editor_property("filename",png); task.set_editor_property("destination_path",MAT_ROOT)
    task.set_editor_property("automated",True); task.set_editor_property("replace_existing",True)
    task.set_editor_property("save",False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    outs=task.get_editor_property("imported_object_paths")
    return unreal.load_asset(outs[0]) if outs else None

def _tex_res(tex):
    f=getattr(tex,"blueprint_get_size_x",None)
    if callable(f):
        try: return float(f())
        except Exception: pass
    try:
        s=tex.get_editor_property("imported_size"); return float(s.x)
    except Exception: return None

def _make_material(name, tex):
    ex=_existing(MAT_ROOT, "M_"+name)                  # reuse if already created (re-run safe)
    if ex is not None: return ex
    MEL=unreal.MaterialEditingLibrary
    at=unreal.AssetToolsHelpers.get_asset_tools()
    mat=at.create_asset("M_"+name, MAT_ROOT, unreal.Material, unreal.MaterialFactoryNew())
    try:
        ts=MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample)
        ts.set_editor_property("texture",tex)
        if RES_SCALING:                                # tile by resolution -> Dark constant texel density
            res=_tex_res(tex) or TEXEL_REF_PX
            til=TEXEL_REF_PX/res                       # >64px texture -> tiling <1 -> bigger world tile
            tc=MEL.create_material_expression(mat, unreal.MaterialExpressionTextureCoordinate)
            tc.set_editor_property("u_tiling", til); tc.set_editor_property("v_tiling", til)
            MEL.connect_material_expressions(tc, "", ts, "UVs")
        MEL.connect_material_property(ts,"RGB",unreal.MaterialProperty.MP_BASE_COLOR)
        MEL.recompile_material(mat)
    except Exception as e:
        unreal.log_warning("  material wiring failed for %s (%s)"%(name,e))
    return mat

def _default_material():
    if _DEFAULT_MAT[0] is None:
        _DEFAULT_MAT[0]=_existing(MAT_ROOT,"M_Inherit_Default") or \
            unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                "M_Inherit_Default", MAT_ROOT, unreal.Material, unreal.MaterialFactoryNew())
    return _DEFAULT_MAT[0]

def material_id(tex_name):
    if not _TEX_OK[0] or tex_name is None: return 0
    key=tex_name.lower()                                 # one material per texture regardless of case
    if key not in _MATS:
        tex=_import_texture(tex_name)
        mat=_make_material(tex_name,tex) if tex else _default_material()
        _MATS[key]=(_NEXTID[0],mat); _NEXTID[0]+=1
    return _MATS[key][0]

def _tri_normal(mesh,tid):
    for call in ((mesh,tid),(mesh,tid,True)):
        try:
            r=getattr(GQ_NORM,_TRINORM)(*call)
            return r[0] if isinstance(r,(tuple,list)) else r
        except Exception: continue
    return None

def _set_matid_tri(mesh,tid,mid):
    try: getattr(GMAT_TRI,_SETMATTRI)(mesh,tid,mid); return True
    except Exception: return False

def _tag_by_selection(mesh, faces, dom):
    # one selection per face, by normal, then set material id on that selection
    for f in faces:
        tex=f.get("tex") or dom
        mid=material_id(tex); n=f["n"]; nv=unreal.Vector(n[0],n[1],n[2])
        sel=None
        for call in ((mesh,nv,15.0),(mesh,nv,15.0,True),(mesh,unreal.GeometryScriptMeshSelection(),nv,15.0)):
            try:
                r=getattr(GSEL_N,_SELN)(*call); sel=r[0] if isinstance(r,(tuple,list)) else r; break
            except Exception: continue
        if sel is None: return False
        ok=False
        for call in ((mesh,sel,mid),(mesh,None,sel,mid)):
            try: getattr(GMAT_SEL,_SETMATSEL)(*call); ok=True; break
            except Exception: continue
        if not ok: return False
    return True

def _tag_by_triangle(mesh, faces, dom):
    tc=tri_count(mesh)
    if not tc or tc<0: return False
    any_ok=False
    for tid in range(tc):
        n=_tri_normal(mesh,tid)
        if n is None: continue
        best=None; bd=-2.0
        for f in faces:
            fn=f["n"]; d=n.x*fn[0]+n.y*fn[1]+n.z*fn[2]
            if d>bd: bd=d; best=f
        tex=(best.get("tex") if best else None) or dom
        if _set_matid_tri(mesh,tid,material_id(tex)): any_ok=True
    return any_ok

def _enable_matids(mesh):
    if _ENMATID:
        try: getattr(GMAT_EN,_ENMATID)(mesh)
        except Exception: pass

_TAG_MODE=[None]   # 'tri' | 'sel' | 'off' - decided on first brush, then reused
def tag_materials(mesh, b):
    if not _TEX_OK[0]: return
    faces=b.get("faces") or []
    if not faces: return
    _enable_matids(mesh)
    texs=[f.get("tex") for f in faces if f.get("tex")]
    dom=max(set(texs),key=texs.count) if texs else None      # inherit fallback: brush's dominant texture
    if _TAG_MODE[0]=="off": return
    if _TAG_MODE[0]=="tri": _tag_by_triangle(mesh,faces,dom); return
    if _TAG_MODE[0]=="sel": _tag_by_selection(mesh,faces,dom); return
    # first brush: prefer the per-triangle path (confirmed signatures), then selection
    if _TRINORM and _SETMATTRI and _tag_by_triangle(mesh,faces,dom): _TAG_MODE[0]="tri"; unreal.log("  material tagging: per-triangle"); return
    if _SELN and _SETMATSEL and _tag_by_selection(mesh,faces,dom): _TAG_MODE[0]="sel"; unreal.log("  material tagging: selection-by-normal"); return
    _TAG_MODE[0]="off"; unreal.log_warning("  material tagging failed both ways; textures disabled")

def apply_world_uvs(mesh):
    """Fixed world-scale box UVs (one repeat per UV_TILE_CM) over the whole mesh."""
    if not _UVBOX: unreal.log_warning("  UV: no box-projection function; UVs unchanged"); return
    t=unreal.Transform()
    t.set_editor_property("scale3d", unreal.Vector(UV_TILE_CM,UV_TILE_CM,UV_TILE_CM))
    esel=unreal.GeometryScriptMeshSelection()
    for a in ((mesh,0,t,esel,2),(mesh,0,t,esel),(mesh,0,t),(mesh,0,t,None),(mesh,t)):
        try:
            getattr(GUV_BOX,_UVBOX)(*a); unreal.log("  UV: box projection applied (args=%d)"%len(a)); return
        except Exception: continue
    unreal.log_warning("  UV: box projection failed on all signatures")

def _pick_selection(r):
    # the function returns (target_mesh, selection) - grab the GeometryScriptMeshSelection, not the mesh
    if isinstance(r,(tuple,list)):
        for x in r:
            if isinstance(x, unreal.GeometryScriptMeshSelection): return x
        return r[-1]
    return r

def _select_by_normal(mesh, nv):
    for call in ((mesh,nv,15.0),(mesh,nv,15.0,True)):
        try:
            r=getattr(GSEL_N,_SELN)(*call); sel=_pick_selection(r)
            if sel is not None and not isinstance(sel, unreal.DynamicMesh): return sel
        except Exception: continue
    return None

def _face_res(tex):
    if not tex: return TEXEL_REF_PX
    e=_MANIFEST.get(tex) or _MANIFEST_LC.get(tex.lower())
    if e and e.get("size"): return float(e["size"][0])
    return TEXEL_REF_PX

def apply_face_uvs(mesh, b):
    """Per-face planar UVs honoring the face's Dark scale, rotation, and U/V offset.
       World tile size (feet) = texture_pixels * 2^(scale-20)  (verified vs DromEd)."""
    if not _PERFACE_UV[0]: return
    if not (GUV_PLN and GSEL_N):
        _PERFACE_UV[0]=False; unreal.log_warning("  per-face UV: planar/selection funcs missing"); return
    faces=b.get("faces") or []
    if not faces: return          # e.g. the world box (no per-face data) - skip WITHOUT disabling per-face
    for f in faces:
        n=f["n"]; nv=unreal.Vector(n[0],n[1],n[2])
        sc=int(f.get("sc",16)); res=_face_res(f.get("tex"))
        tile=res*(2.0**(sc-20))*FEET_CM                 # world tile in cm
        rot=math.radians(float(f.get("rot",0.0) or 0.0))+math.pi   # base was 180 deg off (verified vs DromEd)
        uoff=float(f.get("uoff",0.0) or 0.0); voff=float(f.get("voff",0.0) or 0.0)
        sel=_select_by_normal(mesh,nv)
        if sel is None: _PERFACE_UV[0]=False; unreal.log_warning("  per-face UV: no normal selection; using global box UV"); return
        # CONSISTENT in-plane axes: walls -> V points up (+Z), U horizontal; floor/ceiling -> fixed X/Y.
        if abs(n[2])>0.99:                               # top / bottom
            u0=[1.0,0.0,0.0]; v0=[0.0,1.0,0.0]
        else:                                            # walls (normal horizontal)
            u0=_norm(_cross([0.0,0.0,1.0], n)); v0=_norm(_cross(n,u0))
        c=math.cos(rot); s=math.sin(rot)
        U=[c*u0[i]+s*v0[i] for i in range(3)]
        V=[-s*u0[i]+c*v0[i] for i in range(3)]
        t=unreal.Transform()
        t.set_editor_property("rotation", _quat_from_axes([U,V,list(n)]))
        off=[(uoff*tile)*U[i]+(voff*tile)*V[i] for i in range(3)]   # shift projection origin by the offset
        t.set_editor_property("translation", unreal.Vector(off[0],off[1],off[2]))
        t.set_editor_property("scale3d", unreal.Vector(tile,tile,1.0))
        ok=False; err=None
        for call in ((mesh,0,t,sel),(mesh,0,t,sel,True),(mesh,sel,0,t)):
            try: getattr(GUV_PLN,_UVPLN)(*call); ok=True; break
            except Exception as e: err=e; continue
        if not ok:
            _PERFACE_UV[0]=False
            unreal.log_warning("  per-face UV: planar projection failed (%s); using global box UV"%err); return
        if not _PERFACE_LOGGED[0]:
            _PERFACE_LOGGED[0]=True
            unreal.log("  per-face UV OK (e.g. %s sc=%d -> tile=%.0fcm)"%(f.get("tex"),sc,tile))

def assign_materials(sm):
    if not _TEX_OK[0]: return
    try: sm.set_material(0, _default_material())
    except Exception: pass
    for name,(mid,mat) in _MATS.items():
        try: sm.set_material(mid, mat)
        except Exception as e: unreal.log_warning("  slot %d (%s) assign failed: %s"%(mid,name,e))
    unreal.log("Assigned %d materials (+default)"%len(_MATS))

def mk(b):
    if b.get("shape")=="box":      m=mk_box(b)
    elif b.get("shape")=="wedge":  m=mk_wedge(b)
    elif b.get("shape")=="cylinder": m=mk_cylinder(b)
    else:                          m=mk_buffer(b)
    if _TEX_OK[0]:
        tag_materials(m,b)
        apply_face_uvs(m,b)          # per-face UVs at Dark scale (before boolean, so they carry through)
    return m

def bake(mesh, path):
    # if the asset already exists, overwrite its mesh IN PLACE (no "Overwrite Existing Object" dialog)
    try:
        if GCOPY and unreal.EditorAssetLibrary.does_asset_exist(path):
            sm=unreal.load_asset(path); co=CopyOpts()
            for args in ([mesh,sm,co],[mesh,sm]):
                if co is None and len(args)>2: continue
                try:
                    getattr(GCOPY,COPY_FN)(*args); return sm
                except Exception: continue
    except Exception: pass
    fn=getattr(GASSET,ASSET_FN); pkg,name=path.rsplit("/",1); o=AssetOpts()
    for args in ([mesh,pkg,name,o],[mesh,pkg,name],[mesh,path,o],[mesh,path]):
        if o is None and len(args)>3: continue
        try:
            r=fn(*args); return r[0] if isinstance(r,(tuple,list)) else r
        except Exception: pass
    return None
def set_collision(sm):
    """Level geometry: use the render triangles directly as collision (complex-as-simple)."""
    if not BUILD_COLLISION: return
    try:
        bs=sm.get_editor_property("body_setup")
        if bs is None:                                  # force a body setup to exist
            try:
                ses=unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
                ses.set_convex_decomposition_collisions(sm,0,0)   # creates body setup; we override below
                bs=sm.get_editor_property("body_setup")
            except Exception: pass
        if bs is not None:
            bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
            sm.set_editor_property("body_setup", bs)
            try: bs.invalidate_physics_data()
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_loaded_asset(sm)
            except Exception:
                try: sm.mark_package_dirty()
                except Exception: pass
            unreal.log("  collision: complex-as-simple enabled")
        else:
            unreal.log_warning("  could not access body_setup; collision not set")
    except Exception as e:
        unreal.log_warning("  set_collision failed: %s"%e)

def spawn(sm, label):
    eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    try:                                                # reuse an actor with this label (no duplicates)
        for a in eas.get_all_level_actors():
            if isinstance(a,unreal.StaticMeshActor) and a.get_actor_label()==label:
                a.static_mesh_component.set_static_mesh(sm); return a
    except Exception: pass
    a=eas.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,0))
    a.static_mesh_component.set_static_mesh(sm); a.set_actor_label(label); return a

# ---------------------------------------------------------------- media state machine (direct, solid-first)
def run():
    if BUILD_TEXTURES:
        _tex_report()
        if _TEX_OK[0]: _load_manifest()
    with open(GEO_PATH) as fh: data=json.load(fh)
    B=sorted(data["brushes"], key=lambda x:x["time"])
    world=B[0]; body=B[1:]
    if BUILD_TEXTURES:
        nf=sum(1 for b in B if b.get("faces"))
        unreal.log("Face-texture data: %d/%d brushes carry 'faces'"%(nf,len(B)))
        if nf==0:
            unreal.log_warning("  -> this geo has NO per-face texture data. Regenerate it with the "
                               "current mis_to_geo.py (py mis_to_geo.py 06.mis 06_geo.json).")
    if TEST_LIMIT: body=body[:TEST_LIMIT]
    unreal.log("Media CSG (primitives): %d brushes, water=%s"%(len(body),BUILD_WATER))
    O=BoolOpts()
    result=mk(world)          # start fully solid (the enclosing world box)
    unreal.log("  world solid tris=%s"%tri_count(result))
    WATER=new_mesh()
    fails=[0]; first_err=[None]
    def BOP(tgt,tool,op): GB.apply_mesh_boolean(tgt,I,tool,I,op,O)
    n=len(body)
    for i,b in enumerate(body,1):
        op=b["op"]
        try:
            if   op==0:  BOP(result,mk(b),UNION);    BOP(WATER,mk(b),SUBTRACT)
            elif op==1:  BOP(result,mk(b),SUBTRACT); BOP(WATER,mk(b),SUBTRACT)
            elif op==2:  BOP(result,mk(b),SUBTRACT); BOP(WATER,mk(b),UNION)
            elif op==3:  t=mk(b); BOP(t,result,SUBTRACT); BOP(WATER,t,UNION)
            elif op==4:  BOP(WATER,mk(b),SUBTRACT)
            elif op==5:  t=mk(b); BOP(t,result,INTERSECT); BOP(WATER,t,UNION); BOP(result,mk(b),SUBTRACT)
            elif op==8:  t=mk(b); BOP(t,WATER,INTERSECT); BOP(result,t,UNION); BOP(WATER,mk(b),SUBTRACT)
        except Exception as e:
            fails[0]+=1
            if first_err[0] is None: first_err[0]=str(e)
        if i%250==0: unreal.log("  ...%d/%d  result tris=%s"%(i,n,tri_count(result)))
    unreal.log("Done carving: result tris=%s  WATER tris=%s  boolean failures=%d"%(
        tri_count(result),tri_count(WATER),fails[0]))
    if first_err[0]: unreal.log_error("First boolean error: %s"%first_err[0])

    ensure_uv_normals(result)
    if _TEX_OK[0]:
        if _PERFACE_UV[0]:
            unreal.log("UVs: per-face Dark scale  (tile ft = px * 2^(scale-20))")
        else:
            apply_world_uvs(result)   # fallback: uniform box projection if per-face UV was unavailable
            unreal.log("UVs: world-scale box projection (one repeat / %.0f cm)"%UV_TILE_CM)
    sm=bake(result, ASSET_PATH)
    if sm is None: unreal.log_error("bake failed"); return
    assign_materials(sm)
    set_collision(sm)
    spawn(sm, ASSET_PATH.rsplit("/",1)[-1])
    unreal.log("Level -> %s"%ASSET_PATH)

    if BUILD_WATER:
        if tri_count(WATER)==0:
            unreal.log("No water in this mission - skipping water mesh.")
        else:
            ensure_uv_normals(WATER)
            wpath=ASSET_PATH+"_Water"; wsm=bake(WATER, wpath)
            if wsm is not None:
                spawn(wsm, wpath.rsplit("/",1)[-1])
                unreal.log("Water -> %s  (assign a translucent, two-sided material)"%wpath)
            else: unreal.log_warning("water bake failed")
run()