# extract_textures.py  ---  pull Thief 2 textures out of fam.crf and convert them to PNG for UE.
#
# A .crf archive is just a ZIP. Inside are family folders (core, fort, ...), each holding texture
# files, usually .PCX (with mip levels) but sometimes .GIF / .DDS / .PNG / .TGA. The .MIS files only
# store texture *names* (see the TXLIST chunk), so this tool reads those names from your missions,
# finds each one inside the archive(s) regardless of which family folder it lives in, converts it to
# PNG, and writes a manifest mapping name -> png. UE imports the PNGs; build_in_ue.py uses the manifest.
#
# REQUIRES: Pillow.   pip install pillow --break-system-packages
#
# USAGE (run on the machine that has your Thief 2 install):
#   py extract_textures.py --game "C:/Games/Thief2" --mis "C:/.../missions" --out "C:/.../textures"
#     --game  Thief 2 folder (searched for *.crf and any loose fam/ folders), OR a direct path to fam.crf
#     --mis   a .mis file, OR A FOLDER OF THEM (recursed) - the union of every texture they reference
#             is extracted once into one shared --out folder; the manifest records which missions use each.
#     --out   where the PNGs + textures_manifest.json are written  (default: ./textures)
#     --all   ignore --mis and extract EVERY texture found in the archives
#
# Whole folder of missions (typical):
#   py extract_textures.py --game "C:/Games/Thief2" --mis "C:/.../test_missions" --out textures
# Single mission:
#   py extract_textures.py --game "C:/Games/Thief2" --mis "C:/.../057a33b2-06.mis"

import os, sys, zipfile, struct, json, argparse, math

TEX_EXTS = (".pcx", ".dds", ".png", ".gif", ".tga", ".bmp")


_MTL_CACHE = {}
def _mtl_params(game_root):
    """stem -> {param: [values]} for every .mtl under game_root (NewDark doc/material-format.txt)."""
    if not _MTL_CACHE:
        for root, _, files in os.walk(game_root):
            for f in files:
                if not f.lower().endswith(".mtl"): continue
                d = {}
                try:
                    for line in open(os.path.join(root, f), "r", errors="ignore"):
                        line = line.split(";")[0].strip()
                        parts = line.split()
                        if len(parts) >= 2: d[parts[0].lower()] = parts[1:]
                        elif parts:        d[parts[0].lower()] = []
                except Exception: pass
                if d: _MTL_CACHE[os.path.splitext(f)[0].lower()] = d
        print("  read %d .mtl files" % len(_MTL_CACHE))
        if not _MTL_CACHE: _MTL_CACHE["__none__"] = {}
    return _MTL_CACHE

def _terrain_scale(container, member, game_root):
    """Logical texture size from the HD pack's sibling .mtl ("terrain_scale N"), or None.

    NewDark texture packs declare the size Dark scales by; it cannot be inferred from the image
    dimensions (bloxwall and BIGBL2 are both 64x64 in fam.crf yet declare 64 and 128)."""
    p = _mtl_params(game_root).get(
        os.path.splitext(os.path.basename(member or container))[0].lower())
    if not p or "terrain_scale" not in p: return None
    try: return int(p["terrain_scale"][0])
    except Exception: return None

# ---------------------------------------------------------------- animated textures
# Dark animates a texture by loading sibling files named <base>_1, <base>_2, ... alongside the base
# (render/anim_txt.c:56 ectsAnimTxtIgnore parses exactly that `_<digits>` suffix). The BASE file is
# frame 0 and the _N files follow, so `ani_frames 20` means base + _1.._19.
#
# From doc/material-format.txt and anim_txt.c:
#   ani_rate   = MILLISECONDS PER FRAME (not fps), default 250 (DEF_RATE)
#   ani_frames = frame count; 0 = however many files exist
#   ani_mode   = WRAP (default, DEF_FLAG) | REVERSE | PINGPONG
# WRAP advances cur 0..cnt-1 then snaps back to 0 (ectsAnimHitEdge), i.e. a plain forward loop.
def _anim_info(stem, index, game_root):
    """(frame_entries, rate_ms, mode) for an animated texture, or None if it isn't animated."""
    frames = []
    i = 1
    while True:
        c = index.get("%s_%d" % (stem, i))
        if not c: break
        frames.append(c[0])
        i += 1
    if not frames: return None
    p = _mtl_params(game_root).get(stem.lower(), {})
    try: rate = int(p.get("ani_rate", [250])[0])
    except Exception: rate = 250
    try: want = int(p.get("ani_frames", [0])[0])
    except Exception: want = 0
    mode = " ".join(p.get("ani_mode", ["WRAP"])).upper() or "WRAP"
    if want > 0: frames = frames[:max(0, want - 1)]        # -1: the base file is frame 0
    return frames, rate, mode

