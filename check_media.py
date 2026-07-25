# check_media.py  ---  "what medium is at this point, and WHICH BRUSH made it that?"
#
# Answers the recurring question "why is there still water here?" without a UE build. It replays the
# Dark media ops over a point exactly as DromEd does (media_op[], editor/ged_csg.cpp:235) and prints
# every brush that touched it, so the brush actually responsible is named rather than guessed.
#
# A media op only affects the world state AT ITS OWN POINT IN THE ORDER. An evaporate early in a
# mission does not keep a room dry: any fill water / flood / solid->water that runs LATER refills it.
# That is the usual answer, and this tool shows it as the last WATER-setting row in the chain.
#
# USAGE (coordinates are UE feet by default - what the geo stores, /30.48):
#   py check_media.py test_missions/MISS1_mod_geo.json 0 0 36
#   py check_media.py test_missions/MISS1_mod_geo.json 0 0 36 --cm      # coords in UE cm
#   py check_media.py test_missions/MISS1_mod_geo.json --brush 182      # scan a brush's whole volume
#
# NOTE the Y flip: the geo is written in UE space, where Dark's Y is NEGATED (F_REFLECT). A DromEd
# coordinate (x, y, z) is (x, -y, z) here.

import json, sys, os

SOLID, AIR, WATER = 0, 1, 2
NAME = {SOLID: "SOLID", AIR: "AIR", WATER: "WATER"}
OPNAME = {0: "fill solid", 1: "fill air", 2: "fill water", 3: "flood", 4: "evaporate",
          5: "solid->water", 6: "solid->air", 7: "air->solid", 8: "water->solid", 9: "blockable"}
# media_op[] collapsed onto the three base media (the _PERSIST variants only control whether a later
# brush may override, which does not change shape).
T = {0: (SOLID, SOLID, SOLID),   1: (AIR, AIR, AIR),       2: (WATER, WATER, WATER),
     3: (SOLID, WATER, WATER),   4: (SOLID, AIR, AIR),     5: (WATER, AIR, WATER),
     6: (AIR, AIR, WATER),       7: (SOLID, SOLID, WATER), 8: (SOLID, AIR, SOLID),
     9: (SOLID, AIR, WATER)}
F = 30.48


def load(path):
    g = json.load(open(path))
    B = sorted(g["brushes"], key=lambda x: x["time"])
    world = next((b for b in B if b.get("world")), B[0])
    body = [b for b in B if b is not world]
    return world, body


def bbox(b):
    V = b["verts"]
    return [[min(v[i] for v in V) for i in range(3)],
            [max(v[i] for v in V) for i in range(3)]]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _dot(a, b):
    return sum(a[i]*b[i] for i in range(3))


def planes(b):
    """Outward-facing face planes of the convex brush, from its own triangles (centroid = inside)."""
    V = b["verts"]; Tr = b["tris"]
    n = float(len(V)); C = [sum(v[i] for v in V)/n for i in range(3)]
    out = []; seen = set()
    for t in Tr:
        p0, p1, p2 = V[t[0]], V[t[1]], V[t[2]]
        e1 = [p1[k]-p0[k] for k in range(3)]; e2 = [p2[k]-p0[k] for k in range(3)]
        cx = _cross(e1, e2); L = sum(x*x for x in cx) ** 0.5
        if L < 1e-9:
            continue
        nr = [cx[k]/L for k in range(3)]; d = _dot(nr, p0)
        if _dot(nr, C) - d > 0:
            nr = [-x for x in nr]; d = -d
        k = (round(nr[0], 4), round(nr[1], 4), round(nr[2], 4), round(d, 2))
        if k in seen:
            continue
        seen.add(k); out.append((nr, d))
    return out


# The brush is CONVEX, so "P is inside" is dot(n,P) <= d for every outward face plane. A bbox test is
# NOT enough: cylinders, wedges and rotated boxes fill only part of their bounding box, and a bbox
# hit on one of those would count a flood/evaporate that never actually reached the point (this is
# exactly what made an air point read as evaporated water). Match the builder's _brush_solid.
_PLANE_CACHE = {}


