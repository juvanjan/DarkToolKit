# Dark Engine ground truth vs. our toolkit — findings from `SourceCode/`

Everything below is read out of the Looking Glass T2 source, with the file:line it came
from, plus data checks against the shipped `Thief 2/MISS*.MIS`. This replaces guesswork in
several places where we had tuned constants empirically.

**Caveat that applies throughout:** `SourceCode/` is the *original* Dark. The user's DromEd is
**NewDark**, which extended some limits (e.g. we see 14-sided cylinders in `test_missions/10.mis`,
but original `USED_PRIMAL_SIDES=12` caps cylinders at 12 sides) and added HD texture replacement,
which does not exist in this source at all. Core math below is authoritative; *limits* and
*texture-override behaviour* are not.

---

## 1. The BRLIST brush record — confirmed exactly

`src/editor/editbr_.h` `struct _editBrush` is the on-disk record. With `Grid` = 23 bytes
(`src/editor/gridsnap.h`: float + vector + angvec + bool):

| off | type | field | our parser |
|----|------|-------|-----------|
| 0  | short | `br_id` | ✅ |
| 2  | short | `timestamp` | ✅ |
| 4  | int | **`primal_id`** | ❌ **ignored — see §2** |
| 8  | short | `tx_id` (brush default texture) | ✅ `deftex` |
| 10 | char | `media` (op) | ✅ |
| 11 | char | `flags` | unused |
| 12 | 3×float | `pos` | ✅ |
| 24 | 3×float | `sz` (half-extents) | ✅ |
| 36 | 3×ushort | `ang` (fixang) | ✅ |
| 42 | short | `cur_face` | unused |
| 44 | 23 B | `Grid` | skipped |
| 67 | uchar | `num_faces` | ✅ |
| 68 | 4×char + int | edge/point/use_flg/group_id/pad0 | skipped |
| 76 | 10 B × n | `TexInfo txs[]` | ✅ |

`TexInfo` = `{short tx_id; fixang tx_rot; short tx_scale; ushort tx_x; ushort tx_y;}` — our
`+0/+2/+4/+6/+8` unpack is right.

**Verdict: `mis_to_geo.extract()` parses the format correctly.** The 76-byte stride and the
`nf` at offset 67 are both exactly right. The only header field we throw away is `primal_id`.

### media / op codes (`src/editor/ged_csg.cpp:221`)
`0 fill solid · 1 fill air · 2 fill water · 3 flood · 4 evaporate · 5 solid->water ·
6 solid->air · 7 air->solid · 8 water->solid · 9 blockable`

`KEEP_OPS=(0,1,2,3,4,5,8)` omits 6 and 7. Neither appears in MISS1/2/12/15, so this is
**low priority**, but they are real terrain ops and should be added for safety.

---

## 2. 🔴 Shape detection is wrong — `primal_id` encodes it exactly

`src/editor/primal.h:28-31`:
```c
primalID_Make(type,sides)  = ((type)<<9) + (sides-3)
primalID_GetType(id)       = id >> 9
primalID_GetSides(id)      = (id & 0xff) + 3
PRIMAL_ALIGN_FACE          = 0x100
```
Types: `0 SPECIAL` (4=cube, 9=dodecahedron, 10=wedge, 11=line, 12=light),
`1 CYLINDER`, `2 PYRAMID`, `3 CORNERPYR`.

We instead infer shape from face count (`classify()`: ≥7 → cylinder, 6 → box, 5 → wedge).
That is ambiguous and **wrong at scale**:

| mission | terrain brushes | misclassified | rate |
|---|---|---|---|
| MISS1 | 1473 | 47 | 3% |
| MISS2 | 1906 | 87 | 5% |
| MISS12 | 3788 | 50 | 1% |
| **MISS15** | 4010 | **646** | **16%** |

Concrete failure modes:
- **4-sided cylinder** → 6 faces → we build a **box** (168 in MISS15, 43 in MISS1)
- **Pyramid, 4-sided** → 5 faces → we build a **wedge** (109 in MISS15)
- **Pyramid, 8-sided** → 9 faces → we build a **7-sided cylinder** (206 in MISS15)
- **Cornerpyramid** → we build wedges/cylinders (76 in MISS2)

**`PYRAMID` and `CORNERPYR` are not implemented at all.** Generators are in
`src/editor/primshap.c:PrimShape_CreateNGonPyr` (apex at `z=+1`; `corner_p` puts the apex
over vertex 0 instead of the centre) — porting them is straightforward.

### 2b. The face-align bit changes geometry
`primalBr_Init` builds **two** tables per type: `vcyl` (vertex-aligned) and `fcyl`
(face-aligned), selected by the `0x100` bit. From `build_ngon_base`
(`src/editor/primshap.c:182`):
```c
ang = 2π*(2i + face_mod) / (2n)        // face_mod = 1 if face_align else 0
if (face_align && i==0) scale_f = 1/cos(ang);   // NB: persists for all i
y =  cos(ang)*scale_f;  x = -sin(ang)*scale_f;  z = ±1;
```
So a face-aligned n-gon is rotated by **half a facet** *and* scaled up by **1/cos(π/n)**
(≈1.082 for n=8) so the flat face — not the vertex — touches the unit box.

We always build vertex-aligned. Face-aligned cylinders: **91 in MISS12, 203 in MISS15** —
those come out rotated half a facet and ~8% too small.

**Good news:** our vertex-aligned phase is *exactly* right. `cyl_local` uses
`a = 2πk/n + π/2`, giving `x=cos(a)=-sin(ang)`, `y=sin(a)=cos(ang)` — identical to Dark.
Cylinder face order also matches: sides `0..n-1` (side *i* spans verts *i*→*i+1*),
top `+Z` = slot *n*, bottom `−Z` = slot *n+1* (`PrimShape_CreateNGonCyl` face_pts_list).

⚠️ Which means **`CYL_SLOT_PHASE_FRAC` should be 0, not 7/14.** Since `_cyl_slot()` works on
the *local* normal (pre-`F_REFLECT`), there is nothing left for a half-ring rotation to
correct. The 7/14 = 180° shift is compensating for a bug elsewhere — worth re-deriving
rather than carrying.

---

## 3. 🔴 The real UV algorithm — `src/csg/csgemit.c:493`

`compute_poly_texture_info()` has **two mutually exclusive branches**:

```c
if (brush < 0 || !CB_FACE_BRUSH_ALIGN_TEX(brush, face, &tex_axis[0], &tex_axis[1])) {
    /* MODE B: world dominant-axis */
}
/* scale/align applied in both modes */
```

### Mode A — face-aligned (`tx_rot == 1`)
`ged_csg.cpp:95` — the trigger is a **sentinel value in the rotation field**:
```c
if (curBrush->txs[y].tx_rot == TEXINFO_HACK_ALIGN)   // == 1
    return get_uv_align(curBrush, y, u, v);
```
`get_uv_align` (`src/editor/brrend.c:1299`) takes U/V from a **per-slot table in brush-local
space** and rotates it object→world by the brush's orientation. **No `tx_rot` rotation is
applied** (the field is a flag, not an angle).

```c
cube_src_uv[6][2] = {                 wedge_src_uv[5][2] = {
  {{0, 1,0},{0,0,-1}},   // 0  -X       {{0,-1,1},{-1,0,0}},   // 0 hypotenuse
  {{-1,0,0},{0,0,-1}},   // 1  -Y       {{1,0,0}, {0,1,0}},    // 1 -Z bottom
  {{0,-1,0},{0,0,-1}},   // 2  +X       {{1,0,0}, {0,0,-1}},   // 2 -Y leg
  {{1, 0,0},{0,0,-1}},   // 3  +Y       {{0,0,1}, {0,1,0}},    // 3 +X cap
  {{1, 0,0},{0,1,0}},    // 4  +Z       {{0,-1,0},{0,0,-1}}};  // 4 -X cap
  {{1, 0,0},{0,-1,0}}};  // 5  -Z
```

✅ **This confirms both slot orders we derived empirically** — box `0:-X 1:-Y 2:+X 3:+Y 4:+Z 5:-Z`
and wedge `0:hyp 1:-Z 2:-Y 3:+X 4:-X`. Those are correct; stop re-litigating them.

Note wedge slot 0's U = `(0,-1,1)` is **deliberately not unit length** — that non-unit U *is*
the hypotenuse stretch. `csgemit.c:387` says so outright: *"if row3 != baseaxis, then we may
end up not of unit length. This is the RIGHT behavior to make textures match up!"* So the
slant stretch is **derivable, not a tuning knob**.

