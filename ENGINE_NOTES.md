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

1. **Pyramid + cornerpyramid primitives** — `PrimShape_CreateNGonPyr` (`primshap.c:210`).
2. **Face-aligned primal variants** — half-facet phase + `1/cos(π/n)` radius (§2b).
3. **Dodecahedron** (`PRIMAL_DODEC_IDX`) — 1 brush in MISS1, ignorable.
4. **Ops 6 / 7** in `KEEP_OPS`.
5. **Mode A/B dispatch on `tx_rot == 1`** rather than on a geometric guess (§3).

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