def inside(P, b, eps=0.05):
    box = bbox(b)
    for i in range(3):
        if P[i] < box[0][i]-eps or P[i] > box[1][i]+eps:
            return False
    key = id(b)
    pl = _PLANE_CACHE.get(key)
    if pl is None:
        pl = planes(b); _PLANE_CACHE[key] = pl
    return all(_dot(nr, P) <= d+eps for nr, d in pl)


def chain(P, world, body):
    """Final medium at P plus every brush that changed it, in order."""
    m = SOLID if inside(P, world) else AIR
    hits = []
    for b in body:
        if b["op"] not in T:
            continue
        if inside(P, b):
            new = T[b["op"]][m]
            hits.append((b, m, new))
            m = new
    return m, hits


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return
    geo = a[0]
    if not os.path.isfile(geo):
        print("! no such geo:", geo)
        return
    world, body = load(geo)
    rest = a[1:]
    cm = "--cm" in rest
    rest = [x for x in rest if x != "--cm"]

    if rest and rest[0] == "--brush":
        bid = int(rest[1])
        tgt = next((b for b in body if b.get("id") == bid), None)
        if tgt is None:
            print("! no brush id", bid)
            return
        box = bbox(tgt)
        mn = [box[0][i] / F for i in range(3)]
        mx = [box[1][i] / F for i in range(3)]
        print("brush id%s  op=%d %s  time=%s" % (bid, tgt["op"], OPNAME.get(tgt["op"], "?"), tgt["time"]))
        print("  bounds (ft): x[%g,%g] y[%g,%g] z[%g,%g]" % (mn[0], mx[0], mn[1], mx[1], mn[2], mx[2]))
        N = 24
        counts = {SOLID: 0, AIR: 0, WATER: 0}
        at_step = {SOLID: 0, AIR: 0, WATER: 0}
        culprits = {}
        for i in range(N):
            for j in range(N):
                for k in range(max(4, N // 2)):
                    P = [box[0][d] + (box[1][d] - box[0][d]) *
                         ((( i, j, k)[d] + 0.5) / (N, N, max(4, N // 2))[d]) for d in range(3)]
                    m, hits = chain(P, world, body)
                    counts[m] += 1
                    upto = [h for h in hits if h[0]["time"] <= tgt["time"]]
                    at_step[upto[-1][2] if upto else AIR] += 1
                    if m == WATER:
                        last = next((h[0] for h in reversed(hits) if h[2] == WATER), None)
                        if last is not None:
                            key = (last["id"], last["op"], last["time"])
                            culprits[key] = culprits.get(key, 0) + 1
        tot = sum(counts.values())
        print("  medium right after THIS brush runs : %s" %
              ", ".join("%s %d" % (NAME[k], v) for k, v in sorted(at_step.items()) if v))
        print("  medium in the FINAL level          : %s" %
              ", ".join("%s %d" % (NAME[k], v) for k, v in sorted(counts.items()) if v))
        if counts[WATER]:
            print("  water inside it comes from (brush that last set WATER):")
            for (i2, op, t), n in sorted(culprits.items(), key=lambda x: -x[1]):
                later = "LATER" if t > tgt["time"] else "earlier"
                print("     id%-6s %-13s time=%-6s %s   %d/%d points" %
                      (i2, OPNAME.get(op, "?"), t, later, n, tot))
        return

    if len(rest) < 3:
        print("give x y z (UE feet, or --cm), or --brush <id>")
        return
    P = [float(rest[0]), float(rest[1]), float(rest[2])]
    if not cm:
        P = [v * F for v in P]
    m, hits = chain(P, world, body)
    print("point (%g, %g, %g) ft  ->  %s" % (P[0] / F, P[1] / F, P[2] / F, NAME[m]))
    print("start: %s (%s the world block)" %
          (NAME[SOLID] if inside(P, world) else NAME[AIR],
           "inside" if inside(P, world) else "outside"))
    if not hits:
        print("  no brush covers this point")
        return
    for b, before, after in hits:
        mark = "  <-- changed" if before != after else ""
        print("  t=%-6s id%-6s %-13s %-6s -> %-6s%s" %
              (b["time"], b["id"], OPNAME.get(b["op"], "?"), NAME[before], NAME[after], mark))


if __name__ == "__main__":
    main()
