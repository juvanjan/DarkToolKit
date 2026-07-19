# uv_probe.py --- run inside UE (same way as build_in_ue.py). Takes ~1 second, builds nothing.
#
# WHAT IT ANSWERS
# The whole per-face texturing scheme assumes UE's planar projection uses the plane transform's
# Z axis as the projection direction (X->U, Y->V). Every barcode symptom we have seen is consistent
# with that assumption being wrong - if the rotation is ignored or a different axis is used, faces
# containing the real projection axis collapse into stripes, which is exactly the reported pattern
# (XZ faces fine, YZ and XY faces barcoded along Y).
#
# This builds ONE quad per orientation, applies the SAME transform build_in_ue.py would, reads the
# UVs back, and reports whether they vary in both directions. No guessing.

import unreal, math

_GS=[n for n in dir(unreal) if n.startswith("GeometryScript_")]
def _find(names,*kw):
    for n in _GS:
        o=getattr(unreal,n)
        for nm in names:
            if hasattr(o,nm): return o,nm
    for n in _GS:
        o=getattr(unreal,n)
        for m in dir(o):
            if all(k in m.lower() for k in kw) and not m.startswith("_"): return o,m
    return None,None

GEDIT,_APPEND = _find(["append_buffers_to_mesh"],"append","buffers")
GUV_PLN,_UVPLN= _find(["set_mesh_u_vs_from_planar_projection"],"u_vs","planar")
GUVQ,_GETUV   = _find(["get_mesh_per_vertex_u_vs","get_mesh_triangle_u_vs"],"u_vs","get")
GSEL_N,_SELN  = _find(["select_mesh_elements_by_normal_angle"],"select","normal")

unreal.log("probe: append=%s  planar=%s  getuv=%s  select=%s"%(_APPEND,_UVPLN,_GETUV,_SELN))

def _quat_from_axes(ax):
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

TILE=122.0   # a typical Dark tile in cm

# Three 200cm quads, one per plane, with the U/V/projn frame build_in_ue.py would use for each.
CASES=[
 ("XZ plane (normal +Y)  <- you report this one RENDERS OK",
  [(-100,0,-100),(100,0,-100),(100,0,100),(-100,0,100)], [1,0,0],[0,0,-1],[0,1,0]),
 ("YZ plane (normal +X)  <- you report BARCODE",
  [(0,-100,-100),(0,100,-100),(0,100,100),(0,-100,100)], [0,-1,0],[0,0,-1],[1,0,0]),
 ("XY plane (normal +Z)  <- you report BARCODE",
  [(-100,-100,0),(100,-100,0),(100,100,0),(-100,100,0)], [-1,0,0],[0,-1,0],[0,0,1]),
]

for label,pts,U,V,P in CASES:
    m=unreal.new_object(unreal.DynamicMesh)
    buf=unreal.GeometryScriptSimpleMeshBuffers()
    buf.set_editor_property("vertices",[unreal.Vector(*p) for p in pts])
    buf.set_editor_property("triangles",[unreal.IntVector(0,1,2),unreal.IntVector(0,2,3)])
    getattr(GEDIT,_APPEND)(m,buf)

    t=unreal.Transform()
    t.set_editor_property("rotation",_quat_from_axes([U,V,P]))
    t.set_editor_property("scale3d",unreal.Vector(TILE,TILE,1.0))

    applied=False
    for call in ((m,0,t,unreal.GeometryScriptMeshSelection()),(m,0,t,unreal.GeometryScriptMeshSelection(),True),(m,0,t)):
        try: getattr(GUV_PLN,_UVPLN)(*call); applied=True; break
        except Exception as e: err=e
    if not applied:
        unreal.log_error("%s : projection call FAILED (%s)"%(label,err)); continue

    uvs=None
    for call in ((m,0),(m,)):
        try:
            r=getattr(GUVQ,_GETUV)(*call); uvs=r; break
        except Exception: continue
    unreal.log("-"*70)
    unreal.log(label)
    try:
        lst=uvs[0] if isinstance(uvs,(tuple,list)) else uvs
        vals=[(round(v.x,4),round(v.y,4)) for v in list(lst)[:4]]
        us=[v[0] for v in vals]; vs=[v[1] for v in vals]
        du=max(us)-min(us); dv=max(vs)-min(vs)
        unreal.log("   corner UVs   : %s"%vals)
        unreal.log("   U spread %.4f   V spread %.4f"%(du,dv))
        if du<1e-6 or dv<1e-6:
            unreal.log_error("   COLLAPSED -> this face barcodes. The plane transform's rotation is")
            unreal.log_error("   not producing the projection axis we expect.")
        else:
            unreal.log("   OK: UVs vary in both directions (expect ~1.64 for a 200cm quad at 122cm tile)")
    except Exception as e:
        unreal.log_warning("   could not read UVs back (%s) - report the readback function name above"%e)
unreal.log("-"*70)
unreal.log("probe done")