### Mode B — world dominant axis (everything else)
Quake's `TextureAxisFromPlane`, verbatim (`csgemit.c:367`, layout is `{normal, u, v}` despite
the comment):
```c
{{1,0,0},{0,1,0},{0,0,-1}},   {{0,1,0},{-1,0,0},{0,0,-1}},   {{0,0,1},{1,0,0},{0,-1,0}},
{{-1,0,0},{0,-1,0},{0,0,-1}}, {{0,-1,0},{1,0,0},{0,0,-1}},   {{0,0,-1},{1,0,0},{0,1,0}}
```
Pick `best` = argmax `dot(baseaxis[i][0], normal)`; then rotate U,V by `tx_rot` **about that
axis** (X for best 0/3, Y for 1/4, Z for 2/5), sign negated when `best >= 3`. **The brush's
own rotation is never consulted in this mode.**

### What this means for our code
Our `_uv_basis()` uses the dominant-world-axis path *only* for `cylside`/`slant`, and a
face-perpendicular basis for box walls. For axis-aligned walls perpendicular == dominant, so
it coincides — but **any tilted flat face that isn't a wedge slant is projected wrongly**.
Dark uses dominant-axis for *every* Mode-B face.

And the whole `uaxis` / `capleg` / `capleg_rot` stack is a hand-rolled reconstruction of
**Mode A**, triggered by a *geometric heuristic* (`rotated brush && |n_z|>0.99`) instead of
by the actual flag (`tx_rot == 1`). That is why it needs per-case ±90° patches: it fires on
faces Dark would have put through Mode B, and misses faces Dark puts through Mode A.

`tx_rot == 1` is genuinely rare — 1 face in MISS1, 24 in MISS12, 0 in `test_missions/13.mis`.
**Every wedge in our id3–id7 test set is Mode B**, i.e. DromEd projected them from the world
dominant axis with *no* brush-rotation term. That is very likely the real reason the id5/id6
saga never converged.

### FIXED (2026-07): world caps used +pi/su=-1 (wrong rotation SIGN and inverted V)
Once baking was gated off, brush 1326's floor was still wrong two ways. The shared cap path used
`rot = -tx_rot + π` and `su = -1`: the `+π` rotates U and V 180°, `su=-1` flips U back — but nothing
flips V, so **V was inverted 180°** (invisible on a symmetric tile at rot 0; a rug's asymmetric fringe
exposes it). More importantly the rotation **sign was backwards**: the floor was landing at UE 70°
(diagonal to the 70°-yawed brush) when DromEd shows the rug **aligned to the brush edges** (UE 110°).
Ground truth from the user (not my Dark-source derivation, which had the sign inverted): for a WORLD
cap the texture rotates by its own `tx_rot` about world Z with **su=+1** and sign **+tx_rot on the
floor, -tx_rot on the ceiling** — Dark's `best>=3` negation flipped once by the level's Y-reflection.
`_is_world_cap(f)` (world-horizontal, non-cap-flag, non-uaxis, non-tilted) takes this path in
`_face_rot` / `_face_uv_transform` / `_face_uv_at`. Verified: 1326 floor U now lands on the brush edge
(UE 110°), V on the other edge (20°) = rug aligned like DromEd. Note this is NOT brush-alignment — it
aligns only because the mapper set `tx_rot == H == 70`; a different `tx_rot` would sit diagonal, which
is correct ("textures rotate by their own rotation"). Solid caps already resolved to +tx_rot/su=+1.
Cylinder & pyramid caps (cap flag) keep their own path. Builder-only change — no geo regen.

FOLLOW-UP (same brush): rotation then aligned but the rug's **V offset** was off — its fringe on the
wrong V end. Dark's cap baseaxis is **LEFT-handed** (`baseaxis[5]` U=(1,0,0) V=(0,1,0) normal=(0,0,-1):
U×V=+Z=−normal). Our frame was forced right-handed, so our V pointed 180° opposite Dark's — invisible
to the rotation (V on the right *line*) but it flips the V-offset direction (and mirrors a symmetric
rug in V, which reads as the offset moving). Fix: negate V for world caps in `_face_uv_at` (and V+projn
in `_face_uv_transform` to keep a valid RH quaternion). Verified `v = -dot(V,p)/tile + rawvoff/dv`
equals Dark's `dot(V_dark,p)/scale + align_v` with `V_dark = -ourV`, `align_v = +voff*4/256 = voff/64`
(`CB_MAX_ALIGN=256`, `csgbrush.h:57`). U offset was already right, which is why only V looked wrong.

### FIXED (2026-07): baking now gated on the real `tx_rot == 1` flag
`mis_to_geo._make_faces` used to bake `uaxis`/`vaxis` for **any** rotated brush's horizontal
cap. Baking `R @ base` only equals Dark's world-baseaxis result when `R` is a 90° multiple
(it permutes world axes); a 70° yaw does not. Brush 1326 (H=70) exposed it — its floor cap
came out 20° off DromEd — and id68's cap was a latent **90° off**. Reproduced against the
source: computing Dark's exact `compute_poly_texture_info` (baseaxis + `tx_rot` about that
axis, sign-negated for `best>=3`) and transforming to UE, the builder's **default cap path
matches Dark on all 2722 MISS1 horizontal caps (0 disagreements)**, while the baked path was
wrong on every non-90° cap. So baking is now gated on `frot[slot] == 1` (TEXINFO_HACK_ALIGN)
— brush-align faces keep it (they need R and get no `rot`; 497 such faces mission-wide), and
all other caps fall through to the rotation-independent world-baseaxis path. `mis_to_geo` must
be re-run to regenerate geo. (The larger Mode-A/Mode-B rewrite in §6 is still the endgame; this
retires the incorrect trigger without it.)

---

## 4. 🔴 Texture scale is resolution-INDEPENDENT

`src/editor/editbr_.h:27` + `ged_csg.cpp:134`:
```c
#define scale_pow2int_to_float(pow2i) (((float)(1<<pow2i))*(4.0/(float)(1<<16)))
// = 2^(pow2i) * 2^2 / 2^16 = 2^(pow2i - 14)
```
and `csgemit.c:412`: `u = dot(U, p) / tex_scale + align_u`.

So **one texture repeat spans `2^(tx_scale − 14)` feet — with no texture-size term anywhere.**
At the default `tx_scale=16` that is 4 feet, for every texture regardless of resolution. I
traced it all the way to the rasterizer (`portdraw.c:compute_tmapping` →
`libsrc/g2/pt_duv.c:g2pt_calc_uvw_deltas`) and no bitmap dimension enters.

Our formula is `tile = scale_px · 2^(sc−20)` feet, which equals `2^(sc−14)` **only when
`scale_px == 64`**. For a 128px texture we are 2× too large, for 256px 4×, for 32px 2× too small.

### Data check — designers used one scale regardless of resolution
Joining every face's `tx_scale` to its texture's `scale_px`:

| mission | scale_px 64 | 128 | 256 |
|---|---|---|---|
| MISS2 | median 16 (1361/1373) | median 16 (627/632) | median 16 (607/608) |
| MISS12 | median 16 (576/594) | — | median 16 (147/147) |

If tile size scaled with resolution, a 256px and a 64px texture at `tx_scale=16` on adjacent
walls would tile **4× apart** — no level designer would ship that. Under the engine formula
they both tile at 4 ft. This is strong independent confirmation.

**Implication:** `scale_px`, `OVERRIDE_SCALE_FACTOR=2` and `SCALE_PX_OVERRIDE` are all
compensating for a wrong formula. It's a no-op for the ~60% of textures that are 64px, which
is why it mostly worked — and the "the 2× is not uniform" symptom is exactly the residue.

> ⚠️ **Verify before ripping it out**, because HD texture override is a NewDark feature absent
> from this source. 60-second test: in DromEd put `BLOX` (64px) and `ASFALT` (256px original)
> side by side on one wall, both at scale 16. Same tile size → source model confirmed, delete
> the `scale_px` path. Different → NewDark changed it and we keep a resolution term.

## 4b. Texture offset divisor is 64, not the texture size

`src/editor/csgbrush.h:57`:
```c
#define CB_MAX_ALIGN 256
#define CB_FACE_TEX_ALIGN_U(x,y)  ((float)(get_face(x,y)->tx_x * 4) / 256.0)   // = tx_x / 64
```
Offset is added to `u` in **tile units**: `align = tx_x / 64`. We use `uoff = tx_x / scale_px`
— again correct only at 64px. Same fix as §4.

## 4c. `tx_scale == 0`
`ged_csg.cpp:127` warns and uses **1.0** (i.e. a 1-foot tile), not the default 16.
We map 0 → 16. Rare (6 faces in MISS12) but trivially fixable.

---

## 5. What's simply missing

1. ~~**Pyramid + cornerpyramid primitives**~~ — **DONE** (see §7).
2. ~~**Face-aligned primal variants**~~ — **DONE for pyramids** via `ngon_base(face_align=)`; cylinders
   still always build vertex-aligned (`cyl_local` ignores the bit).
3. **Dodecahedron** (`PRIMAL_DODEC_IDX`) — 1 brush in MISS1, ignorable.
4. **Ops 6 / 7** in `KEEP_OPS`.
5. **Mode A/B dispatch on `tx_rot == 1`** rather than on a geometric guess (§3).

---

## 7. Implemented so far