# ---------------------------------------------------------------- read texture names from a .MIS (TXLIST)
def read_txlist(path):
    """Return [(name, family)] referenced by a mission. family = the folder DromEd resolves it from.
    Record byte 1 is the family index, 1-based into the family-name list (0 = no family / null)."""
    try:
        f = open(path, "rb").read()
        toc = struct.unpack_from("<I", f, 0)[0]
        cnt = struct.unpack_from("<I", f, toc)[0]
        p = toc + 4; chunks = {}
        for _ in range(cnt):
            nm = f[p:p+12].split(b"\x00")[0].decode("latin1")
            off, ln = struct.unpack_from("<II", f, p+12); chunks[nm] = (off, ln); p += 20
        if "TXLIST" not in chunks: return []
        o, l = chunks["TXLIST"]; d = f[o+24:o+24+l]
        _, ntex, nfam = struct.unpack_from("<III", d, 0)
        fams = [d[12+i*16:12+i*16+16].split(b"\x00")[0].decode("latin1") for i in range(nfam)]
        p = 12 + nfam*16; out = []
        for _ in range(ntex):
            fi = d[p+1]; nm = d[p+4:p+20].split(b"\x00")[0].decode("latin1"); p += 20
            if nm and nm.lower() != "null":
                fam = fams[fi-1] if 1 <= fi <= nfam else ""     # 1-based family index
                out.append((nm, fam))
        return out
    except Exception as e:
        print("  ! could not read TXLIST from %s (%s)" % (os.path.basename(path), e))
        return []

def read_txlist_names(path):
    return [nm for nm, _ in read_txlist(path)]

def read_water_prefix(path):
    """The mission's water family prefix, from the FAMILY chunk (render/family.c:900).

    family_name_block is [sky_name, water_name, <MAX_FAMILIES family names>], each FAM_NAME_LEN=24
    bytes. The engine then loads `<prefix>in` and `<prefix>out` out of fam\\waterhw\\ into the two
    RESERVED texture slots WATERIN_IDX=247 / WATEROUT_IDX=248 (family.c:406 family_load_water). Water
    surfaces are never textured from a brush face, so this is the only place the choice is recorded.
    Layout: 24-byte chunk header, then size_per(4), cnt(4), then cnt entries. NewDark raised
    MAX_FAMILIES to 32, so cnt is 34 rather than the 18 the shipped source implies."""
    try:
        f = open(path, "rb").read()
        toc = struct.unpack_from("<I", f, 0)[0]
        cnt = struct.unpack_from("<I", f, toc)[0]
        p = toc + 4; chunks = {}
        for _ in range(cnt):
            nm = f[p:p+12].split(b"\x00")[0].decode("latin1")
            off, ln = struct.unpack_from("<II", f, p+12); chunks[nm] = (off, ln); p += 20
        if "FAMILY" not in chunks: return ""
        o, _l = chunks["FAMILY"]
        per, n = struct.unpack_from("<II", f, o+24)
        if per <= 0 or n < 2: return ""
        base = o + 32
        return f[base+per:base+2*per].split(b"\x00")[0].decode("latin1")   # entry 1 = water
    except Exception as e:
        print("  ! could not read FAMILY from %s (%s)" % (os.path.basename(path), e))
        return ""

def gather_needed(mis_arg):
    files = []
    if os.path.isdir(mis_arg):
        for root, _, fs in os.walk(mis_arg):                 # recurses into subfolders
            for fn in fs:
                if fn.lower().endswith(".mis"): files.append(os.path.join(root, fn))
        files.sort()
    elif os.path.isfile(mis_arg):
        files = [mis_arg]
    else:
        print("! --mis path not found:", mis_arg); return {}, {}
    needed = {}      # lower name -> display name
    usage  = {}      # lower name -> [mission files that use it]
    family = {}      # lower name -> family folder (from TXLIST)
    for mf in files:
        entries = read_txlist(mf)
        # Water surfaces carry NO brush-face texture - the engine substitutes the reserved slots
        # 247/248 from the mission's water family - so they never appear in TXLIST and would be
        # missed entirely. Pull `<prefix>in`/`<prefix>out` from waterhw explicitly.
        wp = read_water_prefix(mf)
        if wp:
            for suf in ("in", "out"):
                entries = entries + [(wp + suf, "waterhw")]
        for nm, fam in entries:
            k = nm.lower()
            needed.setdefault(k, nm)
            usage.setdefault(k, []).append(os.path.basename(mf))
            if fam and k not in family: family[k] = fam
        print("  %-38s %3d textures" % (os.path.basename(mf), len(entries)))
    print("Missions scanned: %d   unique textures referenced: %d" % (len(files), len(needed)))
    return needed, usage, family

