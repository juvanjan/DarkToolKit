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
