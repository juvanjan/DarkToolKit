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

GEO_PATH    = r"C:\Nex\DarkSimProject\DarkSimToolkit\test_missions\16_geo.json"   # <-- SET (the *_geo.json)
ASSET_PATH  = r"/Game/Mission/SM_Mission"
BUILD_WATER = True        # also bake the water volume as a separate static mesh (SM_..._Water)
BUILD_COLLISION = True    # give the mesh collision (complex-as-simple: the triangles ARE the collision)
BUILD_WORLD_BOX = True    # True: start from the enclosing solid cuboid (brush 0) and carve into it (Dark
                          #   default). False: start EMPTY - only additive/solid brushes appear, air carves
                          #   have nothing to cut (useful to preview individual brushes without the shell).
STRIP_WORLD_SHELL = True  # drop the OUTWARD-facing skin of that enclosing cuboid. See _shell_planes().
REBUILD_MATERIALS = True  # WATER material only: delete + recreate instead of reusing. Materials are
                          #   cached BY ASSET NAME, so an edit to the material graph stays invisible
                          #   until the old asset goes - a stale static M_grin is exactly why the
                          #   flipbook did not appear. Cheap here (one asset); the ~170 per-face
                          #   materials are still reused. Set False once the graph is settled.
WATER_PROBE_CM = 4.0      # how far outside a water face to sample the medium when deciding whether
                          #   that face is a water/air surface (drawn) or buried against solid (not).
PROBE_ID9 = False         # diagnostic: log which of the id9 pit surfaces the boolean produced (16.mis).
                          #   Hardcoded to 16.mis coordinates; flip to True when chasing missing faces.
FILL_UNION_GROW_CM = 0.0  # 0 = OFF (geometry stays DromEd-exact). Inflates the fill-solid UNION tool's
                          #   side walls (mk_box `_grow_xy`) to bury them in surrounding solid rather
                          #   than coincide with the cavity walls. Kept because the mechanism is sound,
                          #   but it did NOT cause the 16.mis missing pit walls: 1cm and 30cm produced
                          #   byte-identical output, which is what disproved the coincidence theory.
                          #   The real cause was triangle-id gaps - see tri_ids(). Leave at 0 unless a
                          #   genuine coplanar-union artifact is proven; it perturbs real geometry.
TEST_LIMIT  = 0          # 0 = full mission; >0 = world solid + first N brushes (quick preview)
UV_TILE_CM  = 64.0        # world-space texture tile size (one texture repeat per this many cm)
                          #   calibrated to Dark scale 16 (~2.1 ft/tile). Halve it -> texture bigger.

RES_SCALING = False       # per-texture material tiling (superseded by per-face UV formula below)
TEXEL_REF_PX = 64.0       # fallback texture width if a texture's size is unknown
FEET_CM = 30.48           # Dark world tile (feet) = pixels * 2^(scale-20); this converts feet -> cm
TEX_SHIFT_U = 0.0         # U needs no shift
TEX_SHIFT_V = 0.0         # V needs no shift
MIRROR_TEX_U = True       # level is Y-reflected -> textures come out horizontally mirrored (a left arrow
                          #   reads as a right arrow). Flip the U axis of every face's UV to undo it.
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
_NLIB,_NFN=method_like("recompute","normal")
# NOTE: UE spells these set_mesh_u_vs_from_* -> the substring is "u_vs", NOT "uv". method_like("uv",..)
# silently matched nothing, so this lib was None for the whole project's life (log: "uvs=False (no lib)").
_UVLIB,_UVFN=method_like("u_vs","box","projection")
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
_EUVLOG=[False]
def ensure_uv_normals(m, uvs=True):
    """Seed the normal (and optionally UV) attribute overlays.

    `uvs=False` matters: _UVFN is resolved by NAME (method_like("uv","projection")), so it can land on
    set_mesh_uvs_from_PLANAR_projection. Run over a whole level at identity transform that projects
    everything down one axis - horizontal faces get a (densely tiled) mapping while VERTICAL faces are
    edge-on and collapse to a line, i.e. barcode stripes. Harmless when called on a fresh buffer mesh
    before apply_face_uvs overwrites it; destructive when called on the finished mesh AFTER all the
    per-face projections. The final-assembly call now passes uvs=False for exactly that reason."""
    nok=uok=False
    if _NLIB:
        for a in ((m,),(m,None),(m,None,None),(m,unreal.GeometryScriptCalculateNormalsOptions())
                  if hasattr(unreal,"GeometryScriptCalculateNormalsOptions") else (m,)):
            try: getattr(_NLIB,_NFN)(*(a if isinstance(a,tuple) else (a,))); nok=True; break
            except Exception: continue
    if uvs and _UVLIB:
        # Use a WORLD-SCALED transform, never the identity. _UVFN is resolved by name and its transform
        # maps world units -> UV units, so identity means ONE TEXTURE REPEAT PER CENTIMETRE: on a 5 m
        # wall that is 500 repeats, which aliases on screen into exactly the fine stripes / "barcode"
        # look. UV_TILE_CM gives one repeat per tile instead.
        tf=unreal.Transform()
        tf.set_editor_property("scale3d", unreal.Vector(UV_TILE_CM,UV_TILE_CM,UV_TILE_CM))
        for a in ((m,0,tf),(m,0,None,tf),(m,tf),(m,0)):
            try: getattr(_UVLIB,_UVFN)(*a); uok=True; break
            except Exception: continue
    if not _EUVLOG[0]:
        _EUVLOG[0]=True
        unreal.log("  ensure_uv_normals: normals=%s (%s)  uvs=%s (%s)"%(nok,_NFN or "no lib",uok,_UVFN or "no lib"))
    return nok,uok

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
    # Prefer the orientation baked into the geo. recover_box() infers the frame from the 8 vertices by
    # eigen-decomposition, which is AMBIGUOUS whenever two extents are equal - the eigenvalues are
    # degenerate and the eigenvectors arbitrary within that subspace, so cubes and square-plan boxes
    # came out spuriously rotated (MISS5 id531 24x24x24 -> 69.7 deg, id2846 36x36x16 -> 45 deg) despite
    # H=P=B=0 in the record. Fall back to recovery only for geo written before `axes` existed.
    if b.get("axes") and b.get("ext"):
        V=b["verts"]; n=float(len(V)) or 1.0
        C=[sum(v[i] for v in V)/n for i in range(3)]
        ax=[list(a) for a in b["axes"]]; ext=list(b["ext"])
    else:
        C,ax,ext=recover_box(b["verts"])
    m=new_mesh()
    t=unreal.Transform()
    t.set_editor_property("translation", unreal.Vector(C[0],C[1],C[2]))
    t.set_editor_property("rotation", _quat_from_axes(ax))
    ext=list(ext)
    if b.get("_grow_xy"):
        # Grow the two NON-VERTICAL local extents outward (center fixed, so both opposing walls move
        # out by _grow_xy). Used only for the fill-solid UNION tool: it refills the bottom of a carved
        # cavity, and its side walls are otherwise EXACTLY coincident with the cavity walls -> the
        # GeometryScript boolean discards the whole shared-plane polygon, taking the wall above the
        # fill with it (the missing pit walls in 16.mis). Buried in surrounding solid, the union is a
        # no-op there, and the vertical extent (the visible floor) is left untouched.
        up=max(range(3), key=lambda k:abs(ax[k][2]))   # local axis most aligned with world +Z
        g=float(b["_grow_xy"])
        for k in range(3):
            if k!=up: ext[k]+=g
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
    # A degenerate / near-zero-radius cross-section makes an eigenvalue <= 0, and 1/sqrt() then raised
    # "math domain error" -> the whole cylinder fell back to the raw buffer path. Clamp instead.
    if l1<=1e-12 or l2<=1e-12: return None
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
    # FACE-ALIGNED cylinders: build straight from the geo verts. recover_cylinder fits an ELLIPSE and
    # takes the rotation from its major axis; a circular cross-section has no major axis, so it can't
    # recover the half-facet face-alignment phase (id7 came out 22.5 deg off), and rotating the fitted
    # frame afterwards left the rebuilt facets' planes 74-132cm off the geo face polys -> retag missed
    # 6 of 8 sides. mk_buffer builds the exact geo mesh (cyl_local already emits the face-aligned ring),
    # so mesh == geo face polys and texturing stays in phase. Vertex-aligned cylinders keep the
    # recover_cylinder path, which works and gives clean UE primitive UVs.
    if b.get("falign"): return mk_buffer(b)
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

def _fallback_matid():
    """Most-used material id, for triangles no face could be matched to. Anything is better than 0
    (the default grey), which is what produced the untextured patches on MISS5 id4 / id49."""
    if not _MATS: return 0
    return max(_MATS.values(), key=lambda v: v[0])[0] if len(_MATS)==1 else            sorted(_MATS.values(), key=lambda v: v[0])[0][0]

_BUFLOG=[False]
def mk_buffer(b):
    """Build straight from the geo's verts/tris. NOTE this is the only path that does NOT go through a
    GeometryScript primitive (append_box / append_cylinder / plane-cut), which has two consequences:

    WINDING: the geo's triangles are wound so cross(q-p, r-p) points OUTWARD (orient() forces a
    positive signed volume in UE space). append_buffers_to_mesh wants the opposite handedness, so a
    buffer-built shape comes out INSIDE-OUT: faces visible only from within, and - because an inverted
    solid has negative volume - the CSG union emits a shell instead of adding volume ("two rooms with
    a wall between"). It also makes textures look mirrored, immune to any UV fix. Hence the reversal.

    ATTRIBUTES: primitives create the UV + normal overlays; a bare vertices/triangles buffer does not,
    so per-face planar projections have nothing to write into and every face samples a single texel
    (one flat colour per face). Hence the explicit normals/uv0 below."""
    if GEDIT is None: return mk_box(b)  # last resort
    m=new_mesh(); buf=MeshBuffers()
    V=b["verts"]
    T=[(t[0],t[2],t[1]) for t in b["tris"]]          # reverse winding for UE (see docstring)
    nrm=[[0.0,0.0,0.0] for _ in V]                   # per-vertex normals from the REVERSED tris
    for t in T:
        p,q,r=V[t[0]],V[t[1]],V[t[2]]
        cx=_cross([q[i]-p[i] for i in range(3)],[r[i]-p[i] for i in range(3)])
        for vi in t:
            for i in range(3): nrm[vi][i]+=cx[i]
    nrm=[_norm(n) if (n[0] or n[1] or n[2]) else [0.0,0.0,1.0] for n in nrm]
    got=[]
    buf.set_editor_property("vertices",  [unreal.Vector(v[0],v[1],v[2]) for v in V])
    buf.set_editor_property("triangles", [unreal.IntVector(t[0],t[1],t[2]) for t in T])
    for prop,val in (("normals",[unreal.Vector(n[0],n[1],n[2]) for n in nrm]),
                     ("uv0",    [unreal.Vector2D(v[0]/100.0, v[1]/100.0) for v in V])):
        try: buf.set_editor_property(prop,val); got.append(prop)
        except Exception as e:
            if not _BUFLOG[0]: unreal.log_warning("  mk_buffer: could not set '%s' (%s)"%(prop,e))
    GEDIT.append_buffers_to_mesh(m, buf)
    ensure_uv_normals(m)                             # belt-and-braces overlay initialisation
    if not _BUFLOG[0]:
        _BUFLOG[0]=True
        unreal.log("  mk_buffer: %d verts %d tris, winding reversed for UE, attrs set: %s"
                   %(len(V),len(T),",".join(got) or "NONE"))
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

