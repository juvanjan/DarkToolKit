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
#     --mis   a .mis file, or a folder of them - only textures these reference are extracted
#     --out   where the PNGs + textures_manifest.json are written  (default: ./textures)
#     --all   ignore --mis and extract EVERY texture found in the archives
#
# Example (single mission, default out folder):
#   py extract_textures.py --game "C:/Games/Thief2" --mis "C:/.../057a33b2-06.mis"

import os, sys, zipfile, struct, json, argparse

TEX_EXTS = (".pcx", ".dds", ".png", ".gif", ".tga", ".bmp")

# ---------------------------------------------------------------- read texture names from a .MIS (TXLIST)
def read_txlist_names(path):
    """Return the list of texture names referenced by a mission, from its TXLIST chunk."""
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
        p = 12 + nfam*16; names = []
        for _ in range(ntex):
            nm = d[p+4:p+20].split(b"\x00")[0].decode("latin1"); p += 20
            if nm and nm.lower() != "null": names.append(nm)
        return names
    except Exception as e:
        print("  ! could not read TXLIST from %s (%s)" % (os.path.basename(path), e))
        return []

def gather_needed(mis_arg):
    files = []
    if os.path.isdir(mis_arg):
        for root, _, fs in os.walk(mis_arg):
            for fn in fs:
                if fn.lower().endswith(".mis"): files.append(os.path.join(root, fn))
    elif os.path.isfile(mis_arg):
        files = [mis_arg]
    needed = {}   # lower name -> display name
    for mf in files:
        for nm in read_txlist_names(mf):
            needed.setdefault(nm.lower(), nm)
    print("Missions scanned: %d   textures referenced: %d" % (len(files), len(needed)))
    return needed

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
    index = {}
    def add(key, entry):
        index.setdefault(key, entry)          # first (highest-priority) wins
    for lp in loose:
        stem = os.path.splitext(os.path.basename(lp))[0].lower()
        add(stem, ("loose", lp, None))
    for cp in crfs:
        try:
            zf = zipfile.ZipFile(cp)
        except Exception as e:
            print("  ! cannot open archive %s (%s)" % (cp, e)); continue
        for member in zf.namelist():
            if member.endswith("/"): continue
            b = member.replace("\\", "/").split("/")[-1]
            if not b.lower().endswith(TEX_EXTS): continue
            stem = os.path.splitext(b)[0].lower()
            add(stem, ("crf", cp, member))
    print("Archives: %d   loose files: %d   unique texture stems: %d" % (len(crfs), len(loose), len(index)))
    return index

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
    ap.add_argument("--game", required=True, help="Thief 2 folder, or a direct path to fam.crf")
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

    if a.all or not a.mis:
        needed = {k: k for k in index.keys()}
        if not a.mis: print("(no --mis given; extracting ALL textures found)")
    else:
        needed = gather_needed(a.mis)
        if not needed: print("No textures referenced (check --mis path)."); sys.exit(1)

    os.makedirs(a.out, exist_ok=True)
    manifest = {}; missing = []; ok = 0
    for key, disp in sorted(needed.items()):
        entry = index.get(key)
        if not entry:
            missing.append(disp); continue
        kind, container, member = entry
        try:
            img = load_image(kind, container, member)
            img = img.convert("RGBA") if ("transparency" in img.info or img.mode in ("RGBA","LA","P")) else img.convert("RGB")
            outname = disp + ".png"
            img.save(os.path.join(a.out, outname))
            manifest[disp] = {"png": outname,
                              "source": os.path.basename(container) + (("::"+member) if member else ""),
                              "size": list(img.size)}
            ok += 1
        except Exception as e:
            print("  ! failed to convert %s (%s)" % (disp, e)); missing.append(disp)

    with open(os.path.join(a.out, "textures_manifest.json"), "w") as fh:
        json.dump({"count": ok, "textures": manifest}, fh, indent=1)

    print("\nConverted %d textures -> %s" % (ok, a.out))
    print("Manifest: %s" % os.path.join(a.out, "textures_manifest.json"))
    if missing:
        print("MISSING (%d) - not found in the archives: %s" % (len(missing), ", ".join(missing)))
        print("  These may live in a family folder outside fam.crf, or be custom fan-mission textures.")

if __name__ == "__main__":
    main()