bl_info = {
    "name": "EXMT Modbox Bridge",
    "author": "EXMT",
    "version": (0, 7, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > EXMT",
    "description": "Import the workspace selected in EXMT Modbox, edit or replace meshes and textures in a blueprint, then pack them back into bundles",
    "category": "Import-Export",
}

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Node, NodeSocket, NodeTree, Operator, Panel
from bpy.utils import register_class, unregister_class
from mathutils import Matrix, Quaternion, Vector

try:
    from . import blueprint
    from . import mesh_io
except ImportError:  # loaded as a plain module (headless helpers)
    import blueprint  # type: ignore
    import mesh_io  # type: ignore

DEFAULT_CORE = r"C:\Dev\EXMT\core"
DEFAULT_PYTHON = r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
DEFAULT_WORKSPACE = r"C:\EXMTCacheFolder\workspace"
_APPDATA = os.environ.get("APPDATA", "")
DEFAULT_CONFIG = (
    os.path.join(_APPDATA, "com.exmt.modbox", "EXMTConfig.json") if _APPDATA else ""
)


def _prefs():
    try:
        return bpy.context.preferences.addons[__name__.split(".")[0]].preferences
    except Exception:
        return None


def _core_dir() -> str:
    p = _prefs()
    return p.core_dir if p and p.core_dir else DEFAULT_CORE


def _python_exe() -> str:
    p = _prefs()
    return p.python_exe if p and p.python_exe else DEFAULT_PYTHON


def run_core(args: list, timeout: int = 300) -> dict:
    """Run `python -m exmt_core ...` with the configured interpreter."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _core_dir() + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [_python_exe(), "-m", "exmt_core", *args],
        cwd=_core_dir(), env=env, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[-2000:])
    return {"stdout": proc.stdout, "stderr": proc.stderr}


def target_bone_table(bundle: str, mesh_name: str) -> list:
    """Bones of the game mesh an external object is about to replace.

    The app writes the path of its bone-name index into `EXMTConfig.json`; with
    it the table carries the same names the workspace used, so an external mesh
    whose weights came from a transfer can be matched by name.
    """
    index = ""
    cfg, _path = _read_config()
    if isinstance(cfg, dict):
        index = str(cfg.get("bone_names") or "")
    args = ["bonetable", bundle, mesh_name]
    if index and os.path.isfile(index):
        args.append(index)
    try:
        res = run_core(args, timeout=120)
        payload = json.loads(res["stdout"].strip().splitlines()[-1])
        return payload.get("bones") or []
    except Exception:
        return []


def target_material_slots(bundle: str, mesh_name: str) -> int:
    """How many materials the game mesh carries — i.e. how many submeshes survive.

    The renderer's material list belongs to the mesh being replaced, so a foreign
    model's slot 5..19 hit nothing and Unity skips those submeshes entirely: the
    parts are invisible in game while Blender still renders them (measured
    2026-10-19: Sally's 20-slot PMX over GF2's 4-slot cloth lost 6803 of 14568
    faces — both forearms and both hands).
    """
    try:
        res = run_core(["matinfo", bundle, mesh_name], timeout=120)
        return int(json.loads(res["stdout"].strip().splitlines()[-1]).get("submeshes") or 0)
    except Exception:
        return 0


def _report_export(operator, info: dict) -> None:
    msg = "导出 %d 顶点 / %d 三角面" % (info["verts"], info["tris"])
    dropped = int(info.get("dropped_influences") or 0)
    if dropped:
        msg += "（%d 个权重不属于目标骨骼，已忽略）" % dropped
        operator.report({"WARNING"}, msg)
    else:
        operator.report({"INFO"}, msg)


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #
class EXMT_OT_import_mesh(Operator):
    bl_idname = "exmt.import_mesh"
    bl_label = "导入 .exmtmesh"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    scale: FloatProperty(name="缩放", default=1.0, min=0.001, max=100.0)

    def execute(self, context):
        obj, arm, meta = mesh_io.import_mesh(self.filepath, scale=self.scale)
        self.report({"INFO"}, "导入 %s（%d 顶点）" % (obj.name, len(obj.data.vertices)))
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class EXMT_OT_export_mesh(Operator):
    bl_idname = "exmt.export_mesh"
    bl_label = "导出 .exmtmesh"
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")
    only_selected: BoolProperty(name="仅选中物体", default=True)

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "先选中一个网格物体")
            return {"CANCELLED"}
        info = mesh_io.export_mesh(obj, self.filepath)
        _report_export(self, info)
        return {"FINISHED"}

    def invoke(self, context, event):
        obj = context.active_object
        if obj and obj.name:
            self.filepath = os.path.join(bpy.path.abspath("//"), obj.name + ".exmtmesh")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class EXMT_OT_import_gltf(Operator):
    bl_idname = "exmt.import_gltf"
    bl_label = "导入 glTF（含骨架）"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        bpy.ops.import_scene.gltf(filepath=self.filepath)
        self.report({"INFO"}, "已导入 glTF：%s" % os.path.basename(self.filepath))
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class EXMT_OT_transfer_weights(Operator):
    bl_idname = "exmt.transfer_weights"
    bl_label = "传递权重并绑定骨架"
    bl_options = {"REGISTER", "UNDO"}

    source: StringProperty(name="权重来源网格", description="已绑定骨架的原网格（如 c_VepleySSR01_slg_cloth_lod0）")
    parent_to_armature: BoolProperty(name="绑定到骨架", default=True)

    def execute(self, context):
        dst = context.active_object
        if dst is None or dst.type != "MESH":
            self.report({"ERROR"}, "先选中要绑定的网格")
            return {"CANCELLED"}
        src = bpy.data.objects.get(self.source)
        if src is None or src.type != "MESH":
            self.report({"ERROR"}, "找不到权重来源网格：%s" % self.source)
            return {"CANCELLED"}

        # 1) copy weights by nearest-face interpolation
        #    DataTransfer only writes into *existing* vertex groups, so mirror the
        #    source group names onto the destination first.
        for vg in src.vertex_groups:
            if vg.name not in dst.vertex_groups:
                dst.vertex_groups.new(name=vg.name)
        mod = dst.modifiers.new("exmt_weight_transfer", "DATA_TRANSFER")
        mod.object = src
        mod.use_vert_data = True
        mod.data_types_verts = {"VGROUP_WEIGHTS"}
        mod.vert_mapping = "POLYINTERP_NEAREST"
        mod.layers_vgroup_select_src = "ALL"
        bpy.context.view_layer.objects.active = dst
        bpy.ops.object.modifier_apply(modifier=mod.name)

        # 2) bind to the armature that drives the source mesh
        if self.parent_to_armature:
            arm = src.parent if src.parent and src.parent.type == "ARMATURE" else None
            if arm is None:
                for m in src.modifiers:
                    if m.type == "ARMATURE" and m.object:
                        arm = m.object
                        break
            if arm is not None:
                dst.parent = arm
                if not any(m.type == "ARMATURE" for m in dst.modifiers):
                    amod = dst.modifiers.new("Armature", "ARMATURE")
                    amod.object = arm
            else:
                self.report({"WARNING"}, "来源网格没有骨架，只传递了权重")
        self.report({"INFO"}, "已从 %s 传递权重（%d 个顶点组）" % (src.name, len(dst.vertex_groups)))
        return {"FINISHED"}

    def invoke(self, context, event):
        ctx = context.copy()
        # default the source to another selected mesh
        for o in context.selected_objects:
            if o.type == "MESH" and o is not context.active_object:
                self.source = o.name
                break
        return context.window_manager.invoke_props_dialog(self)


class EXMT_OT_extract_bundle(Operator):
    bl_idname = "exmt.extract_bundle"
    bl_label = "从 Bundle 提取并导入"
    bl_options = {"REGISTER", "UNDO"}

    bundle: StringProperty(name="Bundle", subtype="FILE_PATH")
    mesh_name: StringProperty(name="Mesh 名称")
    import_after: BoolProperty(name="导入到场景", default=True)

    def execute(self, context):
        out = os.path.join(bpy.app.tempdir, "exmt_extract.exmtmesh")
        run_core(["extract", self.bundle, self.mesh_name, out])
        if self.import_after:
            mesh_io.import_mesh(out)
        self.report({"INFO"}, "已提取 %s" % self.mesh_name)
        return {"FINISHED"}


class EXMT_OT_pack_bundle(Operator):
    bl_idname = "exmt.pack_bundle"
    bl_label = "打包回 Bundle"
    bl_options = {"REGISTER"}

    bundle: StringProperty(name="源 Bundle", subtype="FILE_PATH")
    mesh_name: StringProperty(name="Mesh 名称")
    mesh_file: StringProperty(name=".exmtmesh", subtype="FILE_PATH")
    output: StringProperty(name="输出 Bundle", subtype="FILE_PATH")

    def execute(self, context):
        res = run_core(["pack", self.bundle, self.mesh_name, self.mesh_file, self.output])
        self.report({"INFO"}, res["stdout"].strip().splitlines()[-1])
        return {"FINISHED"}


class EXMT_OT_export_and_pack(Operator):
    bl_idname = "exmt.export_and_pack"
    bl_label = "一键导出并打包"
    bl_options = {"REGISTER"}

    bundle: StringProperty(name="源 Bundle", subtype="FILE_PATH")
    mesh_name: StringProperty(name="Mesh 名称")
    output: StringProperty(name="输出 Bundle", subtype="FILE_PATH")

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "先选中一个网格物体")
            return {"CANCELLED"}
        tmp = os.path.join(bpy.app.tempdir, "exmt_pack.exmtmesh")
        # a foreign mesh has no bone table of its own: weights are matched by bone
        # name against the game mesh it replaces
        table = None if obj.get("exmt_bones") else target_bone_table(self.bundle, self.mesh_name)
        info = mesh_io.export_mesh(obj, tmp, {"name": self.mesh_name}, bone_table=table)
        run_core(["pack", self.bundle, self.mesh_name, tmp, self.output])
        msg = "已打包 %s（%d 顶点）" % (os.path.basename(self.output), info["verts"])
        dropped = int(info.get("dropped_influences") or 0)
        if dropped:
            self.report({"WARNING"}, msg + "；%d 个权重不属于该网格的骨骼，已忽略" % dropped)
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


# --------------------------------------------------------------------------- #
# workspace (what the app's Models page writes)
# --------------------------------------------------------------------------- #
def _workspace_dir() -> str:
    p = _prefs()
    return p.workspace_dir if p and p.workspace_dir else DEFAULT_WORKSPACE


def _config_candidates() -> list:
    """Where the app may have written EXMTConfig.json, in priority order."""
    out = []
    p = _prefs()
    if p and p.config_path:
        out.append(p.config_path)
    if _APPDATA:
        out.append(os.path.join(_APPDATA, "com.exmt.modbox", "EXMTConfig.json"))
        # Tauri's appDataDir() may already include the identifier; cover both.
        out.append(os.path.join(_APPDATA, "EXMTConfig.json"))
    if DEFAULT_CONFIG:
        out.append(DEFAULT_CONFIG)
    seen = set()
    unique = []
    for path in out:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _read_config() -> tuple:
    """(config, path) — path is the first actual config file, or ''."""
    for path in _config_candidates():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), path
        except Exception:
            return {}, path
    return {}, ""


def _active_workspace_info() -> tuple:
    """(workspace, source).

    EXMTConfig.json wins when it exists, even if workspace is null: that means
    the app deliberately has no active workspace. Only when no config file
    exists at all do we fall back to the old `<workspace_dir>/current.json`.
    """
    cfg, path = _read_config()
    if path:
        ws = cfg.get("workspace") if isinstance(cfg, dict) else None
        return (ws if isinstance(ws, dict) else None), path
    current = os.path.join(_workspace_dir(), "current.json")
    if os.path.isfile(current):
        try:
            with open(current, encoding="utf-8") as fh:
                return json.load(fh), current
        except Exception:
            return None, current
    return None, ""


def _part_of(mesh_name: str) -> str:
    """c_X_slg_cloth_lod0 -> cloth, so textures can be matched by their name."""
    m = re.match(r"^c_.*?_slg_(.+?)_lod\d+", mesh_name or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# submeshes: the app assigns the index, the blueprint decides which object fills it
# --------------------------------------------------------------------------- #
def _manifest(folder: str) -> dict:
    return blueprint.manifest(folder)


def submeshes(folder: str) -> list:
    return blueprint.submeshes(folder)


def submesh_items(folder: str) -> list:
    return blueprint.submesh_items(folder)


def _image(path: str, non_color: bool = False):
    img = bpy.data.images.load(path, check_existing=True)
    try:
        img.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
    except Exception:
        pass
    return img


def _textured_material(name: str, tex_paths: list, part: str):
    """Principled material from the workspace PNGs (base colour + normal map)."""

    def pick(suffixes):
        for path in tex_paths:
            stem = os.path.splitext(os.path.basename(path))[0]
            if part and ("_%s" % part) not in stem:
                continue
            if any(stem.endswith(s) for s in suffixes):
                return path
        return None

    base = pick(("_d", "_d1", "_da"))
    normal = pick(("_n",))
    if base is None and normal is None:
        return None

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        return mat

    if base:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = _image(base)
        tex.label = os.path.basename(base)
        tex.location = (-420, 220)
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    if normal:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = _image(normal, non_color=True)
        tex.label = os.path.basename(normal)
        tex.location = (-420, -160)
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (-180, -160)
        links.new(tex.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


# --------------------------------------------------------------------------- #
# animations: the workspace's reference clips -> one Action each
# --------------------------------------------------------------------------- #
def _m4(values: list) -> Matrix:
    """`skeleton.json` / clip data store a 4x4 row-major (same as mesh_io)."""
    return Matrix([[float(values[r * 4 + c]) for c in range(4)] for r in range(4)])


def _order_bones(parent: list) -> list:
    """Parents before children, cycle-safe."""
    order, state = [], {}

    def visit(index: int, depth: int = 0) -> None:
        if state.get(index) == 2 or depth > len(parent) + 2:
            return
        if state.get(index) == 1:          # cycle: treat as root, stop recursing
            return
        state[index] = 1
        p = parent[index] if 0 <= index < len(parent) else -1
        if 0 <= p < len(parent) and p != index:
            visit(p, depth + 1)
        state[index] = 2
        order.append(index)

    for i in range(len(parent)):
        visit(i)
    return order


def _animation_actions(folder: str, rig, bones: list, label: str) -> tuple:
    """Turn `animations/*.exmtanim` into Actions on `rig`; `(count, names)`.

    The clip stores **local** TRS per bone plus the hash of its parent — the
    hierarchy is nowhere in the clip itself, the core reads it from the character
    prefab in the same bundle.  A pose is composed the way Unity does it,
    `W(bone) = W(parent) · L(bone)`, with non-animated bones contributing their
    rest local.  Blender wants a pose *basis*, which is
    `(W(parent) · parent_rest⁻¹ · bone_rest)⁻¹ · W(bone)`; that is exact even where
    this workspace's rig has a different (partly inferred) parent chain.
    """
    manifest = _manifest(folder)
    rows = manifest.get("animations") or []
    if not rig or not bones or not rows:
        return 0, []
    by_hash = {int(b.get("hash") or 0): i for i, b in enumerate(bones)}
    rest = [
        _m4(b.get("rest") or (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))
        for b in bones
    ]
    bl_bones = [rig.data.bones.get(b.get("name") or "") for b in bones]
    # index of each bone's *Blender* parent, and Blender's own rest matrix.
    #
    # The rig this addon builds is only an *approximation* of the game's bind
    # pose: `build_armature` derives each bone from head + Y + roll, and measured
    # on a real workspace 18 of 32 bones end up 10–179° off in roll.  Skinning
    # uses `bone.matrix_local` as the rest reference, so a pose computed against
    # the *game's* rest twists the mesh.  Fix: never trust matrix_local as the
    # bind — convert the desired **deform** (game bone world · game rest⁻¹) into
    # a pose matrix through matrix_local, which cancels any rest mismatch.
    bl_parent = []
    for bone in bl_bones:
        parent = bone.parent if bone is not None else None
        bl_parent.append(next((j for j, other in enumerate(bl_bones) if other == parent), -1)
                         if parent is not None else -1)
    bl_local = [(bone.matrix_local.copy() if bone is not None else rest[i])
                for i, bone in enumerate(bl_bones)]

    here = os.path.abspath(folder)
    # a re-import replaces this workspace's actions instead of piling up `.001`
    for old in list(bpy.data.actions):
        if old.get("exmt_workspace") == here:
            bpy.data.actions.remove(old)

    made = []
    skipped = []      # (clip, "N/M") — this rig cannot drive it at all
    partial = []      # (clip, matched, animated) — it will look incomplete
    for row in rows:
        rel = row.get("file") or ""
        path = os.path.join(folder, rel)
        if not rel or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        times = data.get("times") or []
        steps = len(times)
        if steps < 2:
            continue
        tracks = {}
        for track in data.get("bones") or []:
            index = by_hash.get(int(track.get("hash") or 0))
            if index is not None:
                tracks[index] = track
        animated = len(data.get("bones") or [])
        if not tracks:
            skipped.append((row.get("clip") or rel, animated))
            continue
        # 这套骨架撑不起这个动作时，别硬套：只说清"匹配了几根"，并且太差就不建 Action
        coverage = len(tracks) / max(1, animated)
        if coverage < 0.5:
            skipped.append((row.get("clip") or rel, "%d/%d" % (len(tracks), animated)))
            continue
        if coverage < 0.95:
            partial.append((row.get("clip") or rel, len(tracks), animated))
        # the clip's own hierarchy wins; fall back to the skeleton's parent index
        parent = []
        for i, bone in enumerate(bones):
            track = tracks.get(i)
            p = by_hash.get(int(track.get("parent") or 0)) if track else None
            if p is None or p == i:
                guess = bone.get("parent", -1)
                p = guess if isinstance(guess, int) else -1
            parent.append(p if isinstance(p, int) and 0 <= p < len(bones) and p != i else -1)
        order = _order_bones(parent)
        local_rest = [
            (rest[parent[i]].inverted() @ rest[i]) if parent[i] >= 0 else rest[i]
            for i in range(len(bones))
        ]

        def sample(track, key, count, step, fallback):
            rows_ = track.get(key) if track else None
            if not rows_:
                return list(fallback)
            return [float(rows_[c][step]) for c in range(count)]

        channels = {i: {"location": [], "rotation_quaternion": [], "scale": []}
                    for i in tracks}
        for step in range(steps):
            world = {}
            for i in order:
                base = local_rest[i]
                track = tracks.get(i)
                if track:
                    pos = sample(track, "position", 3, step, base.to_translation())
                    if track.get("rotation"):
                        quat = sample(track, "rotation", 4, step, base.to_quaternion())
                        rot = Quaternion((quat[3], quat[0], quat[1], quat[2])).to_matrix()
                    elif track.get("euler"):
                        # Unity stores `localEulerAnglesRaw` in degrees and applies
                        # Z, then X, then Y — the curves and the quaternions are
                        # never both present on one transform (checked: 0 of 258)
                        ex, ey, ez = (math.radians(v) for v in sample(track, "euler", 3, step, (0.0, 0.0, 0.0)))
                        rot = (Matrix.Rotation(ey, 3, "Y") @ Matrix.Rotation(ex, 3, "X")
                               @ Matrix.Rotation(ez, 3, "Z"))
                    else:
                        rot = base.to_3x3()
                    scale = sample(track, "scale", 3, step, base.to_scale())
                    local = (Matrix.Translation(Vector(pos))
                             @ rot.to_4x4()
                             @ Matrix.Diagonal(Vector(scale).to_4d()))
                else:
                    local = base
                world[i] = (world[parent[i]] @ local) if parent[i] >= 0 else local
            for i in tracks:
                bone = bl_bones[i]
                if bone is None:
                    continue
                # deform the mesh the way the game does, then express it through
                # Blender's own rest: pose = (world · game_rest⁻¹) · matrix_local
                pose_want = world[i] @ rest[i].inverted() @ bl_local[i]
                blp = bl_parent[i]
                if blp >= 0:
                    parent_pose = (world[blp] @ rest[blp].inverted() @ bl_local[blp]
                                   if blp in world else bl_local[blp])
                    base_pose = parent_pose @ bl_local[blp].inverted() @ bl_local[i]
                else:
                    base_pose = bl_local[i]
                basis = base_pose.inverted() @ pose_want
                loc, quat, scale = basis.decompose()
                row = channels[i]
                row["location"].extend((float(step + 1), loc.x, loc.y, loc.z))
                row["rotation_quaternion"].extend(
                    (float(step + 1), quat.w, quat.x, quat.y, quat.z))
                row["scale"].extend((float(step + 1), scale.x, scale.y, scale.z))

        name = row.get("clip") or os.path.splitext(os.path.basename(rel))[0]
        action = bpy.data.actions.new(name)
        action.use_fake_user = True
        # which workspace/clip it came from: re-import and overlap checks read this
        action["exmt_workspace"] = here
        action["exmt_clip"] = row.get("clip") or name
        slot = action.slots.new(id_type="OBJECT", name=name[:63])
        strip = action.layers.new("Layer").strips.new(type="KEYFRAME")
        bag = strip.channelbag(slot, ensure=True)
        widths = {"location": 3, "rotation_quaternion": 4, "scale": 3}
        for i, row in channels.items():
            bone_name = (bones[i].get("name") or "").replace('"', "")
            for key, width in widths.items():
                flat = row[key]
                if not flat:
                    continue
                for axis in range(width):
                    curve = bag.fcurves.new('pose.bones["%s"].%s' % (bone_name, key), index=axis)
                    points = [(flat[k], flat[k + 1 + axis]) for k in range(0, len(flat), width + 1)]
                    curve.keyframe_points.add(len(points))
                    co = [value for point in points for value in point]
                    curve.keyframe_points.foreach_set("co", co)
                    curve.update()
        action.use_frame_range = True
        action.frame_start = 1.0
        action.frame_end = float(steps)
        # keep them selectable in the Action editor, and push each onto its own
        # muted NLA track so switching clips is one click
        rig.animation_data_create()
        try:
            track = rig.animation_data.nla_tracks.new()
            track.name = name
            nla = track.strips.new(name, 1, action)
            try:
                nla.action_slot = slot
            except Exception:
                pass
            track.mute = True
        except Exception:
            pass
        action["exmt_bones"] = len(tracks)
        action["exmt_clip_bones"] = animated
        made.append((action, slot, steps))

    notes = {
        "skipped": skipped,
        "partial": partial,
    }
    if not made:
        return 0, [], notes
    # the first clip is live, so the user sees motion immediately
    action, slot, steps = made[0]
    rig.animation_data.action = action
    try:
        rig.animation_data.action_slot = slot
    except Exception:
        pass
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = max(steps, 2)
    scene.frame_set(1)
    return len(made), [act.name for act, _slot, _n in made], notes


def _load_skeleton(folder: str) -> list:
    """Bones of `skeleton.json` (the union of every mesh's table), or []."""
    path = os.path.join(folder, "skeleton.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("bones") or []
    except Exception:
        return []


def _forget_previous_import(folder: str, label: str, keep_rig=None) -> int:
    """Delete the meshes a previous import of this workspace created.

    Called *before* re-importing: Blender would otherwise name the new meshes
    `…_body_lod0.001` (the old datablock still owns the clean name) and the
    collection would grow by one full copy per import.  Only objects that live
    inside this blueprint's `<label>_original` frame and that nothing else uses are
    removed — a mesh the user moved out of the frame, or referenced from their own
    node, stays exactly where it is.
    """
    name = "EXMT %s" % (label or os.path.basename(folder) or "蓝图")
    tree = bpy.data.node_groups.get(name)
    if not isinstance(tree, blueprint.EXMTBlueprintTree):
        return 0
    frame_name = "%s_original" % label
    frame = next((n for n in tree.nodes
                  if n.bl_idname == "NodeFrame" and n.label == frame_name), None)
    if frame is None:
        return 0
    keep = set()
    for node in tree.nodes:
        obj = getattr(node, "object", None)
        if obj is not None and not (node.parent is not None and node.parent == frame):
            keep.add(obj.name)
    removed = 0
    for node in tree.nodes:
        if node.bl_idname != "EXMTNodeObject":
            continue
        if node.parent is None or node.parent != frame:
            continue
        obj = getattr(node, "object", None)
        if obj is None or obj.name in keep or obj.type != "MESH":
            continue
        if obj.children or obj.data is None or obj.data.users > 1:
            continue                      # shared mesh data or a parented rig: leave it
        node.object = None
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        # the *data* block keeps the name alive: without this the next import gets
        # `…_body_lod0.001` even though the object name was freed
        if data is not None and data.users == 0 and isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)
        removed += 1

    # with the meshes gone the previous armature is an empty duplicate; keep any
    # rig that still has children or that a node / the user still references
    collection = bpy.data.collections.get("EXMT · %s" % (label or "工作空间"))
    for obj in list(bpy.data.objects):
        if obj.type != "ARMATURE" or obj.children or obj.name in keep:
            continue
        if keep_rig is not None and obj == keep_rig:
            continue                      # the rig this import reuses: keep it
        if collection is not None and obj.name not in collection.objects:
            continue                      # a rig from somewhere else: not ours to delete
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0 and isinstance(data, bpy.types.Armature):
            bpy.data.armatures.remove(data)
    return removed


def _reuse_armature(collection, name: str, bones: list):
    """The rig a previous import built for this workspace, when it still fits.

    Re-importing used to build a second armature every time (`…001`), leaving an
    empty duplicate behind.  Same bone names → keep the rig the meshes (and the
    user's pose) already use.
    """
    if collection is None or not bones:
        return None
    wanted = [bone.get("name") or bone.get("hash") for bone in bones]
    for obj in collection.objects:
        if obj.type != "ARMATURE" or obj.name != name:
            continue
        have = [bone.name for bone in obj.data.bones]
        if len(have) == len(wanted) and set(have) == set(wanted):
            return obj
    return None


class EXMT_OT_import_workspace(Operator):
    bl_idname = "exmt.import_workspace"
    bl_label = "一键导入当前工作空间"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        info, source = _active_workspace_info()
        if not info:
            if source:
                self.report({"ERROR"}, "EXMTConfig.json 里没有选中的工作空间；先在 App 模型页选一个")
            else:
                self.report({"ERROR"}, "找不到 EXMTConfig.json；先在 App 模型页选一个工作空间")
            return {"CANCELLED"}
        folder = info.get("dir") or ""
        manifest_path = os.path.join(folder, "workspace.json")
        if not folder or not os.path.isfile(manifest_path):
            self.report({"ERROR"}, "工作空间目录或 workspace.json 不存在：%s" % folder)
            return {"CANCELLED"}
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        label = info.get("label") or manifest.get("label") or os.path.basename(folder)

        tex_paths = [os.path.join(folder, t["file"]) for t in manifest.get("textures", [])]

        # one armature for the whole skin: the game ships a *subset* of bones with
        # every mesh, so the union (skeleton.json) is the only complete rig
        bones = _load_skeleton(folder)
        armature = None
        collection = _workspace_collection(label)
        if bones:
            armature = _reuse_armature(
                collection, (manifest.get("entry") or {}).get("id") or label or "exmt_skeleton", bones)
            if armature is None:
                armature = mesh_io.build_armature(
                    (manifest.get("entry") or {}).get("id") or label or "exmt_skeleton", bones)

        # everything this import creates lives in one collection, so two
        # workspaces can be loaded side by side without mixing up the objects.
        # A re-import must not leave the previous meshes behind (they would come
        # back as `…_body_lod0.001`): drop them *before* importing, while the
        # object nodes that pointed at them stay put and get re-pointed by submesh.
        dropped = _forget_previous_import(folder, label, keep_rig=armature)
        if armature is not None:
            _link(collection, armature)

        imported = {}
        for item in manifest.get("meshes", []):
            path = os.path.join(folder, item["file"])
            if not os.path.isfile(path):
                continue
            obj, _arm, _meta = mesh_io.import_mesh(path, armature=armature)
            imported[item.get("mesh", "")] = obj
            _link(collection, obj)

            mat = _textured_material(obj.name + "_mat", tex_paths, _part_of(item.get("mesh", "")))
            if mat is not None:
                for slot in obj.material_slots:
                    slot.material = mat

        # 动作参考：每个导出到 animations/ 的片段做成一个 Action，挂到这副骨架上
        anims, anim_names, anim_notes = 0, [], {}
        if armature is not None and manifest.get("animations"):
            anims, anim_names, anim_notes = _animation_actions(folder, armature, bones, label)

        # one blueprint per import; its object nodes are pre-wired to the imported
        # meshes, so 「生成 Mod」 works immediately and you only re-wire what you swap
        tree = blueprint.new_blueprint(folder, label)
        context.scene.exmt_blueprint = tree
        _cache_texture_meta(tree, folder, manifest)
        wired = _prewire_blueprint(tree, imported, folder, label)
        wired["dropped_before"] = dropped
        dropped = _drop_replaced_objects(wired.get("stale_objects") or [], imported)
        reused = "（复用已有蓝图，连线和贴图选择都保留）" if wired.get("reused") else ""

        if not bones:
            self.report({"WARNING"}, "导入 %d 个网格 · %d 张贴图 · %s：这个工作空间是旧版"
                                     "（没有 skeleton.json），每个网格只有它自己那几根骨；"
                                     "回 App 重新点一次「开始制作」即可拿到完整骨架"
                        % (len(imported), len(tex_paths), label))
            return {"FINISHED"}
        self.report({"INFO"}, "导入 %d 个网格 · %d 张贴图 · %d 根骨骼 · %d 个动作 · %s · 集合「%s」 · 蓝图「%s」%s"
                              "（%d 个物体 + %d 个贴图节点，Frame「%s」%s）"
                    % (len(imported), len(tex_paths), len(bones), anims, label,
                       collection.name, tree.name, reused, wired["wired"], wired["textures"],
                       wired["frame"],
                       ("，清掉 %d 个被替换的旧网格" % dropped) if dropped else ""))
        if anims:
            self.report({"INFO"}, "动作已挂在这副骨架上：%s" % "、".join(anim_names[:4]))
        # 动作和骨架的匹配度必须说出来：动作是按角色做的，骨架是"你勾了哪些网格"的并集
        for clip, ratio in (anim_notes.get("skipped") or [])[:3]:
            self.report({"WARNING"}, "跳过动作「%s」：这副骨架只匹配 %s 根骨，套上去只会变形"
                        % (clip, ratio))
        for clip, matched, animated in (anim_notes.get("partial") or [])[:3]:
            self.report({"WARNING"}, "动作「%s」只匹配 %d/%d 根骨，动作会显示不全（多勾几个网格再导出可补齐）"
                        % (clip, matched, animated))
        return {"FINISHED"}


# --------------------------------------------------------------------------- #
# collections + blueprint wiring
# --------------------------------------------------------------------------- #
def _workspace_collection(label: str):
    """A fresh collection for one import (`EXMT · <label>`), linked to the scene."""
    name = "EXMT · %s" % (label or "工作空间")
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _link(collection, obj) -> None:
    """Link into `collection` unless it is already in exactly that one."""
    if collection not in obj.users_collection:
        collection.objects.link(obj)
    # an object can only live in one collection if we want the collection to mean
    # "this import"; unlink it from the scene root / other imports' collections
    for other in list(obj.users_collection):
        if other != collection:
            try:
                other.objects.unlink(obj)
            except RuntimeError:
                pass


def _cache_texture_meta(tree, folder: str, manifest: dict) -> dict:
    """Ask the core what each texture expects (size / format / mips) and cache it.

    One call per bundle: a texture bundle can hold hundreds of Texture2Ds and
    opening it once is the difference between "instant" and "half a minute".
    """
    grouped = {}
    for item in manifest.get("textures", []):
        grouped.setdefault(item.get("bundle", ""), []).append(item.get("name", ""))
    bundles = manifest.get("bundles") or {}
    if not bundles:
        spec_path = os.path.join(folder, "spec.json")
        if os.path.isfile(spec_path):
            with open(spec_path, encoding="utf-8") as fh:
                bundles = (json.load(fh) or {}).get("bundles") or {}
    meta = {}
    for hash_, names in grouped.items():
        path = bundles.get(hash_)
        if not path or not os.path.isfile(path):
            continue
        try:
            res = run_core(["texinfo", path], timeout=300)
            rows = json.loads(res["stdout"].strip().splitlines()[-1])
        except Exception:
            continue
        wanted = set(names)
        for row in rows:
            if row.get("name") in wanted:
                meta[row["name"]] = {
                    "width": row.get("width"), "height": row.get("height"),
                    "mips": row.get("mips"), "format": row.get("format"),
                    "format_name": row.get("format_name"),
                    "supported": row.get("supported"), "bundle": hash_,
                }
    tree.texture_meta = json.dumps(meta, ensure_ascii=False)
    return meta


def _drop_replaced_objects(names: list, imported: dict) -> int:
    """Delete meshes a re-import replaced (nothing else references them).

    Without this the collection would accumulate `body_lod0.001`, `.002`, … every
    time the workspace is re-imported.
    """
    keep = {obj.name for obj in imported.values()}
    removed = 0
    for name in names:
        if name in keep:
            continue
        obj = bpy.data.objects.get(name)
        if obj is None or obj.users > 0:
            # still linked somewhere (or used by a node the user kept): leave it
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
    return removed


def _frame_around(tree, label: str, nodes: list, pad: float = 26.0):
    """A frame that actually hugs its nodes.

    `frame.shrink` only re-fits when the frame is dragged in the UI, so a frame
    built from Python stays 160×100 and the nodes stick out of it. `NodeFrame`
    exposes writable `width`/`height` in 5.2, so measure the children instead.
    """
    if not nodes:
        return None
    frame = tree.nodes.new("NodeFrame")
    frame.label = label
    frame.shrink = False
    left = min(n.location.x for n in nodes)
    right = max(n.location.x + n.width for n in nodes)
    top = max(n.location.y for n in nodes)
    bottom = min(n.location.y - n.height for n in nodes)
    frame.location = (left - pad, top + pad)
    frame.width = (right - left) + 2 * pad
    frame.height = (top - bottom) + 2 * pad
    for node in nodes:
        node.parent = frame
    return frame


def _prewire_blueprint(tree, imported: dict, folder: str, label: str) -> dict:
    """Build the ready-to-run graph: objects → 分组 → 生成 Mod, plus texture nodes.

    Everything imported lands in one **frame** called `<label>_original`, so the
    graph reads as "these are the original meshes" and you can immediately press
    生成 Mod — or pull one object out of the frame and wire your own model instead.
    The replacement texture nodes get their own frame on the left.

    **Re-importing the same workspace updates this graph instead of rebuilding it**:
    nodes inside `_original` are re-pointed at the freshly imported meshes (their
    wiring, and everything you built outside the frame — extra objects, extra
    groups, picked replacement images — is left alone).  Deleting the old nodes
    instead would throw away exactly the work the graph exists to hold.
    """
    rows = {row["mesh"]: row for row in blueprint._rows(tree)}
    generate = next((n for n in tree.nodes if n.bl_idname == "EXMTNodeGenerate"), None)
    if generate is None:
        generate = tree.nodes.new("EXMTNodeGenerate")
        generate.ensure_slots()
    generate.location = (560, -80)

    frame_name = "%s_original" % label
    frame = next((n for n in tree.nodes
                  if n.bl_idname == "NodeFrame" and n.label == frame_name), None)
    fresh = not frame

    group = next((n for n in tree.nodes if n.bl_idname == "EXMTNodeGroup"), None)
    if group is None or fresh:
        group = tree.nodes.new("EXMTNodeGroup")
        group.location = (200, -80)
        for _ in range(max(0, len(imported) - 4)):
            group.ensure_slots(spare=1)

    # nodes that came from a previous import of this workspace: identified by the
    # frame they live in, so anything the user built outside it is never touched.
    # NB: `parent` returns a fresh wrapper on every access, so compare with `==`,
    # never `is` — `is` was False for the very frame the node was parented to.
    def in_frame(node, container):
        return container is not None and node.parent is not None and node.parent == container

    previous = {}
    if frame is not None:
        for node in tree.nodes:
            if node.bl_idname == "EXMTNodeObject" and in_frame(node, frame):
                previous.setdefault(str(getattr(node, "submesh", "-1")), node)
    stale = []

    wired = 0
    objects = []
    y = 0.0
    for mesh_name, obj in imported.items():
        row = rows.get(mesh_name)
        if row is None:
            continue
        index = str(row["index"])
        node = previous.pop(index, None)
        if node is None:
            node = tree.nodes.new("EXMTNodeObject")
            node.submesh = index
            objects.append(node)
        else:
            # keep the node (and its links): only the mesh behind it is refreshed
            old = getattr(node, "object", None)
            if old is not None and old.name != obj.name:
                stale.append(old)
            node.submesh = index
        node.width = 300
        node.location = (-560, y)
        y -= 210.0
        node.object = obj
        if not in_frame(node, frame):
            objects.append(node)
        if not node.outputs[0].links:
            socket = next((s for s in group.inputs if not s.is_linked), None)
            if socket is None:
                group.ensure_slots()
                socket = next((s for s in group.inputs if not s.is_linked), None)
            if socket is not None:
                tree.links.new(node.outputs[0], socket)
                wired += 1

    # an object node from the last import whose submesh no longer exists: leave the
    # node (the user may have re-pointed it) but drop the mesh it referenced
    for node in previous.values():
        old = getattr(node, "object", None)
        if old is not None:
            stale.append(old)
        node.object = None

    group.ensure_slots()
    if not group.outputs[0].links:
        free = next((s for s in generate.inputs if not s.is_linked), None)
        if free is not None:
            tree.links.new(group.outputs[0], free)
    generate.ensure_slots()

    if frame is None:
        frame = _frame_around(tree, frame_name, objects)
    elif objects or any(n.bl_idname == "EXMTNodeObject" for n in tree.nodes):
        # re-fit around everything the frame owns (Blender only re-fits on drag)
        inside = [n for n in tree.nodes
                  if n.bl_idname == "EXMTNodeObject" and in_frame(n, frame)]
        if inside:
            frame.shrink = False
            left = min(n.location.x for n in inside)
            right = max(n.location.x + n.width for n in inside)
            top = max(n.location.y for n in inside)
            bottom = min(n.location.y - n.height for n in inside)
            frame.location = (left - 26, top + 26)
            frame.width = (right - left) + 52
            frame.height = (top - bottom) + 52

    # replacement textures: one node each, in a grid so twelve do not make a
    # two-metre column; they do not need links (an image on the node = replace it).
    # Re-import keeps the existing nodes — that is where the picked images live.
    texture_frame_name = "%s_textures" % label
    texture_frame = next((n for n in tree.nodes
                          if n.bl_idname == "NodeFrame" and n.label == texture_frame_name), None)
    have = {n.texture: n for n in tree.nodes if n.bl_idname == "EXMTNodeTexture"}
    texture_nodes = []
    columns = 2
    for index, row in enumerate(blueprint.textures(folder)):
        name = row.get("name", "")
        node = have.pop(name, None)
        if node is None:
            node = tree.nodes.new("EXMTNodeTexture")
            node.width = 320
            node.texture = name
        node.location = (-1500 + (index % columns) * 340, -((index // columns) * 190))
        if texture_frame is not None and node.parent != texture_frame:
            node.parent = texture_frame
        texture_nodes.append(node)
    for node in have.values():
        # a texture the workspace no longer contains: keep the node (and its image)
        # but take it out of the frame so it cannot be packed by accident
        node.parent = None
    if texture_frame is None:
        texture_frame = _frame_around(tree, texture_frame_name, texture_nodes)
    elif texture_nodes:
        texture_frame.shrink = False
        left = min(n.location.x for n in texture_nodes)
        right = max(n.location.x + n.width for n in texture_nodes)
        top = max(n.location.y for n in texture_nodes)
        bottom = min(n.location.y - n.height for n in texture_nodes)
        texture_frame.location = (left - 26, top + 26)
        texture_frame.width = (right - left) + 52
        texture_frame.height = (top - bottom) + 52

    return {
        "wired": wired,
        "textures": len(texture_nodes),
        "group": group.name,
        "frame": frame.label if frame else "",
        "texture_frame": texture_frame.label if texture_frame else "",
        "original_frame_size": (round(frame.width), round(frame.height)) if frame else None,
        "reused": not fresh,
        "stale_objects": [obj.name for obj in stale],
    }


# --------------------------------------------------------------------------- #
# panel
# --------------------------------------------------------------------------- #
class EXMT_OT_generate_mod(Operator):
    bl_idname = "exmt.generate_mod"
    bl_label = "生成 Mod（打包到工作空间）"
    bl_description = "把所有蓝图里连到「生成 Mod」节点的物体按 Submesh 打包，输出到 <工作空间>/GeneratedMod"
    bl_options = {"REGISTER"}

    workspace: StringProperty(name="工作空间目录", default="")
    #: only pack this blueprint (the generate node's own button sets it)
    blueprint: StringProperty(name="蓝图", default="")

    # -- setup ------------------------------------------------------------- #
    def _prepare(self, context) -> str:
        """Resolve the workspace and start a `Generation`.  Returns an error text."""
        info, _source = _active_workspace_info()
        info = info or {}
        folder = self.workspace or info.get("dir") or ""
        self.label = info.get("label") or ""
        if not folder or not os.path.isfile(os.path.join(folder, "workspace.json")):
            return "没有可用的工作空间；先在 App 模型页选一个并点「一键导入当前工作空间」"
        tree = bpy.data.node_groups.get(self.blueprint) if self.blueprint else None
        if tree is None:
            tree = getattr(context.scene, "exmt_blueprint", None)
        self._window = getattr(context, "window_manager", None)
        self._pushed = False
        self._timer = None
        try:
            self._run = Generation(folder, self.label, tree=tree, progress=self._progress).start()
        except Exception as exc:  # noqa: BLE001 - surface the core's message
            self._run = None
            return str(exc)[:300]
        return ""

    def invoke(self, context, event):
        """Drive the run from a timer so the window keeps drawing while it works.

        Texture encoding is texconv work (measured 5 s for one 2048² BC7 texture)
        plus a core `modpack` pass; run straight through, that is a minute of a
        frozen window with no feedback.  Here each texture lands in its own timer
        tick: the progress bar moves, the status bar names the texture, Esc cancels.
        """
        error = self._prepare(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        if getattr(context, "window", None) is None:
            return self._finish_sync(context)          # headless: no event loop
        self._status(context, "EXMT 正在生成 Mod…")
        context.window_manager.modal_handler_add(self)
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        """Panel/script path: run the same steps without the modal loop."""
        error = self._prepare(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        return self._finish_sync(context)

    def _finish_sync(self, context):
        try:
            while not self._run.step():
                pass
        except Exception as exc:  # noqa: BLE001
            self._cleanup(context)
            self.report({"ERROR"}, str(exc)[:300])
            return {"CANCELLED"}
        summary = self._run.summary or {}
        self._cleanup(context)
        return self._report(summary)

    # -- progress / teardown ------------------------------------------------ #
    def _progress(self, done, total, name):
        window = getattr(self, "_window", None)
        if window is None:
            return
        total = max(1, total)
        if not getattr(self, "_pushed", False):
            window.progress_begin(0, total)
            self._pushed = True
        window.progress_update(min(done, total))
        text = "编码贴图 %d/%d %s" % (min(done + 1, total), total,
                                     (name or "").replace("c_AlvaSSR0102_slg_", ""))
        self._status(bpy.context, "EXMT 正在%s" % text)
        self._node_progress(text)

    def _node_progress(self, text):
        """Draw the progress on the generate node too, and repaint the editor.

        The modal operator yields between textures, so a node label update is what
        the user actually sees moving (the status bar is a 20 px strip).
        """
        run = getattr(self, "_run", None)
        if run is None:
            return
        for node in run.generate_nodes:
            try:
                node.progress_text = text
            except Exception:
                pass
        for window in getattr(bpy.context.window_manager, "windows", []):
            for area in window.screen.areas:
                if area.type == "NODE_EDITOR":
                    area.tag_redraw()

    def _status(self, context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass

    def _cleanup(self, context):
        window = getattr(self, "_window", None)
        run = getattr(self, "_run", None)
        if run is not None:
            for node in run.generate_nodes:
                try:
                    node.progress_text = ""
                except Exception:
                    pass
        if window is not None and getattr(self, "_pushed", False):
            try:
                window.progress_end()
            except Exception:
                pass
            self._pushed = False
        if getattr(self, "_timer", None) is not None and window is not None:
            try:
                window.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        self._status(context, None)
        run = getattr(self, "_run", None)
        if run is not None:
            run.close()

    def modal(self, context, event):
        if event.type in {"ESC", "RIGHTMOUSE"}:
            self._cleanup(context)
            self.report({"WARNING"}, "已取消生成 Mod")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        try:
            finished = self._run.step()
        except Exception as exc:  # noqa: BLE001
            self._cleanup(context)
            self.report({"ERROR"}, str(exc)[:300])
            return {"CANCELLED"}
        if not finished:
            return {"RUNNING_MODAL"}
        summary = self._run.summary or {}
        self._cleanup(context)
        return self._report(summary)

    def _report(self, summary):
        msg = "生成 Mod：%d 个 Submesh → %d 个包（%s）" % (
            summary.get("packed", summary.get("meshes", 0)), summary.get("bundles", 0),
            summary.get("dir", ""))
        if summary.get("packed_textures"):
            msg += " · %d 张贴图（新编码 %d）" % (
                summary["packed_textures"], summary.get("encoded_textures", 0))
        skipped_tex = summary.get("skipped_textures") or []
        if skipped_tex:
            msg += "；未替换：%s" % "、".join(skipped_tex[:3]) + ("…" if len(skipped_tex) > 3 else "")
        skipped = summary.get("skipped") or []
        if skipped:
            msg += "；未连接的槽位：%s" % "、".join(skipped[:4]) + (
                "…" if len(skipped) > 4 else "")
        if summary.get("missing"):
            self.report({"WARNING"}, msg + "；%d 项缺文件" % len(summary["missing"]))
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


def _texconv() -> str:
    """The bundled texconv.exe (BC7/BC1/…), with an env override for other installs."""
    override = os.environ.get("EXMT_TEXCONV", "")
    if override and os.path.isfile(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("texconv.exe", "texconv"):
        path = os.path.join(here, "bin", name)
        if os.path.isfile(path):
            return path
    raise RuntimeError("找不到 texconv.exe：应该在 %s\\bin\\texconv.exe" % here)


def _core_module(name: str):
    """Import a module of the Python core inside Blender.

    Blender's bundled Python has numpy but neither UnityPy nor lz4, so the core
    package (`exmt_core/__init__`) cannot be imported here. The modules that are
    dependency-free (`ddsinfo`) are loaded straight from their file instead.
    """
    import importlib.util

    path = os.path.join(_core_dir(), "exmt_core", name + ".py")
    if not os.path.isfile(path):
        raise RuntimeError("找不到核心模块：%s" % path)
    spec = importlib.util.spec_from_file_location("exmt_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _encode_texture(image_path: str, meta: dict, work_dir: str) -> bytes:
    """Convert a replacement image into the target's own format, size and mips.

    `-srgbi -srgbo` is an identity transform on the pixel values, which is what we
    want: the numbers in the PNG are the numbers that must end up in the asset.
    Without it texconv treats a Blender-written PNG as sRGB and writes *linear*
    blocks (measured: 31 → 4, 219 → 181, i.e. everything much darker).
    """
    os.makedirs(work_dir, exist_ok=True)
    cmd = [_texconv(),
           "-f", meta["format_name"],
           "-w", str(meta["width"]), "-h", str(meta["height"]),
           "-m", str(meta.get("mips") or 1),
           "-srgbi", "-srgbo",
           "-ft", "dds", "-o", work_dir, "-y", "-nologo", image_path]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=600)
    stem = os.path.splitext(os.path.basename(image_path))[0]
    dds = os.path.join(work_dir, stem + ".dds")
    if not os.path.isfile(dds):
        raise RuntimeError("texconv 没有输出 DDS：%s" % (res.stdout + res.stderr)[-300:])
    return _core_module("ddsinfo").parse_dds(dds)["payload"]


def _texture_jobs(tree, folder: str, patchdir: str, meta: dict) -> tuple:
    """Prepare (but do not encode) every texture node that has a replacement image.

    Returns `(jobs, skipped)`.  Preparing writes the node's image out as a PNG on
    the **main thread** (bpy image access is not thread-safe) and decides from the
    cache whether the texture needs encoding at all:

    * **cache**: unchanged source file + unchanged target → the `.bin` on disk is
      reused, so a second 「生成 Mod」 encodes nothing;
    * the actual texconv calls are pure subprocess work, so they run in a thread
      pool (measured 5 s per 2048² BC7 texture, twelve of them in ~35 s instead of
      ~60 s single-threaded).
    """
    skipped = []
    nodes = [n for n in tree.nodes if n.bl_idname == "EXMTNodeTexture"]
    pending = [n for n in nodes if n.image is not None]
    work = os.path.join(bpy.app.tempdir or tempfile.gettempdir(), "exmt-tex")
    ddsdir = os.path.join(work, "dds")
    os.makedirs(ddsdir, exist_ok=True)

    jobs = []
    for node in pending:
        info = meta.get(node.texture) or {}
        if not info:
            skipped.append("%s（本体里没有这张贴图）" % (node.texture or "?"))
            continue
        if not info.get("supported", True):
            skipped.append("%s（格式 %s 不支持）" % (node.texture, info.get("format")))
            continue
        out = os.path.join(patchdir, (node.texture or "texture") + ".bin")
        stamp = _image_stamp(node.image, info)
        sidecar = out + ".json"
        cached = bool(stamp and _cache_hit(sidecar, stamp) and os.path.isfile(out))
        png = ""
        if not cached:
            png = os.path.join(work, (node.texture or "texture") + ".png")
            image = node.image
            # `image.save()` repoints `filepath_raw`; restore it so the user's
            # datablock keeps its own path (and the cache key stays stable)
            original_path = image.filepath_raw
            original_format = image.file_format
            try:
                image.filepath_raw = png
                image.file_format = "PNG"
                try:
                    image.save()
                except Exception:
                    # packed or generated images have no file on disk yet
                    image.save_render(png)
            finally:
                image.filepath_raw = original_path
                image.file_format = original_format
        jobs.append({"node": node, "info": info, "out": out, "sidecar": sidecar,
                     "stamp": stamp, "png": png, "cached": cached})

    for node in nodes:
        if node.image is None:
            skipped.append(node.texture or "（未选贴图）")
    return jobs, skipped


def _encode_job(job: dict, ddsdir: str) -> None:
    """Run one texconv job and write its `.bin` (+ cache sidecar). Thread-safe."""
    payload = _encode_texture(job["png"], job["info"], ddsdir)
    tmp = job["out"] + ".part"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, job["out"])
    if job["stamp"]:
        with open(job["sidecar"], "w", encoding="utf-8") as fh:
            json.dump(job["stamp"], fh, ensure_ascii=False)


def _texture_rows(jobs: list, folder: str) -> list:
    """One row per bundle that stores the texture (a name can live in several)."""
    rows = []
    for job in jobs:
        info = job["info"]
        name = job["node"].texture
        for hash_ in (texture_bundles_of(name, info, folder) or [info.get("bundle", "")]):
            if not hash_:
                continue
            rows.append({
                "bundle": hash_,
                "name": name,
                "file": job["out"],
                "format": info.get("format"),
                "width": info.get("width"),
                "height": info.get("height"),
                "mips": info.get("mips"),
            })
    return rows


def _texture_patches(tree, folder: str, patchdir: str, meta: dict, progress=None) -> tuple:
    """Synchronous texture export: prepare, encode everything, build the rows."""
    jobs, skipped = _texture_jobs(tree, folder, patchdir, meta)
    todo = [job for job in jobs if not job["cached"]]
    if todo:
        ddsdir = os.path.join(bpy.app.tempdir or tempfile.gettempdir(), "exmt-tex", "dds")
        os.makedirs(ddsdir, exist_ok=True)
        done = 0
        with ThreadPoolExecutor(max_workers=min(len(todo), 6)) as pool:
            futures = {pool.submit(_encode_job, job, ddsdir): job for job in todo}
            for future in as_completed(futures):
                future.result()
                done += 1
                if progress:
                    progress(done, len(todo), futures[future]["node"].texture or "")
    return _texture_rows(jobs, folder), skipped, len(todo)


def texture_bundles_of(name: str, info: dict, folder: str) -> list:
    """Every bundle that holds this texture (from the workspace's index).

    The index is built by the app when the workspace is created; an older
    workspace (or one whose texture the index has not seen) falls back to the
    bundle the app recorded, which is what the pre-index builds did.
    """
    manifest = _manifest(folder) or {}
    table = manifest.get("texture_bundles") or {}
    found = [h for h in (table.get(name) or []) if h]
    if not found and info.get("bundle"):
        found = [info["bundle"]]
    return found


def _image_stamp(image, info: dict):
    """Identity of "this source image, for this target" — None when it has no file.

    A painted/generated datablock has no file to watch (or is dirty in memory), so
    it is always re-encoded; a loaded PNG/JPG is reused until the file's mtime or
    size changes, or the datablock gets edited without saving.
    """
    path = getattr(image, "filepath", "") or ""
    if not path or not os.path.isfile(path) or getattr(image, "is_dirty", False):
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    exe = _texconv()
    return {
        "source": os.path.normcase(os.path.abspath(path)),
        "mtime": int(stat.st_mtime),
        "size": int(stat.st_size),
        "image": image.name,
        "format": info.get("format"),
        "width": info.get("width"),
        "height": info.get("height"),
        "mips": info.get("mips"),
        "texconv": int(os.path.getmtime(exe)) if os.path.isfile(exe) else 0,
    }


def _cache_hit(sidecar: str, stamp: dict) -> bool:
    try:
        with open(sidecar, encoding="utf-8") as fh:
            return json.load(fh) == stamp
    except Exception:
        return False


def generate_mod(folder: str, label: str = "", context=None, tree=None, progress=None) -> dict:
    """Synchronous 生成 Mod (headless / scripted use).

    The operator drives `Generation` step by step so Blender keeps redrawing; this
    wrapper runs the same steps in a loop and returns the packed summary.
    """
    run = Generation(folder, label, tree=tree, progress=progress).start()
    try:
        while not run.step():
            pass
    finally:
        run.close()
    return run.summary


class Generation:
    """One 生成 Mod run, split so the window keeps redrawing while it works.

    `start()` does everything that needs `bpy` (graph walk, mesh export, writing
    the replacement PNGs); `step()` is called from a modal timer and drains the
    texconv threads, then runs the `modpack` core command in another thread.  The
    synchronous `generate_mod()` below just loops `step()` — same result, no UI.
    """

    def __init__(self, folder: str, label: str = "", tree=None, progress=None):
        self.folder = folder
        self.label = label
        self.tree = tree
        self.progress = progress
        self.summary = None
        self.error = None
        self.generate_nodes = []
        self._pool = None
        self._jobs = []
        self._futures = {}
        self._todo = 0
        self._done = 0
        self._pack_future = None

    # -- phase A: main thread, may touch bpy -------------------------------- #
    def start(self) -> "Generation":
        folder, label = self.folder, self.label
        manifest = _manifest(folder)
        if not manifest:
            raise RuntimeError("工作空间里没有 workspace.json：%s" % folder)
        rows = submeshes(folder)
        if not rows:
            raise RuntimeError("工作空间里没有 Submesh 列表，回 App 重新点一次「开始制作」")

        extra = {}
        for name in ("bundles", "mesh_bundles"):
            if not manifest.get(name):
                spec_path = os.path.join(folder, "spec.json")
                if os.path.isfile(spec_path):
                    with open(spec_path, encoding="utf-8") as fh:
                        extra = json.load(fh)
                break
        self.bundles = manifest.get("bundles") or extra.get("bundles") or {}
        self.mesh_bundles = manifest.get("mesh_bundles") or extra.get("mesh_bundles") or {}

        trees = [self.tree] if self.tree is not None else [
            group for group in bpy.data.node_groups
            if group.bl_idname == "EXMT_BlueprintTree"
            and os.path.normcase(getattr(group, "workspace", "") or "") == os.path.normcase(folder)
        ]
        trees = [t for t in trees if isinstance(t, blueprint.EXMTBlueprintTree)]
        if not trees:
            raise RuntimeError("这个工作空间还没有蓝图：先在 EXMT 面板点「一键导入当前工作空间」")

        by_index = {int(row["index"]): row for row in rows}
        work = os.path.join(bpy.app.tempdir or tempfile.gettempdir(), "exmt-mod")
        os.makedirs(work, exist_ok=True)
        self.work = work
        self.outdir = os.path.join(folder, "GeneratedMod")
        self.patchdir = os.path.join(self.outdir, "patches")
        os.makedirs(self.patchdir, exist_ok=True)

        # the graph decides what gets packed: walk every generate node (through any
        # 分组 node) and take each 物体 node's own submesh.  A node that is not
        # plugged in — an import you only keep for reference — is never packed.
        meshes = []
        filled = {}
        textures = []
        texture_skipped = []
        texture_jobs = []
        for group in trees:
            for node in group.nodes:
                if node.bl_idname != "EXMTNodeGenerate":
                    continue
                self.generate_nodes.append(node)
                for source in blueprint.wired_objects(node):
                    index = str(getattr(source, "submesh", "-1"))
                    row = by_index.get(int(index)) if index not in ("", "-1") else None
                    obj = getattr(source, "object", None)
                    if row is None or obj is None or obj.type != "MESH":
                        continue
                    filled[int(index)] = (obj, row)      # later blueprints win
            jobs, skipped_tex = _texture_jobs(group, folder, self.patchdir,
                                              blueprint.texture_meta(group))
            merged = {row["name"]: row for row in texture_jobs}
            merged.update({job["node"].texture: job for job in jobs})
            texture_jobs = list(merged.values())
            texture_skipped.extend(skipped_tex)

        for index, (obj, row) in sorted(filled.items()):
            mesh_name = row["mesh"]
            path = os.path.join(self.patchdir, mesh_name + ".exmtmesh")
            # An imported workspace mesh carries its own bone table; a foreign one
            # (MMD/VRM/another game) does not, and it must borrow the table of the
            # slot it fills.  Without `bone_table` the exporter resolved every
            # group name to None and dropped every influence, writing a mesh with
            # no skinning at all — the game then shows nothing (measured
            # 2026-10-19: Sally_outfit -> all weights 0.0, no bone hashes).
            bone_table = None
            if not obj.get("exmt_bones"):
                bone_table = target_bone_table(row.get("bundle", ""), mesh_name)
            # a foreign model usually carries far more material slots than the game
            # mesh: everything past its slot count is a submesh Unity never draws
            slots_limit = 0
            if len(obj.material_slots) > 1:
                slots_limit = target_material_slots(row.get("bundle", ""), mesh_name)
            info = mesh_io.export_mesh(obj, path, {"name": mesh_name},
                                       bone_table=bone_table, material_slots=slots_limit)
            if info.get("groups") and not info.get("influenced"):
                raise RuntimeError(
                    "「%s」的权重一根骨骼都没匹配上（%d 个顶点组全部对不上目标网格 %s 的骨骼）："
                    "先在面板里用「传递权重并绑定骨架」把这个物体的权重刷到目标骨架上，再生成 Mod。"
                    % (obj.name, info.get("groups"), mesh_name))
            if bone_table:
                mesh_io.reuse_rig(obj, bone_table, self.report)
            for hash_ in [h for h in (self.mesh_bundles.get(mesh_name) or [row.get("bundle", "")]) if h]:
                meshes.append({"bundle": hash_, "mesh": mesh_name, "file": path})

        textures = _texture_rows(texture_jobs, folder)
        skipped = [row["alias"] for index, row in sorted(by_index.items()) if index not in filled]
        if not meshes and not textures:
            raise RuntimeError("蓝图里还没有任何东西连上：把「物体」或「贴图」节点连到「生成 Mod」节点")

        self.textures = textures
        self.meshes = meshes
        self.filled = filled
        self.skipped = skipped
        self.texture_skipped = texture_skipped
        self.texture_jobs = texture_jobs

        spec = {
            "outdir": self.outdir,
            "label": label or manifest.get("label", ""),
            "workspace": manifest.get("workspace_id", "") or folder,
            "game_bundles": manifest.get("game_bundles", ""),
            "bundles": self.bundles,
            "meshes": meshes,
            "textures": textures,
        }
        self.spec_path = os.path.join(work, "modspec.json")
        with open(self.spec_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)

        # drop patch files nothing points at any more (a slot was unlinked or
        # swapped), so `GeneratedMod/patches` mirrors what this Mod contains
        keep = {os.path.basename(item["file"]) for item in meshes + textures}
        for name in os.listdir(self.patchdir):
            if name.endswith((".exmtmesh", ".bin")) and name not in keep:
                try:
                    os.remove(os.path.join(self.patchdir, name))
                except OSError:
                    pass
            elif name.endswith(".bin.json") and name[:-5] not in keep:
                try:
                    os.remove(os.path.join(self.patchdir, name))
                except OSError:
                    pass

        todo = [job for job in texture_jobs if not job["cached"]]
        self._todo = len(todo)
        self._done = 0
        if todo:
            ddsdir = os.path.join(bpy.app.tempdir or tempfile.gettempdir(), "exmt-tex", "dds")
            os.makedirs(ddsdir, exist_ok=True)
            self._pool = ThreadPoolExecutor(max_workers=min(len(todo), 6))
            self._futures = {self._pool.submit(_encode_job, job, ddsdir): job for job in todo}
        return self

    # -- phase B: called from a timer / modal ------------------------------- #
    def step(self) -> bool:
        """One UI tick.  Returns True when the whole run is finished."""
        for future in [f for f in self._futures if f.done()]:
            job = self._futures.pop(future)
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - report, keep the rest going
                self.texture_skipped.append("%s（编码失败：%s）" % (job["node"].texture, exc))
            self._done += 1
            if self.progress:
                self.progress(self._done, max(1, self._todo), job["node"].texture or "")
        if self._futures:
            return False

        if self._pack_future is None:
            if self.progress:
                self.progress(self._todo, max(1, self._todo), "打包")
            self._pool = self._pool or ThreadPoolExecutor(max_workers=1)
            self._pack_future = self._pool.submit(run_core, ["modpack", self.spec_path], 900)
            return False
        if not self._pack_future.done():
            return False

        res = self._pack_future.result()
        summary = json.loads(res["stdout"].strip().splitlines()[-1])
        summary["skipped"] = self.skipped
        summary["packed"] = len(self.filled)
        summary["packed_textures"] = len(self.textures)
        summary["skipped_textures"] = self.texture_skipped
        summary["encoded_textures"] = self._todo
        self.summary = summary
        self.close()
        return True

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


class EXMT_PT_panel(Panel):
    bl_label = "EXMT Modbox"
    bl_idname = "EXMT_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "EXMT"

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="工作空间")
        info, source = _active_workspace_info()
        if info:
            label = info.get("label") or os.path.basename(info.get("dir", "")) or "当前工作空间"
            box.label(text="当前：" + label, icon="CHECKMARK")
        elif source:
            box.label(text="没有选中的工作空间", icon="INFO")
        else:
            box.label(text="未找到 EXMTConfig.json", icon="ERROR")
        box.operator("exmt.import_workspace", icon="IMPORT")

        # 蓝图：选一个、新建、打开节点编辑器、删除；生成按钮在生成节点上也有
        box = layout.box()
        box.label(text="蓝图")
        box.prop(context.scene, "exmt_blueprint", text="")
        row = box.row(align=True)
        row.operator("exmt.blueprint_new", text="", icon="ADD")
        row.operator("exmt.blueprint_open", text="", icon="NODETREE")
        row.operator("exmt.blueprint_sync", text="", icon="FILE_REFRESH")
        row.operator("exmt.blueprint_delete", text="", icon="TRASH")
        tree = getattr(context.scene, "exmt_blueprint", None)
        if tree is not None:
            rows = blueprint._rows(tree)
            nodes = [n for n in tree.nodes if n.bl_idname == "EXMTNodeGenerate"]
            linked = sum(len([s for s in n.inputs if s.is_linked]) for n in nodes)
            box.label(text="%d 个 Submesh · 已连 %d 个槽位" % (len(rows), linked), icon="LINKED")
            op = box.operator("exmt.generate_mod", icon="PACKAGE")
            op.workspace = tree.workspace or (info or {}).get("dir", "")
            op.blueprint = tree.name
        else:
            box.label(text="先点「一键导入当前工作空间」", icon="INFO")

        col = layout.column(align=True)
        col.label(text="模型交换")
        col.operator("exmt.import_gltf", icon="IMPORT")
        col.operator("exmt.import_mesh", icon="IMPORT")
        col.operator("exmt.export_mesh", icon="EXPORT")

        box = layout.box()
        box.label(text="外部衣服")
        box.operator("exmt.transfer_weights", icon="MOD_VERTEX_WEIGHT")

        box = layout.box()
        box.label(text="Bundle 流程")
        op = box.operator("exmt.extract_bundle", icon="PACKAGE")
        op = box.operator("exmt.export_and_pack", icon="FILE_TICK")


class EXMT_AddonPreferences(AddonPreferences):
    bl_idname = __name__

    core_dir: StringProperty(name="核心目录（exmt_core）", default=DEFAULT_CORE, subtype="DIR_PATH")
    python_exe: StringProperty(name="Python 解释器", default=DEFAULT_PYTHON, subtype="FILE_PATH")
    config_path: StringProperty(
        name="EXMTConfig.json",
        default=DEFAULT_CONFIG,
        subtype="FILE_PATH",
        description="App 实时写出的当前工作空间配置",
    )
    workspace_dir: StringProperty(
        name="旧版工作空间目录（回退）",
        default=DEFAULT_WORKSPACE,
        subtype="DIR_PATH",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "core_dir")
        layout.prop(self, "python_exe")
        layout.prop(self, "config_path")
        layout.prop(self, "workspace_dir")


classes = (
    EXMT_AddonPreferences,
    EXMT_OT_import_gltf,
    EXMT_OT_import_mesh,
    EXMT_OT_export_mesh,
    EXMT_OT_transfer_weights,
    EXMT_OT_extract_bundle,
    EXMT_OT_pack_bundle,
    EXMT_OT_export_and_pack,
    EXMT_OT_import_workspace,
    EXMT_OT_generate_mod,
    EXMT_PT_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    blueprint.register_blueprint()


def unregister():
    blueprint.unregister_blueprint()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