# ---------------------------------------------------------------- index available texture files
def index_sources(game):
    """Build lower-basename -> (kind, container, member) for every texture in the .crf archives and
    any loose fam/ folders under `game`. fam*.crf sources rank first so families win over obj/mesh."""
    crfs = []; loose = []
    if os.path.isfile(game) and game.lower().endswith((".crf", ".zip")):
        crfs = [game]
    elif os.path.isdir(game):
        for root, dirs, fs in os.walk(game):
            for fn in fs:
                if fn.lower().endswith((".crf", ".zip")): crfs.append(os.path.join(root, fn))
            # loose texture folders (fan missions / extracted installs)
            base = os.path.basename(root).lower()
            if base in ("fam", "families") or os.path.basename(os.path.dirname(root)).lower() == "fam":
                for fn in fs:
                    if fn.lower().endswith(TEX_EXTS): loose.append(os.path.join(root, fn))
    else:
        print("! --game path not found:", game); return {}
    # fam archives first
    crfs.sort(key=lambda p: (0 if "fam" in os.path.basename(p).lower() else 1, p))
    index = {}   # stem -> [ (kind, container, member, folder) ]  folder = family folder name
    def add(key, entry):
        index.setdefault(key, []).append(entry)
    for lp in loose:
        stem = os.path.splitext(os.path.basename(lp))[0].lower()
        add(stem, ("loose", lp, None, os.path.basename(os.path.dirname(lp))))
    for cp in crfs:
        try:
            zf = zipfile.ZipFile(cp)
        except Exception as e:
            print("  ! cannot open archive %s (%s)" % (cp, e)); continue
        for member in zf.namelist():
            if member.endswith("/"): continue
            parts = member.replace("\\", "/").split("/")
            b = parts[-1]
            if not b.lower().endswith(TEX_EXTS): continue
            stem = os.path.splitext(b)[0].lower()
            folder = parts[-2] if len(parts) >= 2 else ""
            add(stem, ("crf", cp, member, folder))
    print("Archives: %d   loose files: %d   unique texture stems: %d" % (len(crfs), len(loose), len(index)))
    return index

def pick_candidate(cands, fam):
    """Choose the file matching the texture's family folder (as DromEd does); else first available."""
    if not cands: return None, False
    if fam:
        for c in cands:
            if c[3] and c[3].lower() == fam.lower(): return c, True
    return cands[0], (not fam)   # exact=False means we fell back (possible mismatch)