### Wedge slant UV (fixes the id5 half-texture offset)
`_uv_basis` now selects the projection axis with **Dark's signed rule on the DARK-space normal**
(`_dark_axis_index`), using the verbatim `_DARK_BASEAXIS` table. The old `abs()` test on the
*UE* normal was blind to the Y-reflection's sign flip, so for `n_dark=(0,-0.707,+0.707)` it chose
Y where Dark chooses +Z. Same U, same V slope, different constant → a pure texture shift.
Mapping into the build's conventions: `u0 = -su·F·U_dark`, `v0 = -F·V_dark`, `projn = u0 × v0`.
Verified: reproduces Dark on 12/12 slant faces; changes 3 of 325 faces overall.

### 🔴 retag_final: two matcher bugs that caused most "wrong texture" reports
Both produced symptoms that looked like texture or UV problems and were neither.

**1. Orientation tolerance was 60°.** `abs(dot(n_tri, n_face)) > 0.5` let a wedge's hypotenuse match
its own bottom face — MISS5 id33's slant normal `[-0.45,0,0.89]` has `|dot| = 0.894` with its
`[0,0,-1]` bottom, and the brush is only 61 cm tall, so slant triangles near the lower edge also fell
inside the 15 cm plane tolerance and took the bottom face's texture. That is the triangle of wrong
texture across the lower half of the slant. Now `RETAG_NORMAL_DOT = 0.99`; within-brush confusable
face pairs went 4907 → 68. **Keep the `abs()`** — an air carve's visible surface normal is the
negation of the stored brush-outward normal, so both signs are legitimate.

**2. Cell-boundary off-by-one in the spatial index.** Faces are bucketed into 128 cm cells by their
bbox. Boolean output lands on face planes to within floating point, so a triangle centroid sitting on
a cell boundary floors into the *neighbouring* cell and never sees its own face:
```
BARE tri at (-0, 813, -284) normal (-1.00, 0, 0)  candidates=18 bestcos=1.000 bestplane=6949.4cm
```
id4's matching face is at exactly x=0 (`d=0`, centroid inside the polygon) — a perfect match at
distance 0. But `floor(-1e-9/128) = -1` while the face is bucketed at `floor(0/128) = 0`, so only the
far-away `big` faces remained as candidates. Faces are now padded by `CELL_EPS = 2.0` cm when
bucketed.

**The tell was `bestcos=1.000` with a 69 m plane distance.** Perfect orientation plus an absurd
distance means the correct face is ABSENT from the candidate list, not out of tolerance. Widening the
tolerance would have grabbed an unrelated face and produced a plausible-but-wrong texture — the exact
failure mode of bug 1. When a match fails, log *why* before touching a constant.

These unmatched triangles were behind a long chain of misleading symptoms: first flat grey (material
0), then barcode stripes (the placeholder UV `(x/100, y/100)` is a fixed projection down world Z,
which collapses on any vertical face), then a wrong texture (the fallback material). Each fix moved
the symptom without touching the cause.

### ⚠️ Air brushes DO carve when BUILD_WORLD_BOX=False
They cannot carve an *empty* mesh, so an air brush before the first solid contributes nothing — but
once any solid is unioned, later air brushes subtract from it. In MISS5's first five brushes only id2
is solid, yet the result is 40 triangles rather than 12 because id3/4/5 carve it. Do not assume an
air brush is absent from the mesh.

### 🔴 Texture scale comes from `terrain_scale` in the HD pack's `.mtl`
**This supersedes everything in §4 about `scale_px` and `OVERRIDE_SCALE_FACTOR`.**

NewDark HD texture packs ship a `.mtl` beside each replacement image declaring the logical size Dark
scales by:
```
MODS/NTEX/FAM/Core_1/BIGBL2.mtl    ->  terrain_scale 128
MODS/NTEX/FAM/Core_1/bloxwall.mtl  ->  terrain_scale 64
```
**It is not derivable from any file dimension.** `bloxwall` and `BIGBL2` are both 64×64 in `fam.crf`,
both in Core_1, both shipped as DDS — yet they declare 64 and 128, and DromEd tiles them at 4 ft and
8 ft. That is why no computed property ever separated the correct faces from the wrong ones, and why
`OVERRIDE_SCALE_FACTOR` could never be right: there was no uniform rule to find. It was 2 (wrong for
64-logical textures), then 1 (wrong for 128-logical ones).

`extract_textures.py` now reads `terrain_scale` (140 `.mtl` files in this install), falling back to
the `fam.crf` size only for unmodded installs. Verified against DromEd repeat counts on MISS5_mod2:
id4618 Rustgir3 12 repeats / 48 ft = 4 ft (declares 64); id42 bigbl2 4 / 32 ft = 8 ft (128);
id98 wdplnk 1.25 / 10 ft = 8 ft (128). 8/8 tested textures match their `.mtl`.

**How to settle a scale dispute:** ask for a repeat count across a known edge in DromEd. `tile_ft =
edge_ft / repeats`. That is objective; "looks zoomed" is not, and thin faces (a 0.5 ft sliver) give
unreliable readings — two wrong diagnoses came from judging scale on slivers.

### 🔴 Face rotation must be NEGATED
`F_REFLECT = [1,-1,1]` has determinant −1, so mirroring the level reverses the handedness of
rotation: a +90° face rotation in Dark renders as −90° (270°). MISS5 id98 carries raw 16384 (=90°)
and appeared as 270°. `_face_rot()` now negates the record angle.

Affects 2104 of 26000 faces (8.1%) — mostly 90° and 270°. **0° and 180° are invariant under the
flip**, and the wedge/pyramid test missions contain only those two values, so every validation done
on 11.mis and 13.mis was blind to this. Both are provably unaffected (0 faces change).

`_face_rot()` is now the single definition of the rotation stack; there were three separate copies
(`apply_face_uvs`, `_face_uv_transform`, `_face_uv_at`) which is exactly the drift hazard this file
keeps warning about. Same for `_tile_uvoff()`, which owns the tile/offset maths.

### 🔴 The boolean returns a WELDED UV overlay — faces overwrite each other
The worst bug found in this project. It affected **every mission ever built**, including ones we
signed off as correct.

`apply_mesh_boolean` returns a mesh whose UV overlay is welded: faces sharing a vertex share its UV
element, with no seam between them. Each per-face planar projection therefore writes onto its
neighbours, and **the last face projected wins at every shared vertex**:

- **Horizontal faces survive** — their UVs are a function of (x, y), which varies across the face
  no matter which projection wrote it.
- **Vertical faces collapse** — their corners get UVs from an adjacent horizontal projection, and on
  a wall x and y are constant as you move up, so the UV stops varying. Zero UV area = barcode stripes.

Measured on MISS5_mod2: zero-UV-area 125/151 on X, 130/170 on Y, 0/167 on Z. Proven by setting
`RETAG_UV_ONLY="vertical"`, which inverted it exactly (X 5, Y 0, Z 163). Only shared elements can
produce an inversion like that.

**Fix: `rebuild_unwelded()`.** After the boolean and retag, the final mesh is rebuilt so every
triangle owns its own three vertices, with UVs computed analytically by `_face_uv_at()` —
`u = U.p/(su*tile) - uu/su`, `v = V.p/tile - vv` — the closed form of the planar projection the
transform encoded. Nothing is shared, so nothing can be clobbered.
Result: 259/504 collapsed -> **10/504**. Vertex count roughly triples; the static-mesh
"nearly zero tangents / bi-normals" warnings disappear (they were a symptom of the same overlay).
The residual 10 are triangles `retag` could not match to a face (`unmatched tris=36`).

**The old claim that "flat faces' per-primitive planar UVs survive the boolean" is FALSE.** That
assumption lived in `retag_final` and is why only cylinder sides were ever re-projected.

**Debugging lesson worth keeping:** several rounds were lost to instruments that measured the wrong
thing. "The projection call did not raise" was read as "the UVs are correct", and a UV self-check
sampled the whole brush instead of one face, returning a constant that looked like a smoking gun.
Validate the instrument before trusting it. The check that finally worked compares **UV area to world
area**, which is independent of triangle shape and so cannot confuse a sliver with a collapse.

### 🔴 mk_buffer winding — UE wants the OPPOSITE handedness
`mk_buffer` is the only path that hands UE raw triangles; every other shape comes from a
GeometryScript primitive that generates UE-correct winding itself. So this was never exercised until
the first pyramid. The geo's triangles are wound outward by our convention (`orient()` forces a
positive signed volume), and `append_buffers_to_mesh` wants the opposite — so buffer-built shapes
came out **inside-out**. One fault, three symptoms:

- faces visible only from *inside* (back-facing + one-sided materials)
- the CSG union emits a shell instead of adding volume — "two rooms with a wall between", because an
  inverted solid has negative volume
- **textures appear flipped regardless of the UV frame** — an inside-out surface mirrors its texture