# ---------------------------------------------------------------- triangle ID enumeration
# A UE DynamicMesh does NOT guarantee triangle IDs are 0..count-1. Every boolean deletes triangles,
# and the freed IDs leave GAPS: after carving 16.mis the mesh held 110 triangles spread over a LARGER
# id space. Iterating `range(tri_count(m))` therefore did two wrong things at once:
#   - it queried ids inside the range that are DELETED; the query returns zeros, which showed up as
#     11 bogus "degenerate" triangles at (0,0,0) with a zero normal, and
#   - it never visited the 11 REAL triangles whose ids sit past the count.
# rebuild_unwelded copies the mesh triangle by triangle, so those unvisited triangles were dropped
# from the final mesh entirely -> see-through holes (the id9 pit walls). ALWAYS enumerate with
# tri_ids(); never with range(tri_count(...)).
GMAT_ALL,_SETMATALL = _find(["set_all_triangle_material_i_ds","set_all_triangle_material_ids"],
                            "set","all","material")
GMAT_DEL,_MATDEL = _find(["delete_triangles_by_material_id"],"delete","material")
GQ_NIDS, _NIDS = _find(["get_num_triangle_i_ds","get_num_triangle_ids"],"num","triangle","ids")
GQ_VALID,_VALID = _find(["is_valid_triangle_id"],"valid","triangle","id")

def tri_ids(m):
    """Valid triangle ids of m, in order. Falls back to range(count) only if the API is unavailable."""
    n=-1
    if GQ_NIDS:
        try: n=int(getattr(GQ_NIDS,_NIDS)(m))
        except Exception: n=-1
    if n<0: n=tri_count(m)
    if n<0: return []
    if not GQ_VALID: return list(range(n))
    out=[]
    for t in range(n):
        try:
            if getattr(GQ_VALID,_VALID)(m,t): out.append(t)
        except Exception: out.append(t)
    return out

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
# final-mesh retag (coincident-face override: latest brush wins, like Dark)
GTRIIDS,_ALLTRI = _find(["get_all_triangle_i_ds"],"all","triangle","id")
GTRIPOS,_TRIPOS = _find(["get_triangle_positions"],"triangle","positions")
GSEL_IDX,_SELIDX = _find(["convert_index_list_to_mesh_selection","convert_index_array_to_mesh_selection"],"index","selection")
GSEL_BOX,_SELBOX = _find(["select_mesh_elements_in_box"],"select","box")   # geometric per-face selection (crash-safe)
# Diagnostic: restrict the POST-BOOLEAN re-projection to one face orientation.
#   ""          re-project every face (normal behaviour)
#   "vertical"  only faces with a horizontal normal
#   "horizontal" only faces with a vertical normal
# If the boolean's UV overlay is WELDED (faces sharing a vertex share its UV element, last write wins),
# then "vertical" should make walls correct and floors collapse - the exact inverse of what we see now.
# That inversion is the proof; a partial change means something else is going on.
RETAG_UV_ONLY = ""           # diagnostic (see above); "" = normal
REBUILD_UNWELDED = True      # rebuild the final mesh with per-triangle vertices + analytic UVs. This
                             # is the real fix for the welded-overlay problem; set False to compare.

FINAL_RETAG = True        # after boolean, re-assign each face the LATEST brush covering it (Dark override)
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

_IMPFAIL=[]
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
    if not outs:
        # SILENT FAILURE was the trap: material_id() still hands out a valid non-zero id, but binds
        # _default_material() (plain grey) to it, so the face renders untextured while every log line
        # says the texture was assigned. Name the texture instead.
        _IMPFAIL.append(name); unreal.log_warning("  texture import FAILED: %s (%s)"%(name,png)); return None
    a=unreal.load_asset(outs[0])
    if a is None: _IMPFAIL.append(name); unreal.log_warning("  texture import returned nothing: %s"%name)
    return a

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

def _import_png(fname):
    """Import an arbitrary PNG from the texture folder (used for animation atlases)."""
    stem=_os.path.splitext(fname)[0]
    ex=_existing(MAT_ROOT, stem)
    if ex is not None: return ex
    png=_os.path.join(_TEX_ROOT[0], fname)
    if not _os.path.isfile(png): unreal.log_warning("  missing PNG %s"%png); return None
    task=unreal.AssetImportTask()
    task.set_editor_property("filename",png); task.set_editor_property("destination_path",MAT_ROOT)
    task.set_editor_property("automated",True); task.set_editor_property("replace_existing",True)
    task.set_editor_property("save",False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    outs=task.get_editor_property("imported_object_paths")
    if not outs: unreal.log_warning("  atlas import FAILED: %s"%png); return None
    return unreal.load_asset(outs[0])

# HLSL for one Custom node: map the surface UV into the current frame's cell of a sprite-sheet atlas.
# frac(UV) keeps a TILING surface inside its cell (water tiles many times across a pool). Mip
# selection across that frac seam is why the atlas is imported with mipmaps OFF.
_FLIPBOOK_HLSL = """
float ph = frac(T / %(loop)ff);
int fi = (int)floor(ph * %(frames)d.0);
fi = min(fi, %(frames)d - 1);
float c = (float)(fi %% %(cols)d);
float r = (float)(fi / %(cols)d);
return float2((frac(UV.x) + c) / %(cols)d.0, (frac(UV.y) + r) / %(rows)d.0);
"""

def _make_water_material(name):
    """Translucent two-sided water material, animated when the family ships frames.

    Dark never textures a water surface from a brush face: get_texture_for_medium_transition
    (editor/cvtbrush.c:169) substitutes the reserved slots WATERIN_IDX=247 / WATEROUT_IDX=248 by
    crossing direction, and the images come from the mission's water family (fam\\waterhw\\<prefix>
    in/out). The NewDark t2water pack ships 256x256 DDS with real alpha baked in, so the texture's
    own A drives opacity.

    Animation: Dark loads sibling frames <base>_1.._N (render/anim_txt.c) and advances one frame
    every `ani_rate` MILLISECONDS - 60ms x 20 frames = a 1.2s loop for water - in WRAP mode, i.e. a
    plain forward cycle. extract_textures.py packs those frames into one atlas; here a single Custom
    node turns elapsed Time into that frame's cell. If any of the animated wiring fails we fall back
    to the static base frame rather than leaving a broken material."""
    entry=_MANIFEST.get(name) or _MANIFEST_LC.get(name.lower()) or {}
    anim=entry.get("anim")
    # DISTINCT asset name for the animated variant. Materials are reused when the asset already
    # exists, so an M_<name> left over from a build that made a STATIC water material would be
    # returned verbatim and no amount of editing this function would ever show up. The suffix makes
    # the animated material a different asset; REBUILD_MATERIALS forces a rebuild after any later
    # edit to the graph, which reuse would otherwise hide.
    aname="M_"+name+("_anim" if anim else "")
    if REBUILD_MATERIALS:
        try:
            p=MAT_ROOT.rstrip("/")+"/"+aname
            if unreal.EditorAssetLibrary.does_asset_exist(p):
                unreal.EditorAssetLibrary.delete_asset(p)
                unreal.log("  water material: deleted %s to rebuild (REBUILD_MATERIALS)"%aname)
        except Exception as e: unreal.log_warning("  could not delete %s (%s)"%(aname,e))
    else:
        ex=_existing(MAT_ROOT, aname)
        if ex is not None:
            unreal.log("  water material: REUSED existing %s (set REBUILD_MATERIALS=True to "
                       "regenerate it after changing the graph)"%aname)
            return ex
    MEL=unreal.MaterialEditingLibrary
    at=unreal.AssetToolsHelpers.get_asset_tools()
    mat=at.create_asset(aname, MAT_ROOT, unreal.Material, unreal.MaterialFactoryNew())
    try:
        mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
        mat.set_editor_property("two_sided", True)
    except Exception as e:
        unreal.log_warning("  water material flags failed (%s)"%e)
    ts=MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample)
    tex=None; animated=False
    if anim:
        tex=_import_png(anim["atlas"])
        if tex is not None:
            try: tex.set_editor_property("mip_gen_settings",
                                         unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS)
            except Exception: pass
            try:
                ts.set_editor_property("texture",tex)
                uv=MEL.create_material_expression(mat, unreal.MaterialExpressionTextureCoordinate)
                tm=MEL.create_material_expression(mat, unreal.MaterialExpressionTime)
                cu=MEL.create_material_expression(mat, unreal.MaterialExpressionCustom)
                cu.set_editor_property("code", _FLIPBOOK_HLSL % {
                    "loop": max(1e-3, float(anim["loop_s"])), "frames": int(anim["frames"]),
                    "cols": int(anim["cols"]), "rows": int(anim["rows"])})
                cu.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT2)
                ins=[]
                for nm in ("UV","T"):
                    ci=unreal.CustomInput(); ci.set_editor_property("input_name", nm); ins.append(ci)
                cu.set_editor_property("inputs", ins)
                MEL.connect_material_expressions(uv,"",cu,"UV")
                MEL.connect_material_expressions(tm,"",cu,"T")
                MEL.connect_material_expressions(cu,"",ts,"UVs")
                animated=True
            except Exception as e:
                unreal.log_warning("  water flipbook wiring failed (%s) - using the static frame"%e)
                tex=None
    if not animated:
        tex=_import_texture(name)
        if tex is not None:
            try: ts.set_editor_property("texture",tex)
            except Exception as e: unreal.log_warning("  water texture bind failed (%s)"%e)
    try:
        MEL.connect_material_property(ts,"RGB",unreal.MaterialProperty.MP_BASE_COLOR)
        MEL.connect_material_property(ts,"A",  unreal.MaterialProperty.MP_OPACITY)
        MEL.recompile_material(mat)
    except Exception as e:
        unreal.log_warning("  water material wiring failed for %s (%s)"%(name,e))
    if animated:
        unreal.log("  water material: ANIMATED %d frames, %dms/frame (%.2fs %s loop), atlas %dx%d"
                   %(anim["frames"],anim["rate_ms"],anim["loop_s"],anim["mode"],
                     anim["cols"],anim["rows"]))
        # Only WRAP (plain forward cycle) is implemented. REVERSE/PINGPONG bounce at the ends
        # (ectsAnimHitEdge), which the Custom node does not do - say so instead of looking right.
        if anim["mode"]!="WRAP":
            unreal.log_warning("  ani_mode is %s but the flipbook only does WRAP (forward loop); "
                               "the bounce is NOT reproduced"%anim["mode"])
    else:
        unreal.log("  water material: static single frame%s"%("" if not anim else " (atlas unavailable)"))
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
    cone=_safe_cone(faces)        # same over-grab hazard as apply_face_uvs (see _safe_cone)
    for f in faces:
        tex=f.get("tex") or dom
        mid=material_id(tex); n=f["n"]; nv=unreal.Vector(n[0],n[1],n[2])
        sel=None
        for call in ((mesh,nv,cone),(mesh,nv,cone,True),(mesh,unreal.GeometryScriptMeshSelection(),nv,cone)):
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
    ids=tri_ids(mesh)
    if not ids: return False
    any_ok=False
    for tid in ids:
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

def _select_by_normal(mesh, nv, angle=15.0):
    for call in ((mesh,nv,angle),(mesh,nv,angle,True)):
        try:
            r=getattr(GSEL_N,_SELN)(*call); sel=_pick_selection(r)
            if sel is not None and not isinstance(sel, unreal.DynamicMesh): return sel
        except Exception: continue
    return None

