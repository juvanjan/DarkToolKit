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

GEO_PATH    = r"C:\Nex\DarkSimProject\DarkSimToolkit\test_missions\07_geo.json"   # <-- SET (the *_geo.json)
ASSET_PATH  = r"/Game/Mission/SM_Mission"
BUILD_WATER = True        # also bake the water volume as a separate static mesh (SM_..._Water)
TEST_LIMIT  = 0           # 0 = full mission; >0 = world solid + first N brushes (quick preview)
UV_TILE_CM  = 256.0       # world-space texture tile size

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
_TCLIB,_TCFN = method_like("triangle","count")
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
    A,Bc,Cc=_solve3(M,Y)
    tr=A+Cc; disc=max(0.0,(tr/2)**2-(A*Cc-(Bc/2)**2))
    l1=tr/2+math.sqrt(disc); l2=tr/2-math.sqrt(disc)
    a=1.0/math.sqrt(l2); bmin=1.0/math.sqrt(l1)      # a = major semi-axis, along ang
    ang=0.5*math.atan2(Bc,A-Cc)
    # ellipse frame in world: major along e1 rotated by ang
    xw=[math.cos(ang)*e1[i]+math.sin(ang)*e2[i] for i in range(3)]
    yw=[-math.sin(ang)*e1[i]+math.cos(ang)*e2[i] for i in range(3)]
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

def mk(b):
    if b.get("shape")=="box":   return mk_box(b)
    if b.get("shape")=="wedge": return mk_wedge(b)
    if b.get("shape")=="cylinder": return mk_cylinder(b)
    return mk_buffer(b)

def bake(mesh, path):
    fn=getattr(GASSET,ASSET_FN); pkg,name=path.rsplit("/",1); o=AssetOpts()
    for args in ([mesh,pkg,name,o],[mesh,pkg,name],[mesh,path,o],[mesh,path]):
        if o is None and len(args)>3: continue
        try:
            r=fn(*args); return r[0] if isinstance(r,(tuple,list)) else r
        except Exception: pass
    return None
def spawn(sm, label):
    eas=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    a=eas.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,0))
    a.static_mesh_component.set_static_mesh(sm); a.set_actor_label(label); return a

# ---------------------------------------------------------------- media state machine (direct, solid-first)
def run():
    with open(GEO_PATH) as fh: data=json.load(fh)
    B=sorted(data["brushes"], key=lambda x:x["time"])
    world=B[0]; body=B[1:]
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
    sm=bake(result, ASSET_PATH)
    if sm is None: unreal.log_error("bake failed"); return
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