That third one is a trap: it looks exactly like a UV bug and is completely immune to UV fixes.
`mk_buffer` now reverses winding (and recomputes normals from the reversed set).

**Verify geometry in UE before debugging texture orientation.** Three consecutive UV "fixes" were
made downstream of this and all appeared not to work.

### The `_uv_basis` branches, and one constraint that looks real but isn't
For the *same* Dark base axis the two hand-tuned branches differ:

| face | branch | equals |
|---|---|---|
| world-horizontal (`\|n_z\|>0.99`) | cap branch | `+F · [U,V]_dark` |
| vertical | box-wall branch | `−F · [U,V]_dark` |

It is tempting to conclude that a new branch must agree with the cap branch on horizontal faces.
**It must not — that case is unreachable.** `|n_z| > 0.99` is caught by the cap branch *before* the
Dark branch is consulted, so the Dark branch only ever sees TILTED faces. Those may *select* the ±Z
floor/ceiling axis without being horizontal — every pyramid side does (`|n_z| ≈ 0.83`).

Special-casing ±Z to the cap convention on that vacuous reasoning rotated all 14 pyramid sides (and
the 2 ±Z-selecting wedge slants) by 180°. The branch now uses `−F · [U,V]_dark` uniformly.

Beware circular verification throughout: checking a derived frame against a Dark reference built with
the *same* convention always passes and proves nothing. Anchor only on faces confirmed in DromEd.

### `primal_id` shape detection
`classify_primal(pid, nf)` replaces `classify(nf)` (kept only as a fallback for corrupt ids and
for dodec/line/light). Brushes now carry `sides` and `falign` from the record.

### Pyramid / cornerpyramid
- `ngon_base(n, face_align)` — Dark's `build_ngon_base` verbatim, including the `1/cos(π/n)`
  face-aligned radius that Dark computes once at `i==0` and reuses.
- `pyr_local(h, sides, corner, face_align)` — base ring at `z=-1`, apex at index `n`
  (centred, or over vertex 0 for cornerpyr). **Note:** Dark's `face_pts_list` is a polygon vertex
  list, *not* a triangle winding — wind sides `(i, i+1, apex)` and the base fan backwards, or
  `faces_for()` reads inward normals and every slot lands wrong.
- `_pyr_slot(nl, sides, face_align)` — record slot from the local normal: sides `0..n-1`, base `n`.
  Derived from the generator, so **no empirical phase constant** (contrast `CYL_SLOT_PHASE_FRAC`).
- Side faces carry `pyrside=1` and take the same Dark base-axis UV path as wedge slants — their
  non-unit in-plane U/V *is* the `1/cos` stretch.
- No builder work needed: `mk()` routes unknown shapes to `mk_buffer`, which builds from the geo's
  own verts/tris. Flat faces keep their per-primitive UVs through the boolean, so `retag_final`
  correctly skips them.

### Adaptive select-by-normal cone (`_safe_cone`)
`_select_by_normal` had a hardcoded **15°** cone. That is wider than the facet spacing on dense
brushes, so a face's selection also grabbed its neighbours and each planar projection overwrote the
previous one — last write wins. Two visible symptoms, both in the 11.mis screenshot: smeared
textures crossing facet boundaries, and flat single-colour triangles (a projection inherited from a
neighbour projects nearly edge-on to that facet, so its UVs collapse — the same failure the
`_uv_basis` comments describe for left-handed frames).

Measured minimum inter-face angles: **14-gon pyramid 14.41°** (11.mis), 14-gon cylinder 25.71°,
10-gon cylinder 10.30°. So the fixed cone was over-grabbing on the pyramid *and* on some cylinders
— the latter was masked because `apply_cyl_face_uvs` re-projects cylinder sides afterwards.

`_safe_cone(faces)` now returns `max(1, min(15, 0.4 × min_pairwise_angle))`. Faces are flat, so
their own triangles sit exactly on the normal and any positive cone still captures them. Applied in
`apply_face_uvs` and `_tag_by_selection`. Verified: 0 brushes across all 14 test missions where the
cone can reach a second face. (`_tag_by_triangle`, the preferred material path, was never affected —
it takes the argmax face per triangle.)

Validated on `11.mis` (14-sided pyramid, 14 distinct side textures): all 15 record slots map to the
right texture, AABB/apex/ring-radius exact, all 14 side frames right-handed, peak stretch 1.211 vs
the predicted `1/0.825 = 1.212`. `08.mis` id2 (10-sided pyramid, previously built as an 8-sided
cylinder) also now correct.

---

## 6. Suggested order of attack

1. **Read `primal_id`** for shape + sides + align bit. Biggest single correctness win
   (16% of MISS15), and it makes `classify()` obsolete.
2. **Confirm the scale experiment in §4**, then collapse `tile = 2^(sc-14)*30.48` cm and
   `uoff = tx_x/64`. Deletes `scale_px` / `OVERRIDE_SCALE_FACTOR` / `SCALE_PX_OVERRIDE`.
3. **Reimplement UVs as the two real modes** — Mode A off `tx_rot==1` with the two source
   tables, Mode B as dominant-axis for *all* other faces. This should retire `uaxis`,
   `capleg`, `capleg_rot`, `slant`, the `+π` base and the `MIRROR_TEX_U`/`solid` sign
   juggling, replacing ~6 tuned constants with the engine's own tables.
4. Add pyramid/cornerpyramid + face-aligned variants.
5. Re-derive `CYL_SLOT_PHASE_FRAC` (should be 0 — §2b).

Note that step 3 subsumes the open id6 question in `ChatInfo/InitialChatSummary.md`: under
the real algorithm id5 and id6 are both **Mode B**, so neither should get a brush-rotation
term at all.


## The synthetic world block has no faces (brush 0)

`mis_to_geo.convert()` invents brush 0: a solid cuboid 8 ft larger than the mission bounds, written
with `faces=[]`. It is NOT in the .MIS -- `extract()` returns nothing at time 0, because in Dark the
world starts implicitly solid and every brush carves into it. We need a real mesh to carve, hence the
box.

Consequence: the box's OUTWARD skin carries no texture record. `retag_final` can never match it (it
reports `candidates=0`, `bestcos=0.000` -- the giveaway that this is NOT a retag failure but a face
that does not exist), and `rebuild_unwelded` then paints it with `_fallback_matid()`: an invented
texture on a surface DromEd never draws. Dark only emits surfaces bounding an open cell, so nothing
outside the world exists.

`STRIP_WORLD_SHELL` culls it (`_is_shell_tri`): a triangle is shell iff all 3 verts lie on an outer
plane of brush 0 AND its normal points out of the world. The inward test matters -- a real level
surface can be coplanar with the shell, and it must survive. Culling is safe for collision: the
level's own inner surfaces still fully enclose the playable space.

Diagnostic worth repeating: for an axis-aligned mission the exact expected surface set can be
computed OFFLINE by voxelising the brushes through the `media_op` transition table and extracting
boundary quads. For 16.mis that gave 21 solid planes / 8 water planes, and the per-axis triangle
counts in the build log (Z=22, exactly the minimum triangulation; X=29; Y=43) proved the mesh was
complete. Do this before hunting for "missing faces" in the boolean output.


## Fill-solid union drops walls at coincident cavity planes (16.mis pits)

Symptom: a room floor is textured but its walls are MISSING (see-through holes), confirmed by the
wall being absent from BOTH sides. In 16.mis every test chamber is a pit: an air brush carves down
from the corridor floor, then a fill-SOLID brush (op=0) refills the bottom, leaving an open pit with
a wood floor and stone walls. The walls came out as holes.

Cause: the fill-solid brush has the SAME x/y footprint as the air cavity, so its four side faces are
EXACTLY coplanar-coincident with the cavity walls. `apply_mesh_boolean(UNION)` at an exact coincidence
discards the entire shared-plane polygon -- including the portion ABOVE the fill (z of the open pit),
which had no business being removed. Net: the pit walls vanish.

Why only fill-solid: op=0 UNIONS a box back into the carved cavity (creates the coincident wall).
Water (op=2) and flood (op=3) only SUBTRACT from / never re-add to the solid `result` mesh, so they
create no coincident wall and are unaffected. So the fix targets op=0 (and op=8, air->solid) ONLY.

Fix (`FILL_UNION_GROW_CM`, mk_box `_grow_xy`): inflate the union tool's two NON-VERTICAL extents by
1cm (center fixed, both opposing walls move outward). The walls then bury themselves 1cm inside the
surrounding solid -- union there is a no-op -- so no coincident plane exists and the cavity walls
survive intact. The vertical (floor-defining) extent is left EXACT, so the floor does not move. The
up-axis is the local axis most aligned with world +Z, so it works for rotated fills too. Only the
UNION tool is grown; the water-subtract tool of the same brush is left exact.

General lesson: exact coplanar coincidence between a SUBTRACT and a later UNION at the same location
is the reliable face-drop trigger in GeometryScript booleans. Nudge the additive tool outward.


## Triangle IDs have GAPS -- never iterate `range(tri_count(m))`