GSELINFO,_SELINFO=_find(["get_mesh_selection_info"],"selection","info")
_SELCHK={"n":0,"bad":0,"eg":[]}
def sel_selfcheck(mesh, sel, f):
    """Confirm the selection really is ONE face and not the whole mesh."""
    if GSELINFO is None or _SELCHK["n"]>=40: return
    cnt=None
    for call in ((sel,),(mesh,sel),(sel,True)):
        try:
            r=getattr(GSELINFO,_SELINFO)(*call)
            vals=[x for x in (r if isinstance(r,(tuple,list)) else [r]) if isinstance(x,int)]
            if vals: cnt=max(vals); break
        except Exception: continue
    if cnt is None: return
    tc=tri_count(mesh); _SELCHK["n"]+=1
    n=f.get("n",[0,0,0])
    ax="X" if abs(n[0])>0.9 else ("Y" if abs(n[1])>0.9 else ("Z" if abs(n[2])>0.9 else "tilted"))
    whole = (tc>0 and cnt>=tc)
    if whole or cnt==0:
        _SELCHK["bad"]+=1
        if len(_SELCHK["eg"])<10:
            _SELCHK["eg"].append("normal~%-6s tex=%-10s selection=%d tris of %d in mesh  <-- %s"%(
                ax,f.get("tex"),cnt,tc,"WHOLE MESH (projection not restricted!)" if whole else "EMPTY"))
    elif len(_SELCHK["eg"])<10:
        _SELCHK["eg"].append("normal~%-6s tex=%-10s selection=%d tris of %d in mesh  ok"%(ax,f.get("tex"),cnt,tc))

def sel_selfcheck_report():
    if GSELINFO is None:
        unreal.log_warning("Selection self-check: get_mesh_selection_info unavailable - skipped"); return
    unreal.log("Selection self-check: sampled %d faces, %d not restricted to one face"%(_SELCHK["n"],_SELCHK["bad"]))
    for e in _SELCHK["eg"]: unreal.log("    %s"%e)
    if _SELCHK["bad"]:
        unreal.log_error("    ^ ROOT CAUSE: an unrestricted selection makes the planar projection apply to")
        unreal.log_error("      the ENTIRE brush, so the LAST face processed wins. On a box that is the top")
        unreal.log_error("      face, whose Z projection is edge-on to all four sides -> stripes down them.")

def _safe_cone(faces, default=15.0):
    """Half-angle for select-by-normal, shrunk so a selection can only ever hit ONE face.
    A fixed 15 deg cone over-grabs on densely faceted brushes - a 14-gon pyramid's sides are 14.4 deg
    apart and 11.mis's cornerpyramid gets down to 8.3 deg - so each face's planar projection also
    lands on its neighbours and the last write wins (smeared texturing, plus flat single-colour facets
    where an inherited projection ends up edge-on). Faces are flat, so their own triangles sit exactly
    on the normal and any positive cone still captures them."""
    ns=[f.get("n") for f in faces if f.get("n")]
    mn=180.0
    for i in range(len(ns)):
        for j in range(i+1,len(ns)):
            d=ns[i][0]*ns[j][0]+ns[i][1]*ns[j][1]+ns[i][2]*ns[j][2]
            a=math.degrees(math.acos(max(-1.0,min(1.0,d))))
            if a<mn: mn=a
    return max(1.0, min(default, 0.4*mn))

# Dark computes texture SCALE from each texture's *logical* (original) resolution, which is NOT
# necessarily the file resolution. HD texture packs replace a stock 256px texture with a 512px image
# but Dark still scales it as 256 -> our tile came out 2x too big. Keep the 512 file for DISPLAY, but
# feed the logical size into the tile formula. Custom textures (aaaa/bbbb/cccc) have matching logical
# and file sizes, so they need no override and stay correct.
#   Priority: this manual override map -> manifest "scale_px" (extractor: fam.crf original size)
#   -> manifest "size" -> TEXEL_REF_PX default.
SCALE_PX_OVERRIDE = {}         # texture name (lowercase) -> logical resolution; empty (extractor drives it
                               # via manifest scale_px). Manual escape hatch if any texture ever mismaps.

def _face_res_wh(tex):
    """(logical width, logical height) in texels. Height follows the display aspect ratio, which the
    HD override preserves, so a 256x512 file with a 128px logical width is 128x256 logically."""
    w=_face_res(tex)
    e=_MANIFEST_LC.get((tex or "").lower()) or {}
    sz=e.get("size")
    if sz and len(sz)>=2 and sz[0]:
        return w, w*float(sz[1])/float(sz[0])
    return w, w

def _face_res(tex):
    if not tex: return TEXEL_REF_PX
    key=tex.lower()
    if key in SCALE_PX_OVERRIDE: return SCALE_PX_OVERRIDE[key]
    e=_MANIFEST.get(tex) or _MANIFEST_LC.get(tex.lower())
    if e and e.get("scale_px"): return float(e["scale_px"])   # fam.crf original size (HD-override aware)
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
    cone=_safe_cone(faces)        # never wide enough to grab a neighbouring facet (see _safe_cone)
    _okn=[0]
    for f in faces:
        n=f["n"]; nv=unreal.Vector(n[0],n[1],n[2])
        sc=int(f.get("sc",16))
        tile_u,tile_v,uoff,voff=_tile_uvoff(f)          # per-axis world tile in cm + offsets
        tile=tile_u                                     # (logging only)
        rot=_face_rot(f,n)
        sel=_select_by_normal(mesh,nv,cone)
        if sel is None:
            # Skip THIS face, keep going. This used to set _PERFACE_UV[0]=False and return, which on a
            # 4310-brush mission meant one bad face left every later brush with raw primitive UVs.
            _UVFAIL[0]+=1; _UVFAIL_EG.append("brush %s face %s: no normal selection"%(b.get("id"),f.get("tex")))
            continue
        # CONSISTENT in-plane axes; the frame [U,V,projn] MUST be right-handed or the projection collapses.
        # Caps -> Z; cylinder sides & wedge slants -> dominant world axis (stretch); walls -> perpendicular.
        u0,v0,projn=_uv_basis(n,f)
        c=math.cos(rot); s=math.sin(rot)
        U=[c*u0[i]+s*v0[i] for i in range(3)]
        V=[-s*u0[i]+c*v0[i] for i in range(3)]
        t=unreal.Transform()
        t.set_editor_property("rotation", _quat_from_axes([U,V,projn]))
        # projection origin at world 0 + record offset + a global half-tile correction (Dark vs UE)
        su=-1.0 if MIRROR_TEX_U else 1.0                 # mirror U (level is Y-reflected)
        if f.get("solid"): su=-su                         # solid (union) faces flip opposite to air carves
        if f.get("cylside"): su=-su                        # curved side wraps the opposite way vs the caps
        uu=(uoff+TEX_SHIFT_U)*su; vv=voff+TEX_SHIFT_V
        off=[(uu*tile_u)*U[i]+(vv*tile_v)*V[i] for i in range(3)]
        t.set_editor_property("translation", unreal.Vector(off[0],off[1],off[2]))
        t.set_editor_property("scale3d", unreal.Vector(su*tile_u,tile_v,1.0))
        ok=False; err=None
        for call in ((mesh,0,t,sel),(mesh,0,t,sel,True),(mesh,sel,0,t)):
            try: getattr(GUV_PLN,_UVPLN)(*call); ok=True; break
            except Exception as e: err=e; continue
        if not ok:
            _UVFAIL[0]+=1; _UVFAIL_EG.append("brush %s face %s: projection failed (%s)"%(b.get("id"),f.get("tex"),err))
            continue                                     # skip this face only, do not disable globally
        _okn[0]+=1
        sel_selfcheck(mesh, sel, f)
        uv_selfcheck(mesh, f)
        if not _PERFACE_LOGGED[0]:
            _PERFACE_LOGGED[0]=True
            unreal.log("  per-face UV OK (e.g. %s sc=%d -> tile=%.0fcm)"%(f.get("tex"),sc,tile))
    if b.get("shape") not in ("box","wedge","cylinder"):     # buffer-built: report explicitly
        unreal.log("  %s id%s: %d/%d faces projected (cone %.2f deg)"
                   %(b.get("shape"), b.get("id"), _okn[0], len(faces), cone))

# ---- UV self-check ---------------------------------------------------------------------------------
# The build reports "per-face UV OK" purely because the projection CALL returned without raising. That
# says nothing about whether the resulting UVs actually vary across the face - a collapsed projection
# succeeds silently and renders as barcode stripes. Read the UVs back for the first few faces of each
# orientation and measure their spread, so a collapse is reported instead of shipped.
GUVQ,_GETUV=_find(["get_triangle_u_vs"],"triangle","u_vs")
_UVCHK={"n":0,"bad":0,"eg":[]}
def _read_tri_uv(mesh, tid):
    """(uv0,uv1,uv2) for a triangle, or None. Single signature: (target_mesh, uv_set_index, tri_id)."""
    try:
        r=getattr(GUVQ,_GETUV)(mesh,0,tid)
        pts=[x for x in (r if isinstance(r,(tuple,list)) else [r]) if hasattr(x,"x")]
        return [(p.x,p.y) for p in pts] if len(pts)>=3 else None
    except Exception:
        return None

