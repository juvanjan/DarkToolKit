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

import os, sys, zipfile, struct, json, argparse

TEX_EXTS = (".pcx", ".dds", ".png", ".gif", ".tga", ".bmp")

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
    manifest = {}; missing = []; ok = 0; fallbacks = []
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
            manifest[disp] = {"png": outname, "family": fam, "folder": folder,
                              "source": os.path.basename(container) + (("::"+member) if member else ""),
                              "family_matched": bool(fam and exact),
                              "size": list(img.size),
                              "used_by": sorted(set(usage.get(key, [])))}
            ok += 1
        except Exception as e:
            print("  ! failed to convert %s (%s)" % (disp, e)); missing.append(disp)

    with open(os.path.join(a.out, "textures_manifest.json"), "w") as fh:
        json.dump({"count": ok, "textures": manifest}, fh, indent=1)

    print("\nConverted %d textures -> %s" % (ok, a.out))
    print("Manifest: %s" % os.path.join(a.out, "textures_manifest.json"))
    if fallbacks:
        print("FAMILY FALLBACK (%d) - the mission's family folder wasn't found, used another (may look wrong):" % len(fallbacks))
        for fb in fallbacks: print("  " + fb)
    if missing:
        print("MISSING (%d) - not found in the archives: %s" % (len(missing), ", ".join(missing)))
        print("  These may live in a family folder outside fam.crf, or be custom fan-mission textures.")

if __name__ == "__main__":
    main()