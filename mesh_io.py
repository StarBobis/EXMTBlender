"""Import / export EXMT `.exmtmesh` files inside Blender.

The format is the same numpy archive the Tauri toolbox writes, so a round trip
is lossless for the attributes the game actually reads:

    positions, normals, tangents, colors, uv0, weights, bones, indices,
    submesh ranges, bindpose, bone hashes, channel layout (in `meta`).
"""

from __future__ import annotations

import json

import bpy
import numpy as np
from mathutils import Matrix, Vector

FORMAT = "exmt-mesh-1"

# Attributes that carry the game's own tangents across an edit session (Blender
# has no tangent accessor of its own; recomputing them changes normal-mapped
# shading even when the mesh was not touched).
TANGENT_ATTR = "exmt_tangent"
TANGENT_SIGN_ATTR = "exmt_tangent_w"

# Layout used by GF2's character meshes; applied when exporting a mesh that was
# not imported from the game (so the core can pack it back).
DEFAULT_CHANNELS = [
    {"stream": 0, "offset": 0, "format": 0, "dimension": 3},   # position
    {"stream": 0, "offset": 12, "format": 0, "dimension": 3},  # normal
    {"stream": 0, "offset": 24, "format": 0, "dimension": 4},  # tangent
    {"stream": 1, "offset": 0, "format": 0, "dimension": 4},   # color
    {"stream": 1, "offset": 16, "format": 1, "dimension": 2},  # uv0
] + [{"stream": 0, "offset": 0, "format": 0, "dimension": 0} for _ in range(7)] + [
    {"stream": 2, "offset": 0, "format": 0, "dimension": 4},   # blend weights
    {"stream": 2, "offset": 16, "format": 10, "dimension": 4}, # blend indices
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _channel_index(channels, name_map):
    for idx, ch in enumerate(channels):
        if ch.get("dimension", 0) and idx in name_map:
            yield idx, name_map[idx]


def _matrix_from_dict(m):
    return Matrix([[m["e%d%d" % (r, c)] for c in range(4)] for r in range(4)])


def hash_bone_name(value) -> str:
    """Same naming rule as the core: bones are identified by their CRC32 hash."""
    return "bone_%08X" % (int(value) & 0xFFFFFFFF)


def _bone_view(data, meta) -> tuple:
    """(bones, local) for a file that has no merged skeleton view.

    Every `.exmtmesh` the app exports carries the skin skeleton; one written by
    this add-on carries its own table in `bone_hashes`/`bindpose` instead.  Both
    end up naming bones identically, which is what makes the groups bind.
    """
    skel = meta.get("skeleton") or {}
    bones = skel.get("bones") or []
    if bones:
        local = list(skel.get("local") or range(len(bones)))
        return bones, local

    hashes = None
    bind = None
    if "bone_hashes" in data:
        hashes = [int(h) for h in np.asarray(data["bone_hashes"], dtype=np.uint32)]
        if "bindpose" in data:
            bind = np.asarray(data["bindpose"], dtype=np.float64)
    elif (meta.get("tree") or {}).get("m_BoneNameHashes"):
        tree = meta["tree"]
        hashes = [int(h) for h in tree["m_BoneNameHashes"]]
        pose = tree.get("m_BindPose") or []
        if pose:
            bind = np.array([[[m["e%d%d" % (r, c)] for c in range(4)] for r in range(4)]
                             for m in pose], dtype=np.float64)
    if not hashes:
        return [], []

    out = []
    for i, h in enumerate(hashes):
        rest = np.linalg.inv(bind[i]) if bind is not None and i < len(bind) else np.eye(4)
        out.append({"hash": int(h), "name": hash_bone_name(h), "parent": -1,
                    "rest": [float(v) for v in np.asarray(rest).reshape(-1)]})
    return out, list(range(len(out)))


def build_armature(name: str, bones: list, scale: float = 1.0):
    """One armature holding every bone of the skeleton, parents included."""
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    bpy.context.collection.objects.link(obj)
    for other in bpy.context.selected_objects:
        other.select_set(False)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    made = []
    identity = np.eye(4, dtype=np.float64)
    for bone in bones:
        try:
            matrix = np.asarray(bone.get("rest"), dtype=np.float64).reshape(4, 4)
        except Exception:
            # no usable rest transform: park the bone at the origin.  The rest
            # matrix is what the deformation is measured against, so a flat rig
            # still drives the mesh correctly by group name (measured on
            # Sally_outfit: 126 groups, all resolved).
            matrix = identity
        m = Matrix([[float(matrix[r][c]) for c in range(4)] for r in range(4)])
        eb = arm.edit_bones.new(bone["name"])
        head = m.translation * scale
        eb.head = head
        direction = m.to_3x3() @ Vector((0.0, 1.0, 0.0))
        if direction.length < 1e-6:
            direction = Vector((0.0, 1.0, 0.0))
        eb.tail = head + direction.normalized() * 0.04 * scale
        try:
            eb.align_roll(m.to_3x3() @ Vector((0.0, 0.0, 1.0)))
        except Exception:
            eb.roll = 0.0
        made.append(eb)
    for i, bone in enumerate(bones):
        parent = bone.get("parent", -1)
        if not isinstance(parent, int):
            # a plain name -> hash table (bones of a game mesh) carries no parent
            # indices; parse the flat `parents` list when one was handed over
            parents = bone.get("parents")
            parent = int(parents[i]) if isinstance(parents, (list, tuple)) and i < len(parents) else -1
        if isinstance(parent, int) and 0 <= parent < len(made) and parent != i:
            made[i].parent = made[parent]
            made[i].use_connect = False
    # a bone points at its first child, so chains read like a rig instead of dots
    for i, bone in enumerate(bones):
        kids = [made[j] for j, other in enumerate(bones)
                if other.get("parent", -1) == i and j != i]
        if not kids:
            continue
        head = made[i].head
        nearest = min(kids, key=lambda k: (k.head - head).length)
        delta = nearest.head - head
        if delta.length > max(1e-4, 0.005 * scale):
            made[i].tail = nearest.head
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.show_in_front = True
    arm.display_type = "OCTAHEDRAL"
    return obj


def _apply_weights(obj, local, used_bones, bones, weights, bone_count):
    """Weights -> vertex groups whose names equal the bone names.

    Groups are created for every bone this mesh may legally use (the game's
    per-mesh bone table), so hand-painting never lands on a bone the pack step
    would have to drop.
    """
    groups = {}
    for idx in range(bone_count):
        gi = local[idx] if idx < len(local) else idx
        name = bones[gi]["name"] if gi < len(bones) else hash_bone_name(idx)
        groups[idx] = obj.vertex_groups.new(name=name)
    for vi in range(used_bones.shape[0]):
        for k in range(used_bones.shape[1]):
            w = float(weights[vi, k])
            if w <= 0.0:
                continue
            group = groups.get(int(used_bones[vi, k]))
            if group is not None:
                group.add([vi], w, "REPLACE")


def _stored_tangents(mesh, nverts: int):
    """`(nverts, 4)` tangents kept on the mesh at import time, or None."""
    vec = mesh.attributes.get(TANGENT_ATTR)
    if vec is None or len(vec.data) != nverts:
        return None
    out = np.zeros((nverts, 4), dtype=np.float32)
    # foreach_get needs a contiguous buffer: writing straight into out[:, :3]
    # would fill a temporary copy and leave zeros behind
    xyz = np.empty((nverts, 3), dtype=np.float32)
    vec.data.foreach_get("vector", xyz.reshape(-1))
    out[:, :3] = xyz
    sign = mesh.attributes.get(TANGENT_SIGN_ATTR)
    if sign is not None and len(sign.data) == nverts:
        w = np.empty(nverts, dtype=np.float32)
        sign.data.foreach_get("value", w)
        out[:, 3] = w
    else:
        out[:, 3] = 1.0
    return out


def import_mesh(filepath: str, scale: float = 1.0, armature=None):
    """Import one `.exmtmesh`.

    `armature` reuses an existing rig (the workspace operator builds one complete
    skeleton for the whole skin); when omitted the file's own skeleton is built.
    """
    data = np.load(filepath, allow_pickle=False)
    meta = json.loads(str(data["meta"]))
    pos = np.asarray(data["positions"], dtype=np.float32) * scale
    idx = np.asarray(data["indices"], dtype=np.int64).reshape(-1, 3)

    mesh = bpy.data.meshes.new(meta.get("name") or "exmt_mesh")
    mesh.from_pydata(pos.tolist(), [], idx.tolist())
    mesh.validate(verbose=False)
    mesh.update()

    obj = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.collection.objects.link(obj)

    # UVs
    if "uv0" in data:
        uv = np.asarray(data["uv0"], dtype=np.float32)
        layer = mesh.uv_layers.new(name="UVMap")
        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                layer.data[li].uv = uv[vi].tolist()

    # colours -> vertex colour layer (float colour, linear)
    if "colors" in data:
        col = np.asarray(data["colors"], dtype=np.float32)
        attr = mesh.color_attributes.new(name="Color", type="FLOAT_COLOR", domain="POINT")
        for vi in range(len(col)):
            attr.data[vi].color = col[vi].tolist()

    # The game's own normals/tangents: without this Blender recomputes both, which
    # visibly changes the shading (measured: median 2° on normals, 11° on tangents)
    # even when the user changed nothing. Custom split normals make Blender *show*
    # the game's shading too; the tangents ride along as plain attributes.
    normals = data["normals"] if "normals" in data else None
    if normals is not None and len(normals) == len(mesh.vertices):
        try:
            mesh.normals_split_custom_set_from_vertices(
                np.asarray(normals, dtype=np.float32).tolist())
        except Exception:
            pass
    tangents = data["tangents"] if "tangents" in data else None
    if tangents is not None and len(tangents) == len(mesh.vertices):
        t = np.asarray(tangents, dtype=np.float32)
        vec = mesh.attributes.new(name=TANGENT_ATTR, type="FLOAT_VECTOR", domain="POINT")
        vec.data.foreach_set("vector", np.ascontiguousarray(t[:, :3]).reshape(-1))
        sign = mesh.attributes.new(name=TANGENT_SIGN_ATTR, type="FLOAT", domain="POINT")
        sign.data.foreach_set("value", np.ascontiguousarray(t[:, 3]))

    # materials (one slot per submesh; names are placeholders, the game keeps its own)
    nsub = max(1, len(meta.get("submeshes") or [1]))
    for i in range(nsub):
        mat = bpy.data.materials.new("exmt_slot_%d" % i)
        mat.use_nodes = True
        mesh.materials.append(mat)
    first = np.asarray(data["submesh_first"], dtype=np.int64) if "submesh_first" in data else np.zeros(1)
    counts = np.asarray(data["submesh_count"], dtype=np.int64) if "submesh_count" in data else np.array([len(idx)])
    start = 0
    width = 3
    for si, (f, c) in enumerate(zip(first, counts)):
        poly_first = start
        poly_last = start + int(c) // width
        for p in range(poly_first, min(poly_last, len(mesh.polygons))):
            mesh.polygons[p].material_index = min(si, nsub - 1)
        start = poly_last

    # skinning
    bones, local = _bone_view(data, meta)
    if armature is None and "bindpose" in data and "bones" in data and "weights" in data and bones:
        armature = build_armature(mesh.name + "_arm", bones, scale=scale)
    if armature is not None and "bones" in data and "weights" in data and bones:
        _apply_weights(obj, local, np.asarray(data["bones"]), bones,
                       np.asarray(data["weights"]), len(local))
        obj.parent = armature
        for mod in list(obj.modifiers):
            if mod.type == "ARMATURE":
                obj.modifiers.remove(mod)
        mod = obj.modifiers.new("Armature", "ARMATURE")
        mod.object = armature
        mod.use_vertex_groups = True

    obj["exmt_meta"] = json.dumps(meta, ensure_ascii=False)
    # the export needs to map group names back to this mesh's own bone indices, and
    # the rest transforms make an exported file self-contained
    obj["exmt_bones"] = json.dumps([
        {"name": (bones[local[i]]["name"] if i < len(local) and local[i] < len(bones)
                  else hash_bone_name(i)),
         "hash": (int(bones[local[i]]["hash"]) if i < len(local) and local[i] < len(bones) else 0),
         "rest": (bones[local[i]].get("rest") if i < len(local) and local[i] < len(bones) else None)}
        for i in range(len(local))
    ], ensure_ascii=False)
    # carry the game's full typetree (bone hashes, bind poses, submesh descriptors,
    # collision meshes …) so the export can hand every field back unchanged
    if meta.get("tree"):
        obj["exmt_tree"] = json.dumps(meta["tree"], ensure_ascii=False)
    obj["exmt_source"] = filepath
    return obj, armature, meta


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def _own_table(obj) -> list:
    raw = obj.get("exmt_bones")
    if not raw:
        return []
    try:
        table = json.loads(raw) or []
    except Exception:
        return []
    return [b for b in table if isinstance(b, dict) and b.get("name")]


def _scene_names() -> dict:
    """`name -> hash` from every imported EXMT mesh in the scene.

    A garment imported next to the workspace meshes has no table of its own;
    its vertex groups keep the names the weights were transferred under, and
    those names are enough to recover the hash the game identifies bones by.
    """
    out = {}
    for obj in bpy.data.objects:
        for item in _own_table(obj):
            out.setdefault(str(item["name"]), int(item.get("hash") or 0))
    return out


def _scene_bone_table() -> list:
    """The richest bone table any imported EXMT mesh in the scene carries.

    Every imported mesh keeps the game table of its own slot, so the workspace
    meshes together describe the whole rig.  A foreign mesh sitting next to them
    inherits it and its groups bind by name — the same rule `_bone_slots` uses
    with an explicit `bone_table`, only discovered from the scene.
    """
    best = []
    for obj in bpy.data.objects:
        table = _own_table(obj)
        if len(table) > len(best):
            best = table
    return best


def reuse_rig(obj, bone_table: list, report=None):
    """Point the object's Armature modifier at a rig that really holds these bones.

    Export does not read the modifier — it resolves group names through the bone
    table — so this changes nothing in the Mod.  It exists so the viewport tells
    the truth: a forecast mesh whose modifier points at an empty rig refuses to
    move when its bones are posed, which reads as "not bound" to anyone checking
    the file (measured 2026-10-19 on `Sally_outfit`, whose rig held 0 bones).
    """
    names = {str(b.get("name")) for b in bone_table if isinstance(b, dict) and b.get("name")}
    if not names:
        return None

    for mod in obj.modifiers:
        if mod.type != "ARMATURE" or not mod.object:
            continue
        rig_bones = getattr(mod.object.data, "bones", None)
        if rig_bones and names <= set(rig_bones.keys()):
            return mod.object

    candidates = []
    for candidate in bpy.data.objects:
        if candidate.type != "ARMATURE" or candidate == obj:
            continue
        rig_bones = getattr(candidate.data, "bones", None)
        if not rig_bones:
            continue
        keys = set(rig_bones.keys())
        if names <= keys:
            candidates.append(candidate)
    candidates.sort(key=lambda c: -len(c.data.bones))

    if candidates:
        rig = candidates[0]
        if report:
            report({"INFO"}, "绑定到场景里的骨架 %s（%d 根骨骼）" % (rig.name, len(rig.data.bones)))
    else:
        rig = build_armature((obj.name or "mesh") + "_arm", bone_table, scale=1.0)
        if report:
            report({"INFO"}, "新建骨架并绑定 %d 根骨骼" % len(bone_table))

    found = False
    for mod in obj.modifiers:
        if mod.type == "ARMATURE":
            mod.object = rig
            mod.use_vertex_groups = True
            found = True
    if not found:
        mod = obj.modifiers.new("Armature", "ARMATURE")
        mod.object = rig
        mod.use_vertex_groups = True
    obj.parent = rig
    return rig


def _hash_from_name(name: str):
    if name.startswith("bone_"):
        try:
            return int(name[5:], 16)
        except ValueError:
            return None
    return None


def _bone_slots(obj, bone_table=None) -> tuple:
    """(slot table, `resolve(group name) -> slot or None`).

    Without `bone_table` the object's own table defines the slots, which makes an
    untouched mesh round-trip exactly.  With it (an external garment about to be
    packed into a game mesh slot) the target's table defines the slots and names
    are resolved back to their CRC32 hash first — names travel, indices do not.
    """
    own = _own_table(obj)
    target = [b for b in (bone_table or []) if isinstance(b, dict) and b.get("name")] or own
    if not target:
        # A foreign mesh (MMD/VRM/another game) has no table of its own; it must
        # borrow the slot table of the game mesh it replaces.  Without one every
        # group name resolves to None and *every influence is dropped*, which
        # silently writes a mesh that is not skinned at all — the game then draws
        # nothing because no bone can drive a single vertex.
        # Measured 2026-10-19: `Sally_outfit` (Sally PMX over Vepley) exported
        # with weights all 0.0, `bone_hashes` empty and `bindpose` absent; the
        # bone slot collapsed to the origin and the outfit never appeared.
        target = [b for b in _scene_bone_table() if isinstance(b, dict) and b.get("name")]
        if not target:
            return [], lambda name: None

    by_name = {}
    for i, item in enumerate(target):
        by_name[str(item["name"])] = i
    by_hash = {}
    for i, item in enumerate(target):
        if item.get("hash") is not None:
            by_hash.setdefault(int(item["hash"]), i)
    aliases = {}
    for item in own:
        aliases.setdefault(str(item["name"]), int(item.get("hash") or 0))
    if bone_table:
        for name, hash_ in _scene_names().items():
            aliases.setdefault(name, hash_)

    def resolve(name: str):
        slot = by_name.get(name)
        if slot is not None:
            return slot
        hash_ = aliases.get(name)
        if hash_ is None:
            hash_ = _hash_from_name(name)
        if hash_ is not None and hash_ in by_hash:
            return by_hash[hash_]
        if name.startswith("b"):
            try:
                return int(name[1:])
            except ValueError:
                return None
        return None

    return target, resolve


def export_mesh(obj, filepath: str, meta_override: dict | None = None,
                bone_table: list | None = None, material_slots: int | None = None) -> dict:
    deps = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(deps)
    mesh = eval_obj.to_mesh()
    mesh.calc_loop_triangles()

    matrix = obj.matrix_world.copy()
    norm_matrix = matrix.to_3x3().inverted().transposed()

    meta = {}
    raw = obj.get("exmt_meta")
    if raw:
        try:
            meta = json.loads(raw)
        except Exception:
            meta = {}
    if meta_override:
        meta.update(meta_override)
    meta["format"] = FORMAT
    meta["name"] = meta.get("name") or mesh.name

    nverts = len(mesh.vertices)
    positions = np.empty((nverts, 3), dtype=np.float32)
    for i, v in enumerate(mesh.vertices):
        positions[i] = (matrix @ v.co)[:]

    # normals: the custom split normals we set on import (i.e. the game's own),
    # taken per corner so a split edge keeps both sides
    normals = np.empty((nverts, 3), dtype=np.float32)
    seen = np.zeros(nverts, dtype=bool)
    corners = getattr(mesh, "corner_normals", None)
    if corners is not None and len(corners) == len(mesh.loops):
        for loop in mesh.loops:
            vi = loop.vertex_index
            if not seen[vi]:
                normals[vi] = (norm_matrix @ Vector(corners[loop.index].vector))[:]
                seen[vi] = True
    for i in range(nverts):
        if not seen[i]:
            normals[i] = (norm_matrix @ mesh.vertices[i].normal)[:]
    norms = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, norms, out=normals, where=norms > 1e-12)

    # UVs / loop data -> per vertex (first occurrence wins)
    uv = np.zeros((nverts, 2), dtype=np.float32)
    if mesh.uv_layers:
        layer = mesh.uv_layers.active
        seen = np.zeros(nverts, dtype=bool)
        for loop in mesh.loops:
            vi = loop.vertex_index
            if not seen[vi]:
                uv[vi] = layer.data[loop.index].uv[:]
                seen[vi] = True

    # tangents: the stored ones win (an untouched mesh must keep the game's exact
    # tangents); only a mesh whose vertices changed falls back to computing them
    tangents = np.zeros((nverts, 4), dtype=np.float32)
    tangents[:, 0] = 1.0
    tangents[:, 3] = 1.0
    stored = _stored_tangents(mesh, nverts)
    if stored is not None:
        tangents[:, :3] = (np.asarray(matrix.to_3x3(), dtype=np.float64) @ stored[:, :3].T).T
        tangents[:, 3] = stored[:, 3]
    else:
        try:
            mesh.calc_tangents()
            seen = np.zeros(nverts, dtype=bool)
            for loop in mesh.loops:
                vi = loop.vertex_index
                if not seen[vi]:
                    t = loop.tangent
                    tangents[vi, 0:3] = (matrix.to_3x3() @ Vector(t))[:]
                    tangents[vi, 3] = 1.0 if loop.bitangent_sign >= 0 else -1.0
                    seen[vi] = True
        except Exception:
            pass

    # colours
    colors = np.ones((nverts, 4), dtype=np.float32)
    if mesh.color_attributes:
        ca = mesh.color_attributes.active_color
        if ca is not None:
            for i in range(nverts):
                colors[i] = ca.data[i].color[:]

    # weights -> the influence slots the mesh itself declares (GF2 uses 2 or 4),
    # bone index resolved from the group name
    channels = meta.get("channels") or []

    def _dim(slot, default):
        try:
            value = int(channels[slot].get("dimension") or 0)
        except Exception:
            value = 0
        return value or default

    slots = max(1, min(_dim(12, 4), _dim(13, 4), 4))
    bones = np.zeros((nverts, slots), dtype=np.uint32)
    weights = np.zeros((nverts, slots), dtype=np.float32)
    bone_slots, resolve = _bone_slots(obj, bone_table)
    bone_count = len(bone_slots)
    if not bone_count:
        tree_hashes = (meta.get("tree") or {}).get("m_BoneNameHashes") or []
        bone_count = len(tree_hashes)
    group_names = {vg.index: vg.name for vg in obj.vertex_groups}
    dropped = 0
    influenced = 0
    for i, v in enumerate(mesh.vertices):
        infl = []
        for g in v.groups:
            if g.weight <= 0.0:
                continue
            name = group_names.get(g.group, "")
            bi = resolve(name)
            if bi is None or (bone_count and bi >= bone_count):
                dropped += 1
                continue
            infl.append((g.weight, bi))
        infl.sort(key=lambda item: (-item[0], item[1]))
        if len(infl) > slots:
            dropped += len(infl) - slots
            infl = infl[:slots]
        if infl:
            influenced += 1
        total = sum(w for w, _ in infl)
        if abs(total - 1.0) < 1e-6:
            # already normalised: keep the exact float32 values, so an untouched
            # mesh round-trips bit for bit instead of drifting by one ULP
            for k, (w, bi) in enumerate(infl):
                weights[i, k] = w
                bones[i, k] = bi
        else:
            total = total or 1.0
            for k, (w, bi) in enumerate(infl):
                weights[i, k] = w / total
                bones[i, k] = bi

    # indices + submeshes by material slot.  The exporter numbers submesh N with
    # material index N, and Unity only draws a submesh whose material index
    # resolves against the *renderer's* material list — which belongs to the game
    # mesh being replaced.  A foreign model can carry far more slots (measured:
    # Sally's PMX has 20 in use, GF2's cloth has 4), and every dropped submesh is
    # a hole in game while Blender still shows the whole thing.
    tris = np.array([t.vertices[:] for t in mesh.loop_triangles], dtype=np.uint32)
    mat_ids = np.array([t.material_index for t in mesh.loop_triangles], dtype=np.int32)
    nfaces = len(tris)

    limit = int(material_slots or 0)
    used = sorted((int(s) for s in np.unique(mat_ids)), key=lambda s: -int((mat_ids == s).sum()))
    if limit and len(used) > limit:
        # merge the least-used groups into subgroups of roughly equal face count,
        # so the result still spans every material the game will give us
        totals = [(int((mat_ids == s).sum()), s) for s in used]
        target = max(1, sum(n for n, _ in totals) // limit)
        groups, cur, acc = [], [], 0
        for n, s in totals:
            cur.append(s)
            acc += n
            if acc >= target and len(groups) < limit - 1:
                groups.append(cur)
                cur, acc = [], 0
        if cur:
            groups.append(cur)
        remap, notes = {}, []
        for new_index, group in enumerate(groups):
            for s in group:
                remap[s] = new_index
            notes.append("%s(%d)" % (", ".join(
                (obj.material_slots[s].material.name if s < len(obj.material_slots)
                 and obj.material_slots[s].material else str(s)) for s in group),
                int(sum((mat_ids == s).sum() for s in group))))
        mat_ids = np.array([remap.get(int(s), 0) for s in mat_ids], dtype=np.int32)
        meta["material_merge"] = {"limit": limit, "from": len(used), "to": len(groups), "groups": notes}

    order = []
    first = []
    counts = []
    for slot in range(max(1, len(mesh.materials))):
        sel = np.where(mat_ids == slot)[0]
        if len(sel) == 0:
            continue
        first.append(len(order) * 3)
        counts.append(len(sel) * 3)
        order.extend(sel.tolist())
    if not order:
        order = list(range(len(tris)))
        first = [0]
        counts = [len(tris) * 3]
    indices = tris[np.array(order, dtype=np.int64)].reshape(-1).astype(np.uint32)

    meta["index_format"] = 0 if nverts < 65536 else 1
    if not meta.get("channels"):
        meta["channels"] = DEFAULT_CHANNELS
    meta["has_bindpose"] = bool(meta.get("has_bindpose"))
    raw_tree = obj.get("exmt_tree")
    if raw_tree and not meta.get("tree"):
        try:
            meta["tree"] = json.loads(raw_tree)
        except Exception:
            pass
    meta["submeshes"] = [{"firstByte": int(first[i]) * (2 if meta["index_format"] == 0 else 4),
                          "indexCount": int(counts[i])} for i in range(len(counts))]
    meta["vertex_count"] = int(nverts)

    arrays = dict(
        positions=positions,
        normals=normals,
        tangents=tangents,
        colors=colors,
        uv0=uv,
        weights=weights,
        bones=bones,
        indices=indices,
        submesh_first=np.array(first, dtype=np.uint32),
        submesh_count=np.array(counts, dtype=np.uint32),
    )
    # carry this mesh's own bone table so the file stays self-contained (importing
    # an exported file builds the rig again; packing still prefers the game's tree)
    if bone_slots and any(b.get("hash") is not None for b in bone_slots):
        arrays["bone_hashes"] = np.asarray(
            [int(b.get("hash") or 0) for b in bone_slots], dtype=np.uint32)
        rests = []
        complete = True
        for b in bone_slots:
            rest = b.get("rest")
            if not rest or len(rest) != 16:
                complete = False
                break
            rests.append(np.asarray(rest, dtype=np.float64).reshape(4, 4))
        if complete:
            arrays["bindpose"] = np.linalg.inv(np.asarray(rests, dtype=np.float64)).astype(np.float32)
    if "bindpose" in meta and meta.get("has_bindpose"):
        pass
    arrays["meta"] = np.array(json.dumps(meta, ensure_ascii=False))
    with open(filepath, "wb") as fh:
        np.savez_compressed(fh, **arrays)

    eval_obj.to_mesh_clear()
    return {"verts": int(nverts), "tris": int(len(tris)), "file": filepath,
            "dropped_influences": int(dropped), "bones": int(bone_count),
            "influenced": int(influenced), "groups": len(obj.vertex_groups),
            "submeshes": len(counts), "material_merge": meta.get("material_merge")}