def final_uv_check(mesh):
    """Measure the FINISHED mesh with a SHAPE-INDEPENDENT test.

    The previous metric was the per-triangle UV aspect ratio (min/max spread). That flags any sliver
    triangle, and the boolean shreds walls into slivers far more than floors - so it could not tell
    'collapsed UVs' apart from 'thin triangle'. Instead compare UV AREA to WORLD AREA: a projection
    that has collapsed onto a line has zero UV area whatever the triangle's shape, while a healthy
    projection gives uv_area/world_area ~ 1/tile^2 (in 1/cm^2). Reported as texels-per-metre so the
    numbers are readable, and as a spread so a wrong SCALE is visible too."""
    if GUVQ is None or GQ_NORM is None or GTRIPOS is None: return
    ids=tri_ids(mesh)
    if not ids: return
    import math as _m
    buckets={}
    step=max(1,len(ids)//400)
    for tid in ids[::step]:
        uv=_read_tri_uv(mesh,tid)
        if not uv: continue
        n=_tri_normal(mesh,tid)
        if n is None: continue
        try:
            r=getattr(GTRIPOS,_TRIPOS)(mesh,tid)
            pos=[x for x in (r if isinstance(r,(tuple,list)) else [r]) if hasattr(x,"x")]
        except Exception: continue
        if len(pos)<3: continue
        # world area
        e1=(pos[1].x-pos[0].x,pos[1].y-pos[0].y,pos[1].z-pos[0].z)
        e2=(pos[2].x-pos[0].x,pos[2].y-pos[0].y,pos[2].z-pos[0].z)
        cx=(e1[1]*e2[2]-e1[2]*e2[1], e1[2]*e2[0]-e1[0]*e2[2], e1[0]*e2[1]-e1[1]*e2[0])
        wa=0.5*_m.sqrt(cx[0]**2+cx[1]**2+cx[2]**2)
        if wa<1e-6: continue
        # uv area
        ua=0.5*abs((uv[1][0]-uv[0][0])*(uv[2][1]-uv[0][1])-(uv[2][0]-uv[0][0])*(uv[1][1]-uv[0][1]))
        ax="X" if abs(n.x)>0.9 else ("Y" if abs(n.y)>0.9 else ("Z" if abs(n.z)>0.9 else "tilted"))
        bkt=buckets.setdefault(ax,{"n":0,"dead":0,"rep":[]})
        bkt["n"]+=1
        # tiles per metre = sqrt(uv_area/world_area) * 100
        tpm=_m.sqrt(ua/wa)*100.0
        # A genuine collapse is EXACTLY zero UV area, not merely small. A legitimately huge tile
        # (sc=24 gives a 312m tile -> 0.003 tiles/m) was being mis-reported as collapsed by a flat
        # 0.01 cutoff. Test against zero instead; the tiles/m column already shows scale problems.
        if tpm < 1e-6: bkt["dead"]+=1
        else: bkt["rep"].append(tpm)
    unreal.log("FINAL-MESH UV check (shape-independent: UV area vs world area):")
    tot=dead=0
    for ax in ("X","Y","Z","tilted"):
        bkt=buckets.get(ax)
        if not bkt: continue
        tot+=bkt["n"]; dead+=bkt["dead"]
        rs=sorted(bkt["rep"]); med=rs[len(rs)//2] if rs else 0.0
        lo=rs[0] if rs else 0.0; hi=rs[-1] if rs else 0.0
        flag="  <-- COLLAPSED (zero UV area = barcode)" if bkt["dead"]*2>bkt["n"] else ""
        unreal.log("   normal~%-6s tris=%-5d zero-UV-area=%-5d  tiles/m median %.3f (range %.3f-%.3f)%s"
                   %(ax,bkt["n"],bkt["dead"],med,lo,hi,flag))
    if dead: unreal.log_error("   %d/%d triangles have ZERO UV area - genuinely collapsed."%(dead,tot))
    else:    unreal.log("   No collapsed UVs. All %d sampled triangles have real UV area."%tot)
    unreal.log("   (a correct face tiles at 100/tile_cm per metre: tile 122cm -> 0.82, 488cm -> 0.20)")

def uv_selfcheck(mesh, f, tid_hint=None):
    if GUVQ is None or _UVCHK["n"]>=60: return
    ids=tri_ids(mesh)
    if not ids: return
    uvs=[]
    for tid in ids[:12]:
        for call in ((mesh,0,tid),(mesh,tid,0),(mesh,0,tid,True)):
            try:
                r=getattr(GUVQ,_GETUV)(*call)
                pts=[x for x in (r if isinstance(r,(tuple,list)) else [r]) if hasattr(x,"x")]
                if pts: uvs.extend([(p.x,p.y) for p in pts]); break
            except Exception: continue
    if len(uvs)<3: return
    du=max(u for u,_ in uvs)-min(u for u,_ in uvs)
    dv=max(v for _,v in uvs)-min(v for _,v in uvs)
    _UVCHK["n"]+=1
    n=f.get("n",[0,0,0])
    ax="X" if abs(n[0])>0.9 else ("Y" if abs(n[1])>0.9 else ("Z" if abs(n[2])>0.9 else "tilted"))
    if du<1e-5 or dv<1e-5:
        _UVCHK["bad"]+=1
        if len(_UVCHK["eg"])<12:
            _UVCHK["eg"].append("normal~%s tex=%s  U spread %.6f  V spread %.6f  <-- COLLAPSED"%(ax,f.get("tex"),du,dv))
    elif len(_UVCHK["eg"])<12:
        _UVCHK["eg"].append("normal~%s tex=%s  U spread %.4f  V spread %.4f  ok"%(ax,f.get("tex"),du,dv))

def uv_selfcheck_report():
    if GUVQ is None:
        unreal.log_warning("UV self-check: no get_triangle_u_vs - skipped"); return
    unreal.log("UV self-check: sampled %d faces, %d COLLAPSED"%(_UVCHK["n"],_UVCHK["bad"]))
    for e in _UVCHK["eg"]: unreal.log("    %s"%e)
    if _UVCHK["bad"]:
        unreal.log_error("    ^ collapsed faces are the barcodes. The projection frame for those")
        unreal.log_error("      orientations is wrong (U or V has no gradient across the face).")

_UVFAIL=[0]; _UVFAIL_EG=[]
def uv_fail_report():
    if not _UVFAIL[0]:
        unreal.log("Per-face UV: no failures"); return
    unreal.log_warning("Per-face UV: %d face(s) FAILED and kept their primitive UVs "
                       "(these are the ones that look stretched / barcoded):"%_UVFAIL[0])
    for e in _UVFAIL_EG[:10]: unreal.log_warning("    %s"%e)
    if len(_UVFAIL_EG)>10: unreal.log_warning("    ... and %d more"%(len(_UVFAIL_EG)-10))

_CYLUV_LOGGED=[False]
def apply_cyl_face_uvs(mesh, b):
    """Cylinder sides only. Selecting UE facets by the GEO normal fails because UE's append_cylinder is
    phase-shifted from the geo (each geo normal sits between two UE facets -> the 15deg cone grabs a
    neighbor and its wrong tile). Instead we read the UE mesh's OWN facet normals and select each facet
    by ITS OWN normal (unique on a lone cylinder primitive -> clean isolation), then project with the
    matched geo face's tile. Uses only crash-safe calls (tri normal + select-by-normal + planar)."""
    if not _PERFACE_UV[0] or not (GSEL_N and GQ_NORM and GTRIPOS): return
    sides=[f for f in (b.get("faces") or []) if not f.get("cap") and f.get("tex")]
    if not sides: return
    ids=tri_ids(mesh)
    if not ids: return
    # group UE side triangles by rounded normal -> one bucket per facet
    buckets={}
    for tid in ids:
        nr=_tri_normal(mesh,tid)
        if nr is None or abs(nr.z)>0.9: continue                 # skip caps
        key=(round(nr.x,2),round(nr.y,2),round(nr.z,2))
        buckets.setdefault(key,[nr,0])[1]+=1
    nsides=max(len(sides), len(buckets), 3)
    ang=min(15.0, 0.4*(360.0/nsides))                            # < half a facet -> isolate one facet
    okc=0
    for key,(nr,cnt) in buckets.items():
        nvec=[nr.x,nr.y,nr.z]
        best=max(sides, key=lambda f: _dot(nvec, f["n"]))        # geo facet whose normal is closest
        if _dot(nvec,best["n"])<0.5: continue                    # no plausible match
        sel=_select_by_normal(mesh, unreal.Vector(nr.x,nr.y,nr.z), ang)
        if sel is not None and _planar_uv(mesh, sel, _face_uv_transform(best)): okc+=1
    if not _CYLUV_LOGGED[0]:
        _CYLUV_LOGGED[0]=True
        unreal.log("  cylinder per-facet UV: %d/%d facets projected by own-normal"%(okc,len(buckets)))

# ---------------------------------------------------------------- final-mesh retag (Dark override)
def _canon(n,d):
    ax=0
    if abs(n[1])>abs(n[ax]): ax=1
    if abs(n[2])>abs(n[ax]): ax=2
    if n[ax]<0: return (-n[0],-n[1],-n[2]), -d
    return (n[0],n[1],n[2]), d

def _pt_in_poly(c, poly, n, tol=1.5):
    ref=[1.0,0.0,0.0] if abs(n[0])<0.9 else [0.0,1.0,0.0]
    u=_norm(_cross(ref,n)); v=_norm(_cross(n,u))
    pts=[(_dot(p,u),_dot(p,v)) for p in poly]
    cx=_dot(c,u); cy=_dot(c,v)
    pos=neg=False; m=len(pts)
    for i in range(m):
        x1,y1=pts[i]; x2,y2=pts[(i+1)%m]
        cr=(x2-x1)*(cy-y1)-(y2-y1)*(cx-x1)
        if cr> tol: pos=True
        elif cr<-tol: neg=True
    return not (pos and neg)

# Dark's texture base axes, verbatim from csgemit.c:367 (`baseaxis[6][3]`), as {normal, U, V}.
# compute_poly_texture_info() picks the entry maximising dot(normal_i, face_normal) -- a SIGNED dot
# over six SIGNED axes, scanned in this order with a strict '>', so exact ties keep the EARLIER entry.
_DARK_BASEAXIS = [
    ((1,0,0),  (0,1,0),  (0,0,-1)),   # +X
    ((0,1,0),  (-1,0,0), (0,0,-1)),   # +Y
    ((0,0,1),  (1,0,0),  (0,-1,0)),   # +Z  (floor)
    ((-1,0,0), (0,-1,0), (0,0,-1)),   # -X
    ((0,-1,0), (1,0,0),  (0,0,-1)),   # -Y
    ((0,0,-1), (1,0,0),  (0,1,0)),    # -Z  (ceiling)
]

def _dark_axis_index(n):
    """Dark's baseaxis pick for a face whose UE-space normal is n.  The selection MUST run on the
    DARK-space normal: our world is Y-reflected, and Dark compares signed dots, so a flipped Y sign
    can change which axis wins.  (That is exactly the wedge-id5 bug: n_dark=(0,-0.707,+0.707) makes
    Dark's +Y dot NEGATIVE so +Z wins, while an abs()-based test sees |Y|==|Z| and picks Y.)"""
    nd=(n[0], -n[1], n[2])                                   # UE -> Dark (F_REFLECT is its own inverse)
    best=-1; bs=0.0
    for i,(bn,_,_) in enumerate(_DARK_BASEAXIS):
        d=bn[0]*nd[0]+bn[1]*nd[1]+bn[2]*nd[2]
        if d>bs: bs=d; best=i                                # strict '>' -> ties keep the earlier axis
    return best

def _uv_basis(n, f):
    """Return (u0, v0, projn) for the planar projection frame. Caps project straight down Z; cylinder
    sides AND wedge slants project from their DOMINANT world axis (Dark's behaviour -> tilted faces
    stretch by 1/cos(angle-to-axis)); box walls project perpendicular to the face."""
    if f.get("uaxis"):                                      # rotated brush: axes baked from the LOCAL frame.
        # The Y-reflection makes [uaxis,vaxis,n] LEFT-handed -> planar projection collapses (solid colour).
        # Negate U to get a right-handed frame; the standard su=-1 mirror restores the true U direction.
        u=f["uaxis"]; return [-u[0],-u[1],-u[2]], list(f["vaxis"]), list(n)
    if abs(n[2])>0.99:                                       # axis-aligned cap
        return [1.0,0.0,0.0], [0.0, 1.0 if n[2]>0 else -1.0, 0.0], list(n)
    if f.get("slant") or f.get("pyrside") or f.get("dodside"):
        # Tilted flat face (wedge hypotenuse / pyramid side / dodecahedron pentagon): use Dark's own
        # baseaxis entry rather than an abs()-based guess. This is Dark's "mode B" path, and the
        # non-unit U/V it produces for an off-axis plane IS the 1/cos stretch (csgemit.c:387).
        #
        # Convention: -F * [U,V]_dark, i.e. the same one the box-wall branch below uses, with the
        # leading '-' feeding the caller's fixed `rot += pi`. Uniform for every axis Dark can pick.
        #
        # An earlier version special-cased a +/-Z pick to the CAP branch's opposite convention, on the
        # reasoning that both branches must agree for a world-horizontal face. That constraint is
        # vacuous: a face with |n_z| > 0.99 is caught by the cap branch ABOVE and never reaches here.
        # This branch only ever sees TILTED faces, which may *select* the +/-Z floor/ceiling axis
        # without being horizontal - every pyramid side does exactly that (|n_z| ~ 0.83) - so the
        # special case rotated all of them by 180 deg. Confirmed correct in UE as written.
        i=_DARK_BASEAXIS[_dark_axis_index(n)]
        u0=[-i[1][0],  i[1][1], -i[1][2]]                     # -F * U_dark   (F = [1,-1,1])
        v0=[-i[2][0],  i[2][1], -i[2][2]]                     # -F * V_dark
        return u0, v0, _norm(_cross(u0,v0))                   # projn from the cross -> right-handed
    if f.get("cylside"):                                     # project from dominant world axis (stretch)
        ax=0 if (abs(n[0])>=abs(n[1]) and abs(n[0])>=abs(n[2])) else (1 if abs(n[1])>=abs(n[2]) else 2)
        s=1.0 if n[ax]>=0 else -1.0
        if   ax==0: return [0.0,s,0.0], [0.0,0.0,1.0], [s,0.0,0.0]
        elif ax==1: return [-s,0.0,0.0], [0.0,0.0,1.0], [0.0,s,0.0]
        else:       return [1.0,0.0,0.0], [0.0,s,0.0], [0.0,0.0,s]
    u0=_norm(_cross([0.0,0.0,1.0], n)); v0=_norm(_cross(n,u0))       # box wall perpendicular
    return u0, v0, list(n)


def _face_rot(f, n):
    """Final texture rotation in radians. ONE definition for every UV path.

    The record's angle is NEGATED. F_REFLECT = [1,-1,1] has determinant -1, so mirroring the level
    reverses the handedness of rotation: a +90 deg face rotation in Dark renders as -90 (270) unless
    we flip it back. Confirmed on MISS5 id98, whose faces carry raw 16384 (=90 deg) and appeared as
    270 in UE. Affects 2104 of 26000 faces (8.1%); 0 and 180 are unchanged by the flip, which is why
    the wedge/pyramid test missions (rotations of only 0 and 180) never exposed it.

    The +pi base and the cap corrections below are UE-side and were calibrated at rot=0, so they are
    independent of the sign flip and stay as they are."""
    rot=-math.radians(float(f.get("rot",0.0) or 0.0))+math.pi
    if f.get("uaxis"): rot-=math.pi/2       # baked cap frame is 90 deg off (mirror flips sense)
    if f.get("capleg"): rot+=math.pi/2      # cap from the -Y leg, not the extrude axis
    if f.get("capleg_rot"): rot-=math.pi/2  # heading-rotated leg cap (wedge id6)
    if f.get("solid") and abs(n[2])>0.99 and not f.get("uaxis"): rot+=math.pi
    return rot

DARK_TILE_EXACT = False  # True = engine's resolution-independent 2^(sc-14). False = per-texture size.
def _tile_uvoff(f):
    """(tile_u, tile_v, u offset, v offset) in cm. ONE definition, used by every UV path.

    U AND V ARE INDEPENDENT. Dark's tile is per 64 texels, so a NON-SQUARE texture spans a different
    number of tiles on each axis: a 256x512 image is 1 tile wide but 2 tiles tall. Using one number
    for both (we used the width) makes V wrong by exactly the aspect ratio - MISS5 id54's Door11 face
    (256x512) rendered with V 2x too small, while square textures on the same brush looked fine.
    91 of 333 textures are non-square; they cover 10.7% of MISS5's faces.

    scale_px is the logical WIDTH (the fam.crf original). The logical height follows the display
    aspect, which is preserved by the HD override: logical_h = scale_px * h/w."""
    sc=int(f.get("sc",16))
    if DARK_TILE_EXACT:
        tu=tv=(2.0**(sc-14))*FEET_CM; du=dv=64.0
    else:
        du,dv=_face_res_wh(f.get("tex"))
        tu=du*(2.0**(sc-20))*FEET_CM; tv=dv*(2.0**(sc-20))*FEET_CM
    # Both offsets are NEGATED: DromEd's convention is that increasing the record u/v shifts the
    # texture toward -U / -V (in DromEd, raising u slides the texture LEFT). Verified on 14.mis win323.
    return tu, tv, -float(f.get("uoff",0.0) or 0.0)/du, -float(f.get("voff",0.0) or 0.0)/dv

def _face_uv_transform(f):
    n=f["n"]; sc=int(f.get("sc",16))
    tile_u,tile_v,uoff,voff=_tile_uvoff(f); tile=tile_u
    rot=_face_rot(f,n)
    u0,v0,projn=_uv_basis(n,f)
    c=math.cos(rot); s=math.sin(rot)
    U=[c*u0[i]+s*v0[i] for i in range(3)]; V=[-s*u0[i]+c*v0[i] for i in range(3)]
    t=unreal.Transform(); t.set_editor_property("rotation", _quat_from_axes([U,V,projn]))
    su=-1.0 if MIRROR_TEX_U else 1.0                 # negative U scale mirrors the texture horizontally
    if f.get("solid"): su=-su                         # solid (union) faces face the opposite way -> flip back
    if f.get("cylside"): su=-su                        # curved side wraps the opposite way vs the caps
    uu=(uoff+TEX_SHIFT_U)*su; vv=voff+TEX_SHIFT_V     # flip the U offset phase to match the flipped axis
    off=[(uu*tile_u)*U[i]+(vv*tile_v)*V[i] for i in range(3)]
    t.set_editor_property("translation", unreal.Vector(off[0],off[1],off[2]))
    t.set_editor_property("scale3d", unreal.Vector(su*tile_u,tile_v,1.0)); return t

def _planar_uv(mesh, sel, t):
    for call in ((mesh,0,t,sel),(mesh,0,t,sel,True),(mesh,sel,0,t)):
        try: getattr(GUV_PLN,_UVPLN)(*call); return True
        except Exception: continue
    return False

def _tri_centroid(mesh,tid):
    try:
        r=getattr(GTRIPOS,_TRIPOS)(mesh,tid)
        vs=[x for x in r if isinstance(x,unreal.Vector)] if isinstance(r,(tuple,list)) else []
        if len(vs)<3: return None
        return unreal.Vector((vs[0].x+vs[1].x+vs[2].x)/3.0,(vs[0].y+vs[1].y+vs[2].y)/3.0,(vs[0].z+vs[1].z+vs[2].z)/3.0)
    except Exception: return None

_IDXTYPE_CACHE=[None]
def _tri_index_type():
    """EGeometryScriptIndexType.Triangle - the enum convert_index_*_to_mesh_selection wants (NOT
    MeshSelectionType). Names vary across UE builds, so probe candidates once and cache."""
    if _IDXTYPE_CACHE[0] is not None: return _IDXTYPE_CACHE[0]
    for en in ("EGeometryScriptIndexType","GeometryScriptIndexType"):
        e=getattr(unreal,en,None)
        if e is None: continue
        for mem in ("TRIANGLE","TRIANGLE_ID","TRIANGLES","TRIANGLE_INDEX","POLYGON"):
            v=getattr(e,mem,None)
            if v is not None: _IDXTYPE_CACHE[0]=v; return v
    _IDXTYPE_CACHE[0]=False; return False

def _int_array(tids):
    try:
        a=unreal.Array(int)
        for t in tids: a.append(int(t))
        return a
    except Exception:
        return [int(t) for t in tids]

USE_INDEX_SELECTION = False    # convert_index_*_to_mesh_selection ACCESS-VIOLATES (crashes the editor)
                               # in this UE build; keep it off. Cylinder UVs handled another way below.
_SELDBG=[0]
def _selection_from_ids(mesh, tids):
    if not GSEL_IDX or not USE_INDEX_SELECTION: return None
    it=_tri_index_type(); arr=_int_array(tids); fn=getattr(GSEL_IDX,_SELIDX)
    # UE Python requires selection_type as a KEYWORD arg (positional pos-3 is rejected).
    attempts=[]
    if it is not False:
        attempts += [
            (lambda: fn(mesh, arr, selection_type=it)),
            (lambda: fn(mesh, index_list=arr, selection_type=it)),
            (lambda: fn(mesh, arr, index_type=it)),
        ]
    attempts += [ (lambda: fn(mesh, arr)) ]
    last=None
    for a in attempts:
        try:
            r=a(); sel=r[-1] if isinstance(r,(tuple,list)) else r
            if sel is not None: return sel
        except Exception as e:
            last=e; continue
    if _SELDBG[0]<1 and last is not None:
        _SELDBG[0]=1
        unreal.log_warning("  _selection_from_ids failed: fn=%s idxtype=%s arrtype=%s err=%s"
                           %(_SELIDX, it, type(arr).__name__, last))
    return None

# --- geometric per-face selection (Option A): a thin oriented box around the face polygon ----------
_MSELTYPE_CACHE=[None]
def _mesh_sel_type():
    if _MSELTYPE_CACHE[0] is not None: return _MSELTYPE_CACHE[0]
    for en in ("EGeometryScriptMeshSelectionType","GeometryScriptMeshSelectionType"):
        e=getattr(unreal,en,None)
        if e is None: continue
        for mem in ("TRIANGLES","TRIANGLE","POLYGONS","FACES"):
            v=getattr(e,mem,None)
            if v is not None: _MSELTYPE_CACHE[0]=v; return v
    _MSELTYPE_CACHE[0]=False; return False

USE_BOX_SELECTION = True       # RETRY: the two crashers (convert_index, select_in_box) both took the
                               # selection_type ENUM; select_by_normal (no enum) never crashed. So we
                               # retry box selection with NO enum + a VALID unreal.Box. If it still
                               # crashes, set this False to return to the stable (cylinder-imperfect) build.
def _make_box(bmin,bmax):
    """A VALID unreal.Box - an invalid one makes select_mesh_elements_in_box write out of bounds (crash)."""
    for mk in (lambda: unreal.Box(min=bmin,max=bmax,is_valid=True),
               lambda: unreal.Box(min=bmin,max=bmax)):
        try:
            b=mk()
            try: b.set_editor_property("is_valid", True)
            except Exception: pass
            return b
        except Exception: continue
    try:
        b=unreal.Box(); b.set_editor_property("min",bmin); b.set_editor_property("max",bmax)
        b.set_editor_property("is_valid",True); return b
    except Exception: return None

_BOXDBG=[0]
def _selection_from_box(mesh, f):
    """Select ONE face's triangles by its WORLD-SPACE axis-aligned bounding box (select_mesh_elements_in_box
    has NO transform param - the box is a world FBox). A tight AABB + all-3-points-in-box isolates the
    face by POSITION (handles coincident faces, cylinder facets, and multiple cylinders)."""
    if not GSEL_BOX or not USE_BOX_SELECTION: return None
    poly=f.get("poly") or []
    if len(poly)<3: return None
    try:
        xs=[p[0] for p in poly]; ys=[p[1] for p in poly]; zs=[p[2] for p in poly]
        M=1.5                                           # tight margin (cm); keep neighbors out
        box=_make_box(unreal.Vector(min(xs)-M,min(ys)-M,min(zs)-M),
                      unreal.Vector(max(xs)+M,max(ys)+M,max(zs)+M))
        if box is None: return None
        fn=getattr(GSEL_BOX,_SELBOX); st=_mesh_sel_type()
        r=fn(mesh, box, selection_type=st) if st is not False else fn(mesh, box)   # all 3 tri points in box
        sel=r[-1] if isinstance(r,(tuple,list)) else r
        return sel
    except Exception as e:
        if _BOXDBG[0]<1: _BOXDBG[0]=1; unreal.log_warning("  _selection_from_box error: %s"%e)
    return None


def _face_uv_at(f, p):
    """UV for world point p under face f - the analytic form of the planar projection we were asking
    GeometryScript to do. Derivation: the transform is rotation R=[U,V,projn], scale S=(su*tile,tile,1),
    translation T=(uu*tile)U+(vv*tile)V, and a planar projection maps p -> S^-1 R^-1 (p-T), so
        u = U.p/(su*tile) - uu/su      v = V.p/tile - vv
    Computing it here means every vertex gets its own UV: nothing is shared, nothing can be clobbered."""
    n=f["n"]; sc=int(f.get("sc",16))
    tile_u,tile_v,uoff,voff=_tile_uvoff(f); tile=tile_u
    rot=_face_rot(f,n)
    u0,v0,projn=_uv_basis(n,f)
    c=math.cos(rot); sn=math.sin(rot)
    U=[c*u0[i]+sn*v0[i] for i in range(3)]; V=[-sn*u0[i]+c*v0[i] for i in range(3)]
    su=_su_for(f) if "_su_for" in globals() else (-1.0 if MIRROR_TEX_U else 1.0)
    if "_su_for" not in globals():
        if f.get("solid"): su=-su
        if f.get("cylside"): su=-su
    uu=(uoff+TEX_SHIFT_U)*su; vv=voff+TEX_SHIFT_V
    return ((U[0]*p[0]+U[1]*p[1]+U[2]*p[2])/(su*tile_u) - uu/su,
            (V[0]*p[0]+V[1]*p[1]+V[2]*p[2])/tile_v      - vv)

def rebuild_unwelded(mesh, tri2face):
    """Rebuild the finished mesh with every triangle owning its own 3 vertices, UVs computed here.

    WHY: the boolean returns a WELDED UV overlay - faces sharing a vertex share its UV element. Each
    per-face projection therefore overwrites its neighbours and the last write wins. Horizontal faces
    survive (their UVs vary with x/y regardless); vertical faces do not, because x/y are constant up a
    wall, so their UVs stop varying -> zero UV area -> barcode. Proven by RETAG_UV_ONLY='vertical',
    which inverted the symptom exactly. Unwelding removes the sharing, so the problem cannot occur."""
    if GEDIT is None: return None
    ids=tri_ids(mesh)                  # NOT range(tri_count): unvisited ids were DROPPED from the
    if not ids: return None            # rebuilt mesh, which is exactly how faces became see-through
    verts=[]; tris=[]; uvs=[]; mats=[]; miss=0; culled=0; degen=0
    for tid in ids:
        f=tri2face.get(tid)
        try:
            r=getattr(GTRIPOS,_TRIPOS)(mesh,tid)
            ps=[x for x in (r if isinstance(r,(tuple,list)) else [r]) if hasattr(x,"x")]
        except Exception: continue
        if len(ps)<3: continue
        # Drop DEGENERATE (near-zero-area) triangles. The boolean occasionally emits collapsed slivers
        # (they showed up as bare tris at the origin with a zero normal). They render nothing but bloat
        # the mesh and pollute the diagnostics.
        e1=(ps[1].x-ps[0].x,ps[1].y-ps[0].y,ps[1].z-ps[0].z)
        e2=(ps[2].x-ps[0].x,ps[2].y-ps[0].y,ps[2].z-ps[0].z)
        cxd=_cross(e1,e2)
        if (cxd[0]*cxd[0]+cxd[1]*cxd[1]+cxd[2]*cxd[2])<1e-6: degen+=1; continue
        # Outward skin of the synthetic world block - a surface DromEd never draws. Rebuilding the
        # mesh triangle by triangle is the cheapest place to remove it: just don't emit it.
        if f is None and _SHELL[0] is not None:
            p3=[(q.x,q.y,q.z) for q in ps[:3]]
            if _is_shell_tri(p3,_tri_normal_from(p3)): culled+=1; continue
        base=len(verts)
        if f is None:
            # NO face matched. The old placeholder was (x/100, y/100) - a fixed projection down world
            # Z - which collapses on any vertical surface and rendered as barcode stripes. It showed
            # up as one triangle of a quad striped while its partner (which did match) was correct.
            # Project along the triangle's OWN dominant axis instead, so no orientation can collapse.
            e1=(ps[1].x-ps[0].x,ps[1].y-ps[0].y,ps[1].z-ps[0].z)
            e2=(ps[2].x-ps[0].x,ps[2].y-ps[0].y,ps[2].z-ps[0].z)
            cx=_cross(e1,e2); a=[abs(cx[0]),abs(cx[1]),abs(cx[2])]
            ax=a.index(max(a))                       # dominant axis of the triangle normal
            ij=(1,2) if ax==0 else ((0,2) if ax==1 else (0,1))
            T_DEF=121.92                             # 4ft, Dark's default tile
        for q in ps[:3]:
            verts.append((q.x,q.y,q.z))
            if f:
                uvs.append(_face_uv_at(f,(q.x,q.y,q.z)))
            else:
                c3=(q.x,q.y,q.z)
                uvs.append((c3[ij[0]]/T_DEF, c3[ij[1]]/T_DEF))
        tris.append((base,base+1,base+2))
        if f and f.get("tex"):
            mats.append(material_id(f["tex"]))
        else:
            # Never emit material 0 here - that is the DEFAULT GREY material and renders as an
            # untextured patch. Fall back to the mesh's most common texture instead of nothing.
            mats.append(_fallback_matid()); miss+=1
    if not tris: return None
    m=new_mesh(); buf=MeshBuffers()
    buf.set_editor_property("vertices",  [unreal.Vector(*v) for v in verts])
    buf.set_editor_property("triangles", [unreal.IntVector(*t) for t in tris])
    # Face normals, one per vertex. Without these UE logs "did not have normals to recompute" and
    # falls back to per-vertex averaging, which on an unwelded mesh yields degenerate tangent bases.
    nrm=[]
    for (i0,i1,i2) in tris:
        p0,p1,p2=verts[i0],verts[i1],verts[i2]
        e1=[p1[k]-p0[k] for k in range(3)]; e2=[p2[k]-p0[k] for k in range(3)]
        cx=_cross(e1,e2); L=math.sqrt(cx[0]**2+cx[1]**2+cx[2]**2)
        v=[cx[0]/L,cx[1]/L,cx[2]/L] if L>1e-12 else [0.0,0.0,1.0]
        nrm.extend([v,v,v])
    ok=[]
    for prop,val in (("uv0",[unreal.Vector2D(u,v) for (u,v) in uvs]),
                     ("normals",[unreal.Vector(*v) for v in nrm]),):
        try: buf.set_editor_property(prop,val); ok.append(prop)
        except Exception as e: unreal.log_warning("  rebuild_unwelded: could not set %s (%s)"%(prop,e))
    GEDIT.append_buffers_to_mesh(m, buf)
    _enable_matids(m)
    # Every triangle of a freshly-appended buffer starts at material 0, and slot 0 is the blank
    # default material - i.e. FLAT GREY. _set_matid_tri swallows exceptions, so a failed assignment
    # leaves that triangle grey with nothing in the log. Count them.
    matfail=0
    for i,mid in enumerate(mats):
        if not _set_matid_tri(m,i,mid): matfail+=1
    if matfail:
        unreal.log_error("  rebuild_unwelded: %d/%d material assignments FAILED -> those triangles "
                         "render as flat grey (material 0)"%(matfail,len(mats)))
    ensure_uv_normals(m, uvs=False)
    unreal.log("  rebuild_unwelded: %d tris -> %d verts (unwelded), uv set: %s, unmatched tris=%d%s%s"
               %(len(tris),len(verts),",".join(ok) or "NONE",miss,
                 ("  world-shell culled=%d"%culled) if culled else "",
                 ("  degenerate culled=%d"%degen) if degen else ""))
    return m

# ---------------------------------------------------------------- world-shell culling
# mis_to_geo SYNTHESISES brush 0 (mis_to_geo.py ~L435): a solid cuboid 8ft larger than the mission
# bounds, present only so the air brushes have something to carve into. It is not in the .MIS - note
# that extract() returns nothing at time 0 - and it is written with faces=[]. Its OUTWARD skin
# therefore has no texture record: retag leaves it bare and rebuild_unwelded paints it with
# _fallback_matid(), i.e. an invented texture on a surface DromEd never draws. Dark only ever emits
# surfaces that bound an open cell, so nothing outside the world exists at all.
#
# Culling it is safe: the level's own inner surfaces still fully enclose the playable space, so
# collision and the view from inside are unchanged - the only difference is that the level stops
# being wrapped in an opaque block when you look at it from outside.
_SHELL   = [None]         # [(axis, plane_coord, outward_sign), ...] while culling is armed
SHELL_EPS = 1.0           # cm; the shell planes are exact, this only absorbs boolean round-off

def _shell_planes(world):
    V=world.get("verts") or []
    if len(V)<8: return None
    P=[]
    for ax in range(3):
        vals=[v[ax] for v in V]
        P.append((ax,min(vals),-1)); P.append((ax,max(vals),+1))
    return P

def _is_shell_tri(pts,nrl):
    """True if this triangle lies ON an outer plane of the world block AND faces out of the world."""
    S=_SHELL[0]
    if not S or not pts: return False
    for ax,plane,sgn in S:
        if abs(nrl[ax])<0.9:   continue     # not perpendicular to that plane
        if nrl[ax]*sgn<=0:     continue     # faces INWARD -> a real surface of the level, keep it
        if all(abs(p[ax]-plane)<=SHELL_EPS for p in pts): return True
    return False

def _tri_normal_from(p3):
    e1=[p3[1][k]-p3[0][k] for k in range(3)]; e2=[p3[2][k]-p3[0][k] for k in range(3)]
    cx=_cross(e1,e2); L=math.sqrt(cx[0]**2+cx[1]**2+cx[2]**2)
    return [cx[0]/L,cx[1]/L,cx[2]/L] if L>1e-12 else [0.0,0.0,1.0]

RETAG_RESCUE_CM  = 60.0   # plane tolerance for the relaxed second pass (strict pass uses 15)
RETAG_NORMAL_DOT = 0.99   # min |cos| between a result triangle's normal and a face's normal
def retag_final(mesh, body):
    """Assign each result triangle the LATEST brush face that lies on its plane and covers it
       (Dark's 'later brush wins' override; also makes texturing robust to the boolean).

       Matching is POSITION-based: geo faces are bucketed spatially by the cells their polygon
       overlaps, and each result triangle is resolved by its world-space centroid falling inside
       a coplanar face polygon. This is robust to the UE-built cylinder having a different vertex
       phase than the geo cylinder (the old normal-bucketed match failed there, shifting the
       cylinder side textures by +6 facets)."""
    if not (GMAT_TRI and GQ_NORM and GTRIPOS): unreal.log_warning("  retag: missing tri funcs; skipped"); return
    CELL=128.0; CELL_EPS=2.0   # cm of padding so boundary-hugging triangles find their face
    def _cell(p): return (int(math.floor(p[0]/CELL)),int(math.floor(p[1]/CELL)),int(math.floor(p[2]/CELL)))
    cells={}; big=[]
    for b in body:
        t=b.get("time",0)
        for f in b.get("faces",[]):
            if not f.get("tex") or not f.get("poly"): continue
            poly=f["poly"]
            xs=[p[0] for p in poly]; ys=[p[1] for p in poly]; zs=[p[2] for p in poly]
            # Pad the face's bbox by CELL_EPS before bucketing. Boolean output lands on face planes
            # to within floating point, so a triangle centroid on a CELL BOUNDARY floors into the
            # neighbouring cell and never sees the face: MISS5 id4's x=0 face is bucketed at
            # floor(0/128)=0, while a triangle at x=-1e-9 looks up floor(-1e-9/128)=-1. It reported
            # bestcos=1.000 (perfect orientation) with bestplane=6949cm, because the only candidates
            # left were the far-away `big` faces. No tolerance would ever have fixed that.
            lo=_cell([min(xs)-CELL_EPS,min(ys)-CELL_EPS,min(zs)-CELL_EPS])
            hi=_cell([max(xs)+CELL_EPS,max(ys)+CELL_EPS,max(zs)+CELL_EPS])
            span=(hi[0]-lo[0]+1)*(hi[1]-lo[1]+1)*(hi[2]-lo[2]+1)
            entry=(t,f)
            if span>512:
                big.append(entry)
            else:
                for cx in range(lo[0],hi[0]+1):
                    for cy in range(lo[1],hi[1]+1):
                        for cz in range(lo[2],hi[2]+1):
                            cells.setdefault((cx,cy,cz),[]).append(entry)
    ids=tri_ids(mesh)                  # NOT range(tri_count): boolean output has triangle id GAPS
    if not ids: unreal.log_warning("  retag: no triangles"); return
    tc=len(ids)
    _enable_matids(mesh)
    groups={}; matched=0; unmatched=[]; shell=0
    # --- id9 pit probe: report which of the open-pit surfaces the boolean actually produced.
    # Open pit interior (cm): x[-1585.8,-1097.3] y[-609.6,609.6] z[-487.7,-243.8]. Bucket every final
    # triangle whose centroid sits on one of the six bounding planes, by (plane, normal sign).
    _PROBE=[]
    for tid in ids:
        nr=_tri_normal(mesh,tid); c=_tri_centroid(mesh,tid)
        if nr is None or c is None: continue
        nrl=[nr.x,nr.y,nr.z]; cl=[c.x,c.y,c.z]
        # The result triangle's own 3 verts, for the full-coverage test below.
        tpts=None
        try:
            r=getattr(GTRIPOS,_TRIPOS)(mesh,tid)
            vs=[v for v in (r if isinstance(r,(tuple,list)) else [r]) if hasattr(v,"x")]
            if len(vs)>=3: tpts=[[v.x,v.y,v.z] for v in vs[:3]]
        except Exception: pass
        # id9 pit probe (diagnostic; harmless): does a triangle exist on each pit surface?
        if PROBE_ID9 and (-1650<cl[0]<-1030 and -680<cl[1]<680 and -540<cl[2]<-190):
            _PROBE.append((round(cl[0]),round(cl[1]),round(cl[2]),
                           round(nrl[0],2),round(nrl[1],2),round(nrl[2],2)))
        # Outward skin of the synthetic world block: no face record can ever match it, so it would
        # land in `unmatched` and be reported as bare. rebuild_unwelded drops it entirely.
        if _is_shell_tri(tpts or [cl], nrl): shell+=1; continue
        best=None; bestt=-1; bestpd=1e9
        for (t,f) in cells.get(_cell(cl),[])+big:
            fn=f["n"]
            # Orientation must be NEARLY PARALLEL, not merely "roughly coplanar". At the old 0.5
            # (60 deg!) a wedge's hypotenuse matched its own bottom face: MISS5 id33's slant normal
            # [-0.45,0,0.89] has |dot| 0.894 with its [0,0,-1] bottom, and the brush is only 61cm
            # tall, so slant triangles near the lower edge also fell inside the 15cm plane tolerance
            # and took the bottom face's texture - a triangle of the wrong texture across the lower
            # half of the slant. abs() is kept: an AIR carve's visible surface normal is the negation
            # of the stored brush-outward normal, so both signs are legitimate.
            if abs(_dot(nrl,fn))<RETAG_NORMAL_DOT: continue
            pd=abs(_dot(cl,fn)-f["d"])
            if pd>15.0: continue                               # not on the face's plane
            # FULL COVERAGE: the face polygon must contain the WHOLE triangle, not just its centroid.
            # Where a later brush only PARTIALLY overlaps a face, the centroid test hands one triangle
            # of a quad to the rival and the other to the original, splitting the surface along the
            # triangle DIAGONAL (the "big triangle" of wrong texture on id34/id406). Dark splits along
            # the brush boundary (a straight edge); we can't without subdividing, so a partial overlap
            # now leaves the original texture intact. The face's OWN triangles still match - they fill
            # its polygon, and _pt_in_poly's 1.5cm tolerance admits verts on the boundary.
            if not all(_pt_in_poly(q,f["poly"],fn) for q in (tpts or [cl])): continue
            if t>bestt or (t==bestt and pd<bestpd): bestt=t; bestpd=pd; best=f
        if best is not None:
            _set_matid_tri(mesh,tid,material_id(best["tex"]))
            groups.setdefault(id(best),(best,[]))[1].append(tid); matched+=1
        else:
            unmatched.append((tid,nrl,cl))
    unreal.log("  retag: %d/%d tris matched to %d faces%s"%(matched,tc,len(groups),
               ("  (+%d world-shell tris culled)"%shell) if shell else ""))
    if PROBE_ID9:
        unreal.log("  --- id9 PIT PROBE: %d final tris in the pit region ---"%len(_PROBE))
        want={"floor z=-488 (n +Z)":  lambda p: abs(p[2]+488)<8  and p[5]>0.9,
              "wall x=-1586 (n +X)":  lambda p: abs(p[0]+1586)<8 and p[3]>0.9,
              "wall x=-1097 (n -X)":  lambda p: abs(p[0]+1097)<8 and p[3]<-0.9,
              "wall y=-610  (n +Y)":  lambda p: abs(p[1]+610)<8  and p[4]>0.9,
              "wall y=+610  (n -Y)":  lambda p: abs(p[1]-610)<8  and p[4]<-0.9}
        for label,pred in want.items():
            hits=[p for p in _PROBE if pred(p)]
            unreal.log("     %-22s %s (%d tris)"%(label,"PRESENT" if hits else "*** MISSING ***",len(hits)))
        # anything in the region that didn't fall into a named bucket (unexpected orientation)
        named=[p for p in _PROBE if any(pred(p) for pred in want.values())]
        for p in _PROBE:
            if p not in named:
                unreal.log("     other tri at (%d,%d,%d) n(%.2f,%.2f,%.2f)"%p)

    # RELAXED SECOND PASS for triangles the strict test missed.
    #
    # The boolean creates new triangles along every intersection, and a fair number have centroids
    # that fall outside EVERY face polygon (1175 of 72004 on MISS5). Those used to keep material id 0,
    # which is the DEFAULT GREY material - the untextured patches on id4 and id49. They also got
    # placeholder UVs in rebuild_unwelded.
    #
    # So: drop the polygon-coverage requirement, keep the orientation requirement, and take the
    # nearest coplanar face by plane distance (latest brush wins ties, as before). A triangle that
    # lies ON a face's plane and faces the same way belongs to that surface even if its centroid
    # drifts past the polygon edge.
    rescued=0; diag=[]; diagpos=[]
    if unmatched:
        for (tid,nrl,cl) in unmatched:
            best=None; bestt=-1; bestpd=1e9
            for (t,f) in cells.get(_cell(cl),[])+big:
                fn=f["n"]
                if abs(_dot(nrl,fn))<RETAG_NORMAL_DOT: continue
                pd=abs(_dot(cl,fn)-f["d"])
                if pd>RETAG_RESCUE_CM: continue
                if pd<bestpd-1e-6 or (abs(pd-bestpd)<=1e-6 and t>bestt):
                    bestt=t; bestpd=pd; best=f
            if best is not None:
                _set_matid_tri(mesh,tid,material_id(best["tex"]))
                groups.setdefault(id(best),(best,[]))[1].append(tid); rescued+=1
            else:
                # WHY did it fail? Record the closest candidate on each axis so the blocker is
                # visible instead of guessed at. bd = best |cos| seen, bpd = best plane distance
                # among candidates that passed the orientation test.
                bd=0.0; bpd=1e9; ncand=0
                for (t,f) in cells.get(_cell(cl),[])+big:
                    ncand+=1
                    d=abs(_dot(nrl,f["n"]))
                    if d>bd: bd=d
                    if d>=RETAG_NORMAL_DOT:
                        pd=abs(_dot(cl,f["n"])-f["d"])
                        if pd<bpd: bpd=pd
                diag.append((ncand,bd,bpd))
                if len(diagpos)<8: diagpos.append((cl,nrl,ncand,bd,bpd))
        unreal.log("  retag rescue: %d/%d unmatched tris resolved by nearest coplanar face (%d still bare)"
                   %(rescued,len(unmatched),len(unmatched)-rescued))
        if diag:
            nocand=sum(1 for c,_,_ in diag if c==0)
            orient=sum(1 for c,d,_ in diag if c>0 and d<RETAG_NORMAL_DOT)
            plane =sum(1 for c,d,pd in diag if c>0 and d>=RETAG_NORMAL_DOT and pd>RETAG_RESCUE_CM)
            unreal.log("  rescue blockers: no candidate in cell=%d, orientation<%.2f=%d, plane>%.0fcm=%d"
                       %(nocand,RETAG_NORMAL_DOT,orient,RETAG_RESCUE_CM,plane))
            bds=sorted(d for c,d,_ in diag if c>0)
            pds=sorted(pd for c,d,pd in diag if pd<1e8)
            if bds: unreal.log("     best |cos| seen: min %.3f  median %.3f  max %.3f"%(bds[0],bds[len(bds)//2],bds[-1]))
            if pds: unreal.log("     best plane dist: min %.1f  median %.1f  max %.1fcm"%(pds[0],pds[len(pds)//2],pds[-1]))
            for (cl,nrl,ncand,bd,bpd) in diagpos:
                unreal.log("     BARE tri at (%.0f, %.0f, %.0f) normal (%.2f, %.2f, %.2f)  "
                           "candidates=%d bestcos=%.3f bestplane=%.1fcm"
                           %(cl[0],cl[1],cl[2],nrl[0],nrl[1],nrl[2],ncand,bd,bpd))
    tri2face={}
    for fid,(f,tids) in groups.items():
        for t in tids: tri2face[t]=f
    retag_final.tri2face=tri2face
    if GUV_PLN and (GSEL_IDX or GSEL_BOX):
        okc=0; nosel=0; noproj=0; skipflat=0; skiptilt=0
        for fid,(f,tids) in groups.items():
            # RE-PROJECT EVERY FACE, not just cylinder sides.
            #
            # This used to skip flat faces on the assumption that their per-primitive planar UVs survive
            # the boolean. Measured on the finished MISS5 mesh, they do not: of the sampled triangles,
            # 127/151 with normal~X and 148/170 with normal~Y had a COLLAPSED UV axis, while normal~Z
            # was healthy (1/167). Vertical faces lose their UVs in the boolean - which is why the top
            # face looked right and its texture appeared to run down the sides as stripes.
            #
            # The old worry about over-grabbing was about the BOX (AABB) fallback. The exact
            # triangle-ID selection built from the retag match is tried first and is not affected; only
            # fall back to the AABB for a face whose ids we could not resolve.
            # _selection_from_ids is a no-op project-wide (USE_INDEX_SELECTION=False - the convert_index_*
            # functions access-violate), so the AABB path does all the real work. It is safe for an
            # AXIS-ALIGNED face: the polygon's world AABB is a ~3cm slab containing exactly that face.
            # The over-grab hazard in the original comment applies to TILTED / triangular faces, whose
            # AABB balloons to enclose unrelated geometry - those keep their pre-boolean UVs, which the
            # final-mesh measurement shows are mostly intact (3/21 flat) unlike the walls (275/321).
            fn=f["n"]
            if RETAG_UV_ONLY=="vertical"   and abs(fn[2])>0.5: skiptilt+=1; nosel+=1; continue
            if RETAG_UV_ONLY=="horizontal" and abs(fn[2])<=0.5: skiptilt+=1; nosel+=1; continue
            sel=_selection_from_ids(mesh,tids)
            if sel is None:
                axis_aligned = max(abs(fn[0]),abs(fn[1]),abs(fn[2])) > 0.99
                if f.get("cylside") or axis_aligned: sel=_selection_from_box(mesh,f)
            if sel is None: nosel+=1; skiptilt+=1; continue
            if _planar_uv(mesh,sel,_face_uv_transform(f)): okc+=1
            else: noproj+=1
        unreal.log("  retag UV: %d/%d faces RE-projected post-boolean (selection-failed=%d, projection-failed=%d)"
                   %(okc,len(groups),nosel,noproj))
        if nosel: unreal.log("           (%d of those are tilted faces deliberately left on their "
                             "pre-boolean UVs - the AABB would over-grab there)"%skiptilt)

def assign_materials(sm):
    if not _TEX_OK[0]: return
    try: sm.set_material(0, _default_material())
    except Exception: pass
    for name,(mid,mat) in _MATS.items():
        try: sm.set_material(mid, mat)
        except Exception as e: unreal.log_warning("  slot %d (%s) assign failed: %s"%(mid,name,e))
    unreal.log("Assigned %d materials (+default)"%len(_MATS))

def mk(b,grow_xy=0.0):
    # grow_xy>0 inflates the two non-vertical walls of a BOX tool (fill-solid union only; see mk_box).
    # A shallow copy carries the flag so the shared brush dict and its texture data are untouched.
    if grow_xy and b.get("shape")=="box":
        b=dict(b); b["_grow_xy"]=grow_xy
    if b.get("shape")=="box":      m=mk_box(b)
    elif b.get("shape")=="wedge":  m=mk_wedge(b)
    elif b.get("shape")=="cylinder": m=mk_cylinder(b)
    else:                          m=mk_buffer(b)
    if _TEX_OK[0]:
        tag_materials(m,b)
        apply_face_uvs(m,b)          # per-face UVs at Dark scale (before boolean, so they carry through)
        if b.get("shape")=="cylinder":
            apply_cyl_face_uvs(m,b)  # overwrite side UVs by each UE facet's OWN normal (phase-robust)
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

# ---------------------------------------------------------------- exact medium lookup (Dark's rule)
# Dark renders a water surface ONLY where water meets AIR. get_texture_for_medium_transition
# (editor/cvtbrush.c:169) hands back WATERIN/WATEROUT for the air<->water crossings, and
# ConvertOnePortal drops the boundary entirely when both sides end up the same medium:
#     if (dest_final_medium == final_medium) { render_info->texture_id = 0; }
# Where water meets SOLID the visible surface belongs to the solid brush and wears its own texture,
# so the water volume's face there must not be drawn at all - it would z-fight the wall.
#
# We can answer "what medium is at point P" exactly, the same way DromEd does: replay the media ops
# in brush order. Every Dark primitive is CONVEX, so "P is inside brush b" is just a plane test.
SOLID,AIR,WATER_M = 0,1,2
MEDIA_T = {0:{SOLID:SOLID, AIR:SOLID, WATER_M:SOLID},   # 0 fill solid
           1:{SOLID:AIR,   AIR:AIR,   WATER_M:AIR},     # 1 fill air
           2:{SOLID:WATER_M,AIR:WATER_M,WATER_M:WATER_M},# 2 fill water
           3:{SOLID:SOLID, AIR:WATER_M,WATER_M:WATER_M},# 3 flood  (only fills existing air)
           4:{SOLID:SOLID, AIR:AIR,   WATER_M:AIR}}     # 4 evaporate

def _brush_solid(b):
    """(planes, bbox) for a convex brush. Planes are outward-facing: dot(n,P) <= d means inside."""
    V=b.get("verts") or []; T=b.get("tris") or []
    if len(V)<4 or not T: return None
    n=float(len(V)); C=[sum(v[i] for v in V)/n for i in range(3)]
    planes=[]; seen=set()
    for t in T:
        p0,p1,p2=V[t[0]],V[t[1]],V[t[2]]
        e1=[p1[k]-p0[k] for k in range(3)]; e2=[p2[k]-p0[k] for k in range(3)]
        cx=_cross(e1,e2); L=math.sqrt(cx[0]**2+cx[1]**2+cx[2]**2)
        if L<1e-9: continue
        nr=[cx[k]/L for k in range(3)]; d=_dot(nr,p0)
        if _dot(nr,C)-d>0: nr=[-x for x in nr]; d=-d      # orient outward (centroid must be inside)
        key=(round(nr[0],4),round(nr[1],4),round(nr[2],4),round(d,2))
        if key in seen: continue
        seen.add(key); planes.append((nr,d))
    bb=[[min(v[i] for v in V) for i in range(3)],[max(v[i] for v in V) for i in range(3)]]
    return (planes,bb)

def _inside_solid(P, sol, eps=0.05):
    planes,bb=sol
    for i in range(3):
        if P[i]<bb[0][i]-eps or P[i]>bb[1][i]+eps: return False   # cheap bbox reject
    for nr,d in planes:
        if _dot(nr,P)>d+eps: return False
    return True

def build_media_model(world, body):
    """Precompute convex descriptions once; returns (world_solid, [(op, solid), ...]) in time order."""
    ws=_brush_solid(world)
    seq=[]
    for b in sorted(body, key=lambda x:x.get("time",0)):
        if b.get("op") not in MEDIA_T: continue
        s=_brush_solid(b)
        if s: seq.append((b["op"], s))
    return ws, seq

def medium_at(P, model):
    """Final medium at P: SOLID / AIR / WATER_M. Outside the world block everything is AIR."""
    ws,seq=model
    m = SOLID if (ws and _inside_solid(P,ws)) else AIR
    for op,sol in seq:
        if _inside_solid(P,sol): m=MEDIA_T[op][m]
    return m

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
    if BUILD_WORLD_BOX:
        result=mk(world)      # start fully solid (the enclosing world box), then carve into it
        _SHELL[0]=_shell_planes(world) if STRIP_WORLD_SHELL else None
        unreal.log("  world solid tris=%s%s"%(tri_count(result),
                   "  (outward skin will be culled)" if _SHELL[0] else ""))
    else:
        result=new_mesh()     # start EMPTY: no enclosing cuboid; only additive brushes contribute
        unreal.log("  world box SKIPPED (BUILD_WORLD_BOX=False) - starting from empty mesh")
    WATER=new_mesh()
    fails=[0]; first_err=[None]
    def BOP(tgt,tool,op): GB.apply_mesh_boolean(tgt,I,tool,I,op,O)
    def WOP(tool,op):
        # No-op while WATER is still empty: subtracting from / unioning into an empty mesh makes the
        # engine log "Boolean operation failed due to an empty result" for every single brush.
        if tri_count(WATER)>0 or op==UNION: BOP(WATER,tool,op)
    n=len(body)
    for i,b in enumerate(body,1):
        op=b["op"]
        try:
            if   op==0:  BOP(result,mk(b,grow_xy=FILL_UNION_GROW_CM),UNION);    WOP(mk(b),SUBTRACT)
            elif op==1:  BOP(result,mk(b),SUBTRACT); WOP(mk(b),SUBTRACT)
            elif op==2:  BOP(result,mk(b),SUBTRACT); WOP(mk(b),UNION)
            elif op==3:  t=mk(b); BOP(t,result,SUBTRACT); BOP(WATER,t,UNION)
            elif op==4:  WOP(mk(b),SUBTRACT)
            elif op==5:  t=mk(b); BOP(t,result,INTERSECT); BOP(WATER,t,UNION); BOP(result,mk(b),SUBTRACT)
            elif op==8:  t=mk(b,grow_xy=FILL_UNION_GROW_CM); BOP(t,WATER,INTERSECT); BOP(result,t,UNION); WOP(mk(b),SUBTRACT)
        except Exception as e:
            fails[0]+=1
            if first_err[0] is None: first_err[0]=str(e)
        if i%250==0: unreal.log("  ...%d/%d  result tris=%s"%(i,n,tri_count(result)))
    unreal.log("Done carving: result tris=%s  WATER tris=%s  boolean failures=%d"%(
        tri_count(result),tri_count(WATER),fails[0]))
    if first_err[0]: unreal.log_error("First boolean error: %s"%first_err[0])

    # normals only: a whole-mesh UV projection here would overwrite every per-face projection we just
    # did, collapsing vertical faces to barcode stripes (see ensure_uv_normals docstring).
    # NORMALS ONLY. _UVFN is resolved by NAME so it can be a PLANAR projection; run over the whole
    # level it projects every face down one axis, so only faces perpendicular to that axis survive and
    # everything containing it collapses into stripes. That is precisely the reported MISS5 symptom:
    # XZ faces (normal +/-Y) correct, YZ and XY faces barcoded along Y -> a whole-mesh planar
    # projection along world Y. Never let a name-guessed projection touch the finished mesh.
    ensure_uv_normals(result, uvs=False)
    if _TEX_OK[0]:
        if FINAL_RETAG:
            retag_final(result, body) # Dark override: latest brush face wins per result triangle
            if REBUILD_UNWELDED:
                nm=rebuild_unwelded(result, getattr(retag_final,"tri2face",{}))
                if nm is not None: result=nm
                else: unreal.log_warning("  rebuild_unwelded unavailable - keeping welded mesh")
        if _PERFACE_UV[0]:
            unreal.log("UVs: per-face Dark scale  (tile ft = px * 2^(scale-20))")
        else:
            # Fallback is the EXPLICITLY resolved BOX projection, which has no degenerate axis - every
            # face is mapped from its own dominant axis, so nothing can collapse to a barcode.
            unreal.log_warning("UVs: per-face projection was unavailable -> world-scale BOX projection "
                               "(one repeat / %.0f cm). Textures will be uniformly tiled, not Dark-exact."%UV_TILE_CM)
            apply_world_uvs(result)
    if _IMPFAIL:
        unreal.log_error("  %d TEXTURE(S) FAILED TO IMPORT - these render as flat grey:"%len(_IMPFAIL))
        for t in sorted(set(_IMPFAIL)): unreal.log_error("      %s"%t)
    else:
        unreal.log("  all textures imported OK")
    uv_fail_report()
    sel_selfcheck_report()
    final_uv_check(result)
    # One unmissable block at the end. The per-face vs fallback distinction is THE thing that decides
    # whether textures are Dark-exact or barcoded, and it was previously buried in the log.
    unreal.log("="*66)
    if not _TEX_OK[0]:
        unreal.log_error("  TEXTURES: DISABLED (no manifest / BUILD_TEXTURES off)")
    elif _PERFACE_UV[0]:
        unreal.log("  TEXTURES: per-face Dark projection ACTIVE  (%d face failures)"%_UVFAIL[0])
    else:
        unreal.log_error("  TEXTURES: per-face projection OFF -> whole-mesh BOX fallback.")
        unreal.log_error("  This is the barcode/uniform-tiling case. Cause is logged above;")
        unreal.log_error("  bisect with TEST_LIMIT to find the brush that turns it off.")
    unreal.log("="*66)
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
            # The water volume is assembled from mk(b) meshes, and mk() tags each triangle with the
            # BRUSH FACE's material id (bigbl, rufgry, ...). Those ids are meaningless here - a water
            # surface never wears a brush texture - and they left every triangle pointing at a slot
            # this mesh does not fill, i.e. untextured. Collapse them all onto slot 0 first.
            _enable_matids(WATER)
            # Dark draws a water surface ONLY at a water/air boundary. Sample just OUTSIDE each water
            # triangle (centroid + eps*outward normal) and keep it only if that lands in AIR; where it
            # lands in solid the wall owns the surface and this face must not be drawn. Keepers get
            # material 0, the rest material 1, then material 1 is deleted wholesale.
            model=build_media_model(world, body)
            keep=drop=0
            for t in tri_ids(WATER):
                nr=_tri_normal(WATER,t); c=_tri_centroid(WATER,t)
                if nr is None or c is None: _set_matid_tri(WATER,t,0); keep+=1; continue
                P=[c.x+nr.x*WATER_PROBE_CM, c.y+nr.y*WATER_PROBE_CM, c.z+nr.z*WATER_PROBE_CM]
                if medium_at(P,model)==AIR: _set_matid_tri(WATER,t,0); keep+=1
                else:                       _set_matid_tri(WATER,t,1); drop+=1
            if drop and GMAT_DEL:
                try:
                    getattr(GMAT_DEL,_MATDEL)(WATER,1)
                    unreal.log("  water surface: kept %d tris at water/air, removed %d against solid"
                               %(keep,drop))
                except Exception as e:
                    unreal.log_warning("  could not delete non-air water faces (%s) - the water box "
                                       "will show its buried sides"%e)
            else:
                unreal.log("  water surface: kept %d tris at water/air, removed %d against solid"
                           %(keep,drop))
            if tri_count(WATER)==0:
                unreal.log("No water/air surface in this mission - skipping water mesh."); return
            wpath=ASSET_PATH+"_Water"; wsm=bake(WATER, wpath)
            if wsm is not None:
                # The water look comes from the mission's FAMILY chunk, not from any brush face.
                wt=(data.get("water") or {}).get("tex_in") or ""
                wmat=None
                if _TEX_OK[0] and wt:
                    if (_MANIFEST.get(wt) or _MANIFEST_LC.get(wt.lower())):
                        wmat=_make_water_material(wt)
                    else:
                        unreal.log_warning("  water texture '%s' not in the manifest - re-run "
                                           "extract_textures.py to pull the waterhw family"%wt)
                if wmat is not None:
                    try:
                        wsm.set_material(0, wmat)      # same call the main mesh uses; static_materials
                        unreal.log("Water -> %s  material=M_%s on slot 0 (translucent, two-sided, "
                                   "%s tris retagged)"%(wpath,wt,"all" if nre<0 else nre))
                    except Exception as e:
                        unreal.log_warning("  could not assign water material (%s)"%e)
                else:
                    unreal.log("Water -> %s  (no water texture; assign a material by hand)"%wpath)
                spawn(wsm, wpath.rsplit("/",1)[-1])
            else: unreal.log_warning("water bake failed")
run()