THE cause of "some faces are not being drawn" (16.mis id9 pit walls, and any other missing surface).

A UE `DynamicMesh` does not keep triangle ids compact. Every boolean deletes triangles and the freed
ids leave holes, so after carving 16.mis the mesh held 110 triangles spread over a LARGER id space.
Every loop in this file used `for tid in range(tri_count(mesh))`, which is wrong twice over:

  * ids inside the range that are DELETED still get queried. `get_triangle_positions` /
    `get_triangle_face_normal` return zeros, which surfaced as 11 bogus "degenerate" triangles at
    (0,0,0) with a (0,0,0) normal in the retag bare-tri report. They are not degenerate, they are
    not triangles at all.
  * REAL triangles whose id sits past the count are NEVER VISITED. `rebuild_unwelded` copies the mesh
    triangle by triangle, so those were silently dropped from the final mesh -> see-through holes.

The arithmetic is the giveaway, and it is worth checking first whenever faces go missing:
`rebuild_unwelded: 99 tris` + `degenerate culled=11` == `Done carving: result tris=110`. Built plus
"degenerate" exactly equalling the mesh count means the loop visited the wrong id set.

Fix: `tri_ids(mesh)` enumerates via `get_num_triangle_i_ds` + `is_valid_triangle_id` (both were
already in the resolved-function list all along). All six iteration sites now use it. If those APIs
are missing it falls back to `range(count)`, i.e. the old behaviour.

Note this ALSO silently corrupted texturing, not just geometry: retag keys `tri2face` by triangle id,
so with the wrong id set some triangles were tagged from a neighbour's entry.

Dead end recorded so it is not retried: the missing walls were NOT a coplanar-coincidence artifact
between the air carve and the fill-solid union. `FILL_UNION_GROW_CM` at 1cm and at 30cm produced
byte-identical boolean output (110 tris, identical probe) -- a 30x change with zero effect is what
disproved it. It is left in the file at 0.0.


## Water textures: the FAMILY chunk, not the brush faces

A water surface NEVER takes a texture from a brush face. `get_texture_for_medium_transition`
(editor/cvtbrush.c:169) overrides the face id with one of two RESERVED slots, chosen by which way
you cross the boundary:

    air -> water  =>  WATERIN_IDX  = 247    (surface seen from the air side)
    water -> air  =>  WATEROUT_IDX = 248    (surface seen from underwater)
    same medium both sides => 0, the boundary is not rendered at all

All three reserved ids (247, 248, and 249 = BACKHACK/sky) are also flagged RENDER_DOESNT_LIGHT
(csg.c:252) -- water and sky are UNLIT in Dark, never lightmapped.

Which images fill those slots comes from the mission's FAMILY chunk, entry 1 (entry 0 is the sky
family) -- `family_name_block_build`, render/family.c:900. `family_load_water(prefix)` then loads
`<prefix>in` and `<prefix>out` from `fam\waterhw\` (family.c:406; the dir is picked by render type,
`fam\water\` for software and `fam\waterhw\` for hardware, family.c:58).

Chunk layout: 24-byte header, then size_per(4) = FAM_NAME_LEN = 24, then cnt(4), then cnt entries.
NewDark raised MAX_FAMILIES from 16 to 32, so cnt is 34, not the 18 the shipped source implies. Read
it with read_water_prefix() (both mis_to_geo.py and extract_textures.py have one).

EVERY mission in this repo says `gr` -- green water. Do not guess blue; read the chunk.

FLOW_TEX is a different, per-flow-group override (`sEdMedMoSurface`, editsave.c:558): 256 entries of
32 bytes {short in, short out, char prefix[16], char pad[12]}, payload starting 24 bytes into the
chunk. In our missions every entry is in=247 out=248 with an EMPTY prefix, i.e. "use the mission
default", so FAMILY is the operative setting.

Where the images live: the NewDark HD pack `MODS/t2water/FAM/WATERHW/` is active via `mod_path` in
cam_mod.ini, so that is what DromEd actually displays -- 256x256 DDS with real alpha baked in, plus
20 animation frames each (`GRin_1..19`) and a .mtl declaring `terrain_scale 128`, `ani_rate 60`,
`ani_frames 20`. We currently use the single base frame; the `_N` frames are there if the water is
ever animated. index_sources() already picks the loose HD .dds over the fam.crf PCX.


## A water surface exists ONLY where water meets air

Dark does not draw the water volume -- it draws the water/air INTERFACE. Two rules in the source:

  * `get_texture_for_medium_transition` (editor/cvtbrush.c:169) returns WATERIN for air->water and
    WATEROUT for water->air, and nothing else. A water/solid boundary is not a water surface.
  * `ConvertOnePortal` drops a boundary outright when both sides settle to the same medium:
        if (dest_final_medium == final_medium) { render_info->texture_id = 0; }

Where water meets solid, the visible surface belongs to the SOLID brush and wears its own face
texture. The water volume's face there must not be drawn at all or it z-fights the wall. For a pool
sunk into a chamber floor that means exactly ONE drawn face: the top.

We decide this exactly rather than by heuristic. `medium_at(P, model)` replays the media ops in brush
order the way DromEd does; every Dark primitive is CONVEX, so "P inside brush" is just
`dot(n,P) <= d` over the brush's face planes (derived from its own triangles and oriented outward
against the brush centroid, so winding does not matter -- which is essential, because the geo's
Y-reflection has determinant -1 and flips winding). Then each water triangle is probed at
`centroid + WATER_PROBE_CM * outward_normal`: lands in AIR -> keep, anything else -> delete.

Validation worth repeating for any change to the media logic: `medium_at` was cross-checked against
the independent voxel model over all 6048 cell centres of 16.mis with 0 disagreements, and the water
brush's six faces then resolved to cull/cull/cull/cull/cull + draw-top.

Implementation note: the water volume is assembled from `mk(b)` meshes, and `mk()` tags every
triangle with the BRUSH FACE's material id (bigbl, rufgry, ...). Those ids are meaningless on the
water mesh and pointed at slots it never fills, which rendered it untextured. Keepers are retagged to
material 0 and the culled ones to material 1, then `delete_triangles_by_material_id(WATER, 1)`
removes them in one call. Assign with `sm.set_material(0, mat)` -- the same call assign_materials
uses; `set_editor_property("static_materials", ...)` did not take.


## Animated textures (water)

Dark animates a texture by loading sibling files named `<base>_1`, `<base>_2`, ... next to the base.
`ectsAnimTxtIgnore` (render/anim_txt.c:56) is what parses that `_<digits>` suffix -- it exists to stop
the frames being loaded as ordinary textures in their own right. The BASE file is frame 0 and the _N
files follow, so `ani_frames 20` means base + _1.._19 (exactly what MODS/t2water ships).

Parameters come from the .mtl (doc/material-format.txt); only the base/root texture of an animation
carries one:

    ani_rate    MILLISECONDS PER FRAME, not fps. Default 250 (DEF_RATE in anim_txt.c).
    ani_frames  frame count; 0 = however many files are found.
    ani_mode    WRAP (default, DEF_FLAG) | REVERSE | PINGPONG.
    force_ani_settings 1   these win over DromEd's "Texture Anim Data" property.

Water: `ani_rate 60`, `ani_frames 20` -> a 1.2 s loop at 16.67 fps.

WRAP is a plain forward cycle: `ectsAnimRunSingle` increments cur to cnt-1, then `ectsAnimHitEdge`
snaps it back to 0. REVERSE/PINGPONG instead bounce by toggling the FLAG_REVERSE bit at each end.

Our implementation: extract_textures.py packs the frames into a sprite-sheet atlas
(`<name>_anim.png`, 20 frames -> 5x4 of 256x256 = 1280x1024) and records
`anim {atlas, frames, cols, rows, rate_ms, mode, loop_s, frame_size}` in the manifest. build_in_ue.py
builds the material with ONE Custom HLSL node mapping elapsed Time into the current frame's cell --
one node rather than ~15 wired expressions, so there is far less to get wrong. Only WRAP is
implemented; a non-WRAP mode logs a warning rather than quietly looking wrong.

Two details that matter:
  * `frac(UV)` inside the node keeps a TILING surface within its frame cell (water tiles many times
    across a pool). That frac seam would otherwise pick wrong mips, so the atlas is imported with
    `TMGS_NO_MIPMAPS`.
  * Atlas row 0 is at the TOP and UE's V=0 is also the top, so the row offset needs no flip.

Verify the frame schedule OFFLINE before building -- render the HLSL with the manifest's numbers and
step time in ani_rate increments. It must hit frames 0..19 in order and return to 0 at exactly
loop_s. That caught nothing this time but costs one command.

Animated textures are not water-specific: any face texture with `_N` siblings gets an atlas too
(blin/blout are applied as ordinary face textures in MISS5_mod2). Only the water material currently
consumes the atlas -- `_make_water_material`. Wiring the same flipbook into the regular per-face
material path (`_make_material`) is the obvious follow-up.


## The complete media op table

`mediaop_names` (editor/ged_csg.cpp:221) and `media_op[]` (:235). MediaOp is `uchar[MAX_MEDIA]`
mapping CURRENT medium -> NEW medium; MAX_MEDIA is 6 because SOLID/AIR/WATER each have a _PERSIST
variant (media.h:11), but persistence only controls whether a later brush may override the cell, so
it makes no difference to shape and we collapse onto the three base media.

  op  name           S->  A->  W->    set ops on the SOLID mesh (`result`) and the WATER mesh
  --  -------------  ---  ---  ---    -------------------------------------------------------
  0   fill solid      S    S    S     SOLID |= B ; WATER -= B
  1   fill air        A    A    A     SOLID -= B ; WATER -= B
  2   fill water      W    W    W     SOLID -= B ; WATER |= B
  3   flood           S    W    W     t = B - SOLID ; WATER |= t
  4   evaporate       S    A    A     WATER -= B
  5   solid->water    W    A    W     t = B & SOLID ; WATER |= t ; SOLID -= B
  6   solid->air      A    A    W     SOLID -= B                       <- water untouched
  7   air->solid      S    S    W     t = B - WATER ; SOLID |= t       <- water untouched
  8   water->solid    S    A    S     t = B & WATER ; SOLID |= t ; WATER -= B
  9   blockable       S    A    W     nothing (persist flags only)

Ops 6 and 7 were missing from KEEP_OPS, so those brushes were dropped at export and did nothing at
all -- no warning, the brush simply had no effect. Op 9 is still excluded on purpose: its row is the
identity on the base media.

The distinction that is easy to miss: `solid->air` (6) is NOT the same as `fill air` (1). Both clear
solid, but fill air also clears WATER while solid->air leaves water alone. Likewise `air->solid` (7)
is not `fill solid` (0): fill solid overwrites water too, air->solid does not.

Note op 8 is `water->solid`, not `air->solid` -- an earlier comment in this file had them swapped
even though the implementation was right.

Derive-then-check, rather than hand-writing the boolean sequences: for a transition table T and
brush region B,
    SOLID' = (SOLID - B) | union of {X&B : T[X]=SOLID}
    WATER' = (WATER - B) | union of {X&B : T[X]=WATER}
Two offline checks are worth re-running after ANY change here, both cheap:
  1. MEDIA_T in build_in_ue.py vs the table transcribed from ged_csg.cpp -- must be 0 mismatches.
  2. Simulate the CSG branches on a 6-cell grid holding all three media, with B covering one cell of
     each, and compare against the table. Also assert SOLID and WATER never overlap.

16.mis is the test bench: four chambers, each with a fill, then a later op applied to the +Y half
only, so both halves are visible side by side (evaporate / water->solid / solid->water / solid->air).


## An empty boolean result is a FAILURE that leaves the target unchanged

This is the single most dangerous property of GeometryScript's boolean for the media CSG, and it bit
us as "solid->water also turned AIR into water".

`apply_mesh_boolean` logs "Boolean operation failed due to an empty result" and leaves the TARGET
MESH UNTOUCHED. Whether that is harmless or catastrophic depends on what the target is:

  * SAFE - a SUBTRACT/UNION applied straight to the accumulator (`result` or WATER). If the brush
    does not overlap, "unchanged" is precisely the correct outcome.
  * CATASTROPHIC - building an intermediate. Ops 3/5/7/8 compute
        op3 t = B - SOLID      op5 t = B & SOLID      op7 t = B - WATER      op8 t = B & WATER
    and then merge t into an accumulator. When that intersect/subtract comes back empty, `t` is still
    THE WHOLE BRUSH, so the merge floods the entire brush volume into WATER or SOLID.

16.mis has two identical solid->water brushes (id14, id15). id14 correctly converted the solid; id15
then found no solid left, its INTERSECT failed, and `WATER |= t` unioned the whole brush -- including
the air above the fill -- into the water volume. Exactly the reported symptom, and it would equally
affect a second flood, a water->solid over already-solid ground, etc.

Guard: `media_present()` asks the exact media model whether any of the media the op CONSUMES exists
inside the brush, replaying only the brushes BEFORE it (bbox-pruned, so it stays cheap on large
missions). If not, the op is skipped entirely -- which is also what the media table says should
happen, since with no source medium present the row is the identity. NEEDS = {3: AIR/WATER,
5: SOLID, 7: SOLID/AIR, 8: WATER}.

Note which half of each op is conditional: op5 always does SOLID -= B (S->W removes it from solid
regardless), and op8 always does WATER -= B; only the gain side is guarded.

The guard samples a grid inside the convex brush. A medium present only as a sliver thinner than the
sample spacing could be missed, in which case we skip an op that would have changed almost nothing --
far better than flooding the brush. The boolean still does the exact geometry whenever the answer is
yes; sampling only ever decides go/no-go.

`VALIDATE_MEDIA` audits both finished volumes against the model: sample just inside each boundary
triangle and assert it encloses the expected medium. Any disagreement means the BOOLEAN diverged from
the media table (the table itself is cross-checked against ged_csg.cpp and an independent voxel
model), and it names the offending coordinates. Leave it on -- it is how this class of bug becomes
visible instead of silent.


## Partly-exposed faces must be SPLIT at the media boundary, not per-triangle

A per-TRIANGLE keep/drop test is not enough for deciding which water surfaces to draw. Where a face
is only PARTLY exposed to air, the centroid test hands one whole triangle to air and the other to
solid, cutting the surface along the triangle DIAGONAL. Dark cuts along the brush boundary -- a
straight edge -- so the result is visibly wrong: a big diagonal wedge of water.

This is the same failure as the id34 "big triangle" on brush faces. There we could only AVOID it (by
requiring a face polygon to cover the whole triangle before claiming it); on the water surface we can
fix it properly, because we own the mesh: clip each triangle by the brush face planes that actually
cross it, then test each piece. Every media boundary is one of those planes, so after clipping each
piece lies wholly in one medium and the centroid test is exact.

`clip_surface_to_medium()` does this and returns a NEW surface mesh. Two things that matter:
  * Prune planes per triangle (skip any whose signed distances do not straddle the triangle), or it
    is O(water tris x every plane in the mission).
  * Clipping REBUILDS the mesh, so UVs applied beforehand are lost with the old one -- re-run
    ensure_uv_normals afterwards or the flipbook samples a constant UV and the water renders as one
    flat colour.

Worked example (16.mis): id17 `solid->air` spans x[-24,0] y[-28,8], which reaches PAST chamber 3, so
the water body's x faces meet air for y[0,8] and solid for y[8,20]. The clip splits that face at
exactly y=8 -- keep y[0,8], cull y[8,20] -- instead of slicing it corner to corner.

Worth re-checking offline after any change: take the water body's face as a quad, run the same plane
list over it, and confirm the pieces land on brush boundaries with the expected keep/cull verdicts.


## A mesh built from MeshBuffers MUST carry uv0 -- missing UVs CRASH the editor

`Assertion failed: NumUVs > 0 [StaticMesh.cpp]` takes the whole editor down; it is not a soft
failure or a log line. It happens when a DynamicMesh appended via `append_buffers_to_mesh` has no uv0
channel and is then baked to a StaticMesh.

`ensure_uv_normals(m)` does NOT rescue this. Its projection needs a UV layer to already exist, and it
swallows the failure, so the mesh reaches bake() with zero UV channels and asserts.

Rule: any `MeshBuffers` you append must set "uv0" alongside "vertices"/"triangles"/"normals", and the
code should refuse to return a mesh whose uv0 could not be set rather than hand a crash to bake().
Both mesh builders here do that now -- rebuild_unwelded and clip_surface_to_medium.

Project each polygon from ITS OWN dominant axis, never one axis for the whole mesh: a single planar
projection leaves every face parallel to that axis edge-on, collapsing it to zero UV area (the
barcode failure again).

Related ordering trap: clipping REBUILDS the mesh, so UVs written into the buffer would be destroyed
by a later whole-mesh `ensure_uv_normals(WATER)` with uvs=True. Pass uvs=False once the buffer
carries its own.

Water tile: `WATER_TILE_CM` = 243.84 (8 ft), matching the waterhw pack's `terrain_scale 128` at Dark
scale 16 (128 * 2^(16-20) = 8 ft). Water has no brush-face record, so there is no per-face scale to
read -- unlike terrain, this one is a constant.


## blockable (op 9): no geometry, no media change -- collision only

What it is in Dark: NOT a solid. Its media_op row maps AIR->AIR_PERSIST and WATER->WATER_PERSIST,
and `ConvertPersistantCells(CELL_CAN_BLOCK_VISION)` (ged_csg.cpp:603) then resolves those straight
back to the base medium (`ConvertFindFinalMedium` just subtracts the offset) and flags the cells
CELL_CAN_BLOCK_VISION. So the final medium is unchanged; the brush exists to SPLIT CELLS.

Its real job is doors. `DrBlkGenerateBrushes` (editor/doorblok.cpp) walks every RotDoor/TransDoor
object at portalize time, synthesises a blockable brush at the door's closed position, and
`DrBlkDestroyBrushes` deletes them afterwards. The blocking comes from the door OBJECT; the brush
just gives it a portal to block.

We export it (KEEP_OPS includes 9, and the terrain-op guard is op<=9) but it must never reach the
SOLID mesh -- unioning it into `result` would make it a visible wall. It goes into its own BLOCK mesh,
baked as <ASSET>_Blockers with collision on and the component hidden. A hidden StaticMeshComponent
still collides, so it blocks without drawing. MEDIA_T[9] is the identity, so medium_at is correctly
unaffected by it.

### Trap found here: a real brush can have time 0

mis_to_geo used to give the synthesised world box time=0, and the builder picked the world as the
first brush after sorting by time. But DromEd brush times start at 0, and 16.mis has TWO brushes at
time 0 (id27, id29) -- they tied with the world box. Only Python's stable sort kept the world box
first; any reordering would have made a 16x20x16ft brush "the world" and turned the real world box
into an ordinary solid fill, filling the level in.

The world box now has time=-1 AND an explicit `world: true` flag, and the builder selects it by that
flag (falling back to B[0] for older geo). Never identify the world brush positionally.


## The water SURFACE must be a model water/air boundary, not just "air outside"

Symptom: a green water disc filled the courtyard centre of MISS1_mod where DromEd shows dry floor.

check_media at the centre (0,0,z) ends in SOLID/AIR: giant fill-water cylinders (id52-57) are
cancelled by later water->solid + fill-air + flood + evaporate. So the MODEL has no water there. But
the raw boolean WATER volume still had that water - the ops cancel analytically, yet the accumulated
booleans did not fully undo the union. clip_surface_to_medium kept any face whose OUTSIDE sampled AIR,
so the leftover volume's top surface (air above) was kept and rendered.

Fix: a real water surface is a WATER/AIR boundary. Keep a clipped face only if the OUTSIDE is AIR AND
the INSIDE is WATER, both per medium_at. This makes the water surface exact per the media table and
IMMUNE to leftover raw-volume geometry - the raw water mesh is now used only for triangle POSITIONS;
the model decides what survives. Verified offline: courtyard centre rejected (no boundary), genuine
pools like id1753 (-11,83,14) kept. It also shrinks the output massively (the 300k-tri / 571MB water
mesh and its degenerate-tangent warnings were mostly leftover volume).

General principle for anything derived from the boolean meshes: trust the analytic medium model for
WHAT should exist, and use the boolean geometry only for WHERE the triangles are.

## validate_volume must be NORMAL-INDEPENDENT

First version sampled `centroid - outward_normal*eps` and expected `want` - it assumed outward
normals. Where the boolean left inward-facing triangles it probed the wrong side, so a DEEPER probe
made the wrong-count RISE (7088 at 4cm -> 12425 at 40cm) instead of fall. That is the signature of a
normal-direction bug, not a media bug.

Correct check: a surface triangle of the `want` volume separates `want` from not-`want`. Sample BOTH
sides (centroid +/- normal*depth); legit iff exactly one side is `want`. Flag only "floating" faces
(`want` on NEITHER side); "buried" faces (`want` on both) are wasteful, not wrong, and reported
separately. Depth is a few facet widths to clear cylinder/wedge faceting noise.


## Cylinders must be built from geo verts, never append_cylinder (phase)

Symptom (17.mis): a tower of stacked cylinders came out as a jagged sawtooth instead of clean nested
rings. The tower is 7 cylinders of DIFFERENT side counts (16,15,14,13,12,11,10, id2..id8).

Cause: mk_cylinder used the GeometryScript append_cylinder primitive for non-face-aligned cylinders.
append_cylinder places facets at UE's OWN phase; recover_cylinder can fit centre/radius/rotation but
NOT the phase, because a circular cross-section has no major axis to key off. So each layer got an
arbitrary phase and the facets did not stack. (A lone cylinder hides this; a stack of different-n
cylinders does not.)

Fix: mk_cylinder now ALWAYS returns mk_buffer, i.e. the exact baked geo verts, whose phase is Dark's
(cyl_local/build_ngon_base, pi/n) for every layer, and which also carries any per-brush rotation and
elliptical shape faithfully. Side UVs are overwritten afterwards by apply_cyl_face_uvs (per-facet,
own-normal), so the primitive path's "clean UVs" - its only claimed advantage - were never needed.
The face-aligned path already used mk_buffer for exactly this phase reason; this just extends it to
all cylinders.

Rule: for any Dark primitive, prefer mk_buffer (the baked geo) over a UE primitive. The geo is the
authoritative Dark geometry; a UE primitive re-derives it with UE conventions and loses phase.


## Cylinder side texture: emit an explicit slot (side k -> record k)

Symptom (17.mis): on a tower of cylinders with different side counts, the wood/stone pattern sat at
the wrong CLOCK POSITION on id3 (15 sides) and id7 (11 sides) - shifted by one facet - while every
even cylinder and id5 (13) were correct.

Cause: cylinder sides were the last shape inferring their texture-record slot from the face-normal
AZIMUTH (_cyl_slot), which needed the empirical CYL_SLOT_PHASE_FRAC constant. Verified against the
correct even cylinders in DromEd, the true mapping is trivial: SIDE k -> RECORD slot k. The azimuth
path reproduced that for even n, but on ODD side counts the atan2 round() tipped exactly ONE facet
across a step boundary and handed it slot k-1, so that facet (and everything read relative to it)
looked rotated. n=13 happened to land right; 15 and 11 did not.

Fix: cyl_local now emits an explicit per-triangle tslot exactly like pyr_local/dodec_local - side k
-> slot k, top cap -> n, bottom cap -> n+1 - and faces_for keys on it (falling back to _cyl_slot only
for pre-tslot geo). No azimuth, no rounding, no phase constant. Verified: even cylinders and n=13 are
byte-identical to before; id3 and id7 each shift by one facet to the correct spot.

Lesson (again): use the generator's known slot, never re-derive it from a normal. This is the same
fix already applied to pyramids and dodecahedra; cylinders were just the last holdout. Combined with
building cylinders from geo verts (mk_buffer, not append_cylinder), cylinder geometry AND texturing
now come straight from the baked geo.


## Phantom solid from intersect-based ops (water->solid / air->solid)

Symptom (MISS1_mod): a point the model AND DromEd call AIR came out SOLID in the build.

Trace: fill-water carved it to water, evaporate (id93) turned it to air; then water->solid (id178,
op8) ran. id178 has legit water elsewhere so its guard correctly proceeds, but op8 adds solid as
`brush INTERSECT WATER_mesh` UNIONed into result. On a big mission the accumulated boolean WATER mesh
(dozens of unions/subtracts) does not exactly match the analytic model - the evaporate did not fully
clear the water at this point in the MESH - so op8 converted that stale/phantom water to solid where
the model says air. 42 water->solid brushes in MISS1_mod, so this is widespread, not a one-off.

Same root as the water-surface bug: the accumulated boolean volume diverges from the analytic model.
Water was fixed by rebuilding its surface from the model; the SOLID mesh inherits the divergence
through op5/op8, which read the stale meshes.

Fix (MODEL_CULL_SOLID, in rebuild_unwelded): the media model is the authority. A real solid surface
triangle has SOLID on exactly one side per medium_at; a phantom-solid blob's surface has air on BOTH
sides. Sample both sides (+/- MODEL_CULL_PROBE_CM along the normal) and drop the triangle if neither
side is SOLID. The phantom blob loses its shell and renders (and collides) as nothing. Probe 8cm
clears sub-facet faceting offsets (a few cm) while staying inside any real wall thicker than it, so
genuine thin walls - whose model interior IS solid - survive. Flag defaults on; watch the
'phantom-solid culled' count and set False if it ever removes a real (sub-8cm) wall.

This is a post-hoc CORRECTION, not a true fix of op5/op8. A real fix would feed those ops a
model-correct water/solid volume instead of the accumulated boolean mesh; that is a larger change
(essentially rebuilding volumes from the model, as Dark's BSP does). The cull removes the visible/
collidable symptom uniformly and cheaply. UNVERIFIED IN-ENGINE at time of writing.


### UPDATE: MODEL_CULL_SOLID failed in-engine - disabled

Tested on MISS1_mod: it culled 6785 tris and REAL WALLS VANISHED. The premise - "a real solid
surface tri has SOLID on one side per medium_at" - does not hold at an 8cm probe on dense geometry:
a genuine wall frequently has air within 8cm on BOTH sides (the wall is thin, or there is an adjacent
air pocket/room on its 'solid' side), so it reads as phantom and gets removed. The normal-independent
validate_volume uses the same test and its "floating SOLID" count (6686 here) is therefore mostly
FALSE POSITIVES, not real phantoms - do not treat that number as a bug count.

Conclusion: medium_at is reliable as a WHOLE-CELL / coarse oracle (it matched the voxel model and
DromEd for point queries), but NOT as a per-triangle few-cm-probe discriminator between phantom and
real solid. Flag left OFF. The phantom solid from water->solid over stale accumulated water remains a
real but localised issue; a safe fix needs a model-correct water VOLUME fed to op5/op8 (a larger
rebuild), not a post-hoc surface cull. Do not re-enable without a fundamentally different test.


### MODEL_CULL_SOLID, take 2: exact-plane probe + safety cap (re-enabled)

The take-1 failure (deleted real walls) was NOT medium_at being wrong - validated on MISS1_mod, the
convex-plane point test agrees with an independent ray-cast mesh test to 7/11800 points (boundary
ties) across box/cylinder/wedge/pyramid/dodec. It was the PROBE: 8cm from the triangle centroid
crosses the many thin features of a detailed mansion (trim, ledges, moldings), so their solid side
read as air.

Take-2 evaluates the model INFINITESIMALLY off the EXACT retag face plane, the way Dark decides a
boundary:
  - project the triangle centroid onto f's exact plane (n,d), then step +/- MODEL_CULL_PROBE_CM (1cm)
    along f's normal. A wall >=1cm thick keeps SOLID on one side -> survives, regardless of thickness.
  - only MATCHED tris are eligible (an unmatched tri has no trusted plane).
  - decided in a PRE-PASS; if the cull would drop more than MODEL_CULL_MAX_FRAC (6%) of tris it ABORTS
    entirely and keeps the full mesh - a hard guard against another 'walls vanished'.

This works now that cylinders build from geo verts (mk_buffer): built surface == geo plane, so there
is no faceting offset to clear and a tiny probe is safe. It is still a post-hoc correction of the
op5/op8 phantom, not a cure of the boolean-volume drift, but it is thin-feature-safe and self-limiting.
UNVERIFIED IN-ENGINE; watch the 'phantom-solid culled' count and confirm trim/ledges survive.


### MODEL_CULL_SOLID, take 3: also a dead end - disabled for good

Even off the EXACT retag face plane with a 1cm probe, the cull flagged 12% of MISS1_mod result tris
(safety cap aborted, so no walls lost this time). A neighbourhood scan of the flagged coords showed
they sit right next to real solid (within 30cm; one was itself solid) - i.e. they are REAL wall
surfaces, mis-flagged. Cause: ~12% of retag matches put the triangle on a slightly-offset face plane,
so projecting onto that plane and sampling +/-1cm lands in air on both sides even though the triangle
is a real boundary.

Conclusion, now firm: a post-hoc PER-TRIANGLE cull cannot isolate phantom solid, because it compounds
two unreliable things - the boolean result geometry and the retag plane match. All three probe
variants (8cm centroid, 1cm centroid, 1cm exact-plane) either delete real walls or, capped, do
nothing. Do not attempt a cull-based fix again.

The only correct fix is to stop deriving the SOLID volume from accumulated booleans at all and emit
its surface from the media model + geo faces directly - the way the WATER surface already works
(clip_surface_to_medium): for each brush face polygon, clip to its visible extent and keep the pieces
that are a real solid/non-solid boundary per medium_at, textured by that face. That removes the
phantom by construction (op8's stale-water solid never enters the geometry). It is a new pipeline with
its own risks (cracks/T-junctions between per-brush faces, performance) and must be built iteratively
in-engine. Scoped but not small.


## Option 3: local media rebuild for the intersect ops (5/7/8)

The phantom solid (id178 corridor) is baked by the ops that CONVERT a region by intersecting the
brush with the running SOLID/WATER mesh: op5 solid->water (reads SOLID), op7 air->solid (reads
WATER), op8 water->solid (reads WATER). That global mesh carries ~1400 booleans of accumulated
drift, so e.g. an evaporate's subtract leaves residual water that op8 later turns to phantom solid.

Fix (LOCAL_INTERSECT_REBUILD): for each such brush, replay ONLY the earlier brushes whose bbox
overlaps it into a FRESH local (LS, LW) using the exact media ops, and intersect against that instead
of the global mesh. A short clean sequence (avg 36 brushes on MISS1_mod) subtracts reliably, so the
stale water is gone and op8 adds nothing where the model says air. `mk(b, tex=False)` builds the
throwaway replay tools without material/UV work.

Validated offline (the LOGIC, not the UE booleans): a per-point simulation of the local replay gives
LW=empty at the phantom point (op8 adds nothing), and matches the analytic model at 0/208 sampled
points across every op5/7/8 region. Boolean EXECUTION is untested but a short local sequence is far
more reliable than the 1400-op global one.

Cost: ~3300 extra boolean ops (2-3x build time). Contained: only ops 5/7/8 change; everything else
(fill ops, textures, water surface, cylinders) is untouched. Flag off = old behavior. This is a
correction of the intersect source, not the full model-driven rebuild (1b); it does not fix any
boolean-fidelity artifact outside ops 5/7/8, but those three are the only ones that read a drifted
mesh to create geometry.


## THE ROOT CAUSE: the empty-result trap

`BooleanUnion: Boolean operation failed due to an empty result` has been in every build log. It is
not noise - it is the root cause of the phantom solid. GeometryScript's apply_mesh_boolean REFUSES a
SUBTRACT whose result would be empty (the tool fully contains the target) and leaves the target
UNCHANGED. So when an evaporate box fully contains the water it should clear, the water is NOT
removed - and a later water->solid converts that stale water into phantom solid.

This is why nothing worked: it defeats the global water mesh (evaporate covers all current water ->
subtract refused -> water survives) AND the option-3 local rebuild even harder (the local water is
small and the evaporate always contains it). It is a specific, fixable engine behaviour, not vague
'drift'.

Fix (in local_media's `sub`): before a removal subtract, if the tool's convex hull fully contains the
target mesh's AABB (mesh_bbox + _inside_solid on all 8 corners), return a fresh EMPTY mesh instead of
calling the boolean that would refuse to empty it. mesh_bbox uses GeometryScript get_mesh_bounding_box
(method_like 'bounding','box'); if that is unavailable sub() falls back to the plain subtract (no
regression, no fix - visible in the log).

Broader implication: the same trap affects the GLOBAL water/result subtracts (WOP, result-=brush).
With LOCAL_INTERSECT_REBUILD on, op5/7/8 read the local meshes so the global residue no longer feeds
phantom solid, and the water SURFACE is model-clipped (immune). If phantom water/solid ever appears
via the global path, apply the same empty-result guard to WOP.


## Retag subdivision: split straddling triangles at coplanar-face boundaries

Symptom: the diagonal 'big triangle' where a wall quad straddles the boundary between stacked coplanar
faces from different brushes (id180 side9: brick z12-20 / mold09 z20-24 / brick z24-40 at x=-92.3;
id645 north: cracked / brick / mold09 at y=33). retag's per-triangle assignment can't represent a
boundary that cuts across a triangle: the full-coverage test leaves the straddler unmatched, the
rescue pass hands it to one neighbour, and its two halves show different textures split on the
triangle diagonal.

Fix (RETAG_SUBDIVIDE): retag now, for a triangle no single face fully covers, collects the coplanar
faces that PARTIALLY cover it (>=1 vertex inside) into tri2sub[tid], latest-first. rebuild_unwelded
then clips the triangle among those faces (_clip_tri_among: each face claims its overlap via inward
edge-plane clipping, remainder cascades to earlier faces = Dark's 'later wins') and textures each
piece with its own face; uncovered leftover -> fallback. Same family as the water-surface clip.

REFINEMENT (the id180-vs-id645 split): the first cut only subdivided when NO face fully covered the
triangle (best is None). That fixed id645 but NOT id180, because a tall brick brush (id9, z0-24) spans
the whole wall and FULLY covers the straddler -> best=id9, no subdivision, and the later mold09 band
(id180, z20-24) that should override the top could not. The correct texture at any point is the LATEST
face covering it, so retag now computes, per triangle VERTEX, the latest covering face; if all three
agree -> that face covers the whole (convex) triangle, assign it whole (fast path); if they DISAGREE
-> subdivide among ALL coplanar coverers (full or partial) latest-first, even when a full-height brush
'fully covers'. This is what makes mold09-over-brick split correctly regardless of a spanning brush.
Verified offline on the real id180 config: straddle z18-22 -> uniform=False, per-vert brick/brick/mold09;
inside-band triangles stay uniform (mold09 / brick) on the fast path.

Validated offline against the real id180 wall: a triangle straddling z=20 splits into brick(below) +
mold09(above); one straddling z=20 AND z=24 splits into brick/mold09/brick - both area-conserving and
disjoint (no double-texture / z-fight). The non-straddling path is unchanged (emit() fan of one
triangle == old behaviour). UE mesh append is the same proven append_buffers_to_mesh path; only that
final step is engine-untested.