# ---------------------------------------------------------------- convert one texture to PNG
def load_image(kind, container, member):
    from PIL import Image
    import io
    if kind == "loose":
        return Image.open(container)
    zf = zipfile.ZipFile(container)
    raw = zf.read(member)
    return Image.open(io.BytesIO(raw))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=r"C:/Nex/DarkSimProject/Thief 2",
                    help="Thief 2 folder, or a direct path to fam.crf")
    ap.add_argument("--mis",  default=None,  help=".mis file or folder of them (which textures to pull)")
    ap.add_argument("--out",  default="textures")
    ap.add_argument("--all",  action="store_true", help="extract every texture found, ignore --mis")
    a = ap.parse_args()

    try:
        import PIL  # noqa
    except Exception:
        print("Pillow is required:  pip install pillow --break-system-packages"); sys.exit(1)

    index = index_sources(a.game)
    if not index: sys.exit(1)

    usage = {}; family = {}
    if a.all or not a.mis:
        needed = {k: k for k in index.keys()}
        if not a.mis: print("(no --mis given; extracting ALL textures found)")
    else:
        needed, usage, family = gather_needed(a.mis)
        if not needed: print("No textures referenced (check --mis path)."); sys.exit(1)

    os.makedirs(a.out, exist_ok=True)
    manifest = {}; missing = []; ok = 0; fallbacks = []; overrides = []; animated = []
    for key, disp in sorted(needed.items()):
        cands = index.get(key)
        if not cands:
            missing.append(disp); continue
        fam = family.get(key, "")
        entry, exact = pick_candidate(cands, fam)
        kind, container, member, folder = entry
        if fam and not exact:
            others = sorted(set(c[3] for c in cands if c[3]))
            fallbacks.append("%s (wants family '%s', found in %s)" % (disp, fam, others or "unknown folder"))
        try:
            img = load_image(kind, container, member)
            img = img.convert("RGBA") if ("transparency" in img.info or img.mode in ("RGBA","LA","P")) else img.convert("RGB")
            outname = disp + ".png"
            img.save(os.path.join(a.out, outname))
            # Dark scales a texture by its ORIGINAL size, even when a loose/HD file overrides it for
            # display. Record that as scale_px so the tile formula (px*2^(scale-20)) matches DromEd.
            #
            # THE AUTHORITY IS terrain_scale. NewDark HD texture packs ship a sibling .mtl next to each
            # replacement image declaring the logical size Dark should scale by:
            #     MODS/NTEX/FAM/Core_1/BIGBL2.mtl  ->  "terrain_scale 128"
            # This is NOT derivable from the file sizes. bloxwall and BIGBL2 are both 64x64 in fam.crf,
            # both in Core_1, both 512/256px as DDS - yet terrain_scale says 64 and 128 respectively,
            # and DromEd tiles them 4ft and 8ft accordingly. Measured against DromEd on MISS5_mod2:
            # id4618 Rustgir3 12 repeats / 48ft = 4ft (terrain_scale 64), id42 bigbl2 4 / 32ft = 8ft
            # (128), id98 wdplnk 1.25 / 10ft = 8ft (128). All six tested textures match their .mtl.
            #
            # Fall back to the fam.crf original only when no .mtl exists (unmodded installs).
            scale_px = img.size[0]
            ts = _terrain_scale(container, member, a.game)
            if ts:
                scale_px = ts
            elif kind != "crf":
                crf_c = next((c for c in cands if c[0] == "crf"                       # same-family fam.crf copy
                              and (not fam or (c[3] and c[3].lower() == fam.lower()))), None)
                crf_c = crf_c or next((c for c in cands if c[0] == "crf"), None)       # any fam.crf copy
                if crf_c:
                    try: scale_px = load_image(crf_c[0], crf_c[1], crf_c[2]).size[0]
                    except Exception: pass
            if scale_px != img.size[0]:
                overrides.append("%s: display %dpx, scale %dpx (%s)"
                                 % (disp, img.size[0], scale_px, "terrain_scale" if ts else "fam.crf original"))
            manifest[disp] = {"png": outname, "family": fam, "folder": folder,
                              "source": os.path.basename(container) + (("::"+member) if member else ""),
                              "family_matched": bool(fam and exact),
                              "size": list(img.size),
                              "scale_px": scale_px,
                              "used_by": sorted(set(usage.get(key, [])))}
            # Animated? Lay the frames out as a sprite-sheet atlas; UE gets one texture plus a
            # flipbook material rather than 20 assets. Frame 0 is the BASE image (anim_txt.c).
            ai = _anim_info(key, index, a.game)
            if ai:
                extra, rate, mode = ai
                try:
                    imgs = [img.convert("RGBA")]
                    for e in extra:
                        fi = load_image(e[0], e[1], e[2]).convert("RGBA")
                        if fi.size != img.size: fi = fi.resize(img.size)
                        imgs.append(fi)
                    n = len(imgs)
                    cols = int(math.ceil(math.sqrt(n)))
                    rows = int(math.ceil(n / float(cols)))
                    fw, fh = img.size
                    from PIL import Image as _Im
                    sheet = _Im.new("RGBA", (cols*fw, rows*fh), (0, 0, 0, 0))
                    for i2, fr in enumerate(imgs):
                        sheet.paste(fr, ((i2 % cols)*fw, (i2 // cols)*fh))
                    aname = disp + "_anim.png"
                    sheet.save(os.path.join(a.out, aname))
                    manifest[disp]["anim"] = {"atlas": aname, "frames": n, "cols": cols, "rows": rows,
                                              "rate_ms": rate, "mode": mode,
                                              "loop_s": round(n*rate/1000.0, 4),
                                              "frame_size": [fw, fh]}
                    animated.append("%s: %d frames, %dms/frame (%.2fs loop), %s, atlas %dx%d"
                                    % (disp, n, rate, n*rate/1000.0, mode, cols, rows))
                except Exception as e:
                    print("  ! could not build animation atlas for %s (%s)" % (disp, e))
            ok += 1
        except Exception as e:
            print("  ! failed to convert %s (%s)" % (disp, e)); missing.append(disp)

    with open(os.path.join(a.out, "textures_manifest.json"), "w") as fh:
        json.dump({"count": ok, "textures": manifest}, fh, indent=1)

    print("\nConverted %d textures -> %s" % (ok, a.out))
    print("Manifest: %s" % os.path.join(a.out, "textures_manifest.json"))
    if overrides:
        print("SCALE OVERRIDE (%d) - HD/loose file used for display, but Dark scale taken from fam.crf size:" % len(overrides))
        for ov in overrides: print("  " + ov)
    if animated:
        print("ANIMATED (%d) - frames packed into a sprite-sheet atlas for a UE flipbook:" % len(animated))
        for an in animated: print("  " + an)
    if fallbacks:
        print("FAMILY FALLBACK (%d) - the mission's family folder wasn't found, used another (may look wrong):" % len(fallbacks))
        for fb in fallbacks: print("  " + fb)
    if missing:
        print("MISSING (%d) - not found in the archives: %s" % (len(missing), ", ".join(missing)))
        print("  These may live in a family folder outside fam.crf, or be custom fan-mission textures.")

if __name__ == "__main__":
    main()