"""EXMT blueprint: a node tree that says which object fills which submesh.

The app assigns every mesh of a workspace a **submesh index** (0, 1, 2 …) and the
Blender add-on never guesses an object's identity from its name or from custom
properties. Instead:

```
[物体] ──┐
[物体] ──┼──▶ [生成 Mod]
[物体] ──┘
```

* `EXMTBlueprintTree` — one per 「一键导入当前工作空间」, holds `workspace` (folder).
* `EXMTNodeObject` — pick an object + a submesh (`0. body_lod0`); output socket.
* `EXMTNodeGenerate` — one input slot per submesh of the workspace; a slot's
  connection is the object that will be packed for that index; has the button.

The generate operator lives in `__init__` (it needs the packer); this module only
models the tree, the sockets and the drawing.
"""

import json
import os
import re

import bpy
from bpy.props import EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Node, NodeSocket, NodeTree, Operator
from bpy.utils import register_class, unregister_class


# --------------------------------------------------------------------------- #
# the workspace manifest, parsed once per file change
# --------------------------------------------------------------------------- #
# The node editor redraws *every* node on every mouse move, and a node's label,
# its enum items and its info line all used to call `manifest()` directly: that
# is one open + `json.loads` of workspace.json per node per frame (measured: 34
# nodes, i.e. 34 file reads for a single redraw).  Dragging felt like a slideshow
# and the graph was blamed for it.  The file only changes when the app exports a
# workspace, so parse it once and key the cache on (mtime, size) — a re-export
# invalidates itself, no hooks needed.
_CACHE = {
    "workspace": {},   # path -> (stamp, data, rows)
    "items": {},       # path -> (stamp, enum items)
    "texture_rows": {},
    "texture_items": {},
    "texture_meta": {},
    "images": {},
}
_CACHE_LIMIT = 16


def _stamp(path: str):
    """Identity of a file's contents, or None when it cannot be read."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _pathkey(folder: str) -> str:
    """Normalised workspace.json path; '' for 'no workspace'."""
    if not folder:
        return ""
    return os.path.normcase(os.path.abspath(os.path.join(folder, "workspace.json")))


def _trim(cache: dict) -> None:
    while len(cache) > _CACHE_LIMIT:
        cache.pop(next(iter(cache)))


def _manifest_rows(data: dict) -> list:
    """`[{index, alias, mesh, bundle, file, verts, bones}]` from a parsed workspace."""
    rows = data.get("submeshes")
    if isinstance(rows, list) and rows:
        return rows
    meshes = data.get("meshes")
    if not isinstance(meshes, list):
        meshes = []
    out = []
    for position, item in enumerate(meshes):
        if not isinstance(item, dict):
            continue
        mesh_name = item.get("mesh", "")
        short = re.sub(r"^c_[A-Za-z0-9]+_(?:slg_)?", "", mesh_name) or mesh_name
        out.append({
            "index": int(item.get("index", position)),
            "alias": item.get("alias") or short,
            "file": item.get("file", ""),
            "mesh": mesh_name,
            "bundle": item.get("bundle", ""),
            "verts": item.get("verts", 0),
            # the app records the target's bone-table size here; the packer uses
            # it to tell a static Submesh (0) from a skinned one before exporting
            "bones": item.get("bones", 0),
            # `materials` = the renderer's submesh count (a foreign model with
            # more slots has parts Unity never draws) and `bone_table` = the
            # target's own bones, used when a foreign mesh is exported into it.
            "materials": item.get("materials", 0),
            "bone_table": item.get("bone_table") or [],
        })
    return out


def _workspace(folder: str) -> tuple:
    """(stamp, manifest dict, submesh rows) — one `stat` per call when cached."""
    key = _pathkey(folder)
    path = os.path.join(folder, "workspace.json") if folder else ""
    stamp = _stamp(path) if path else None
    hit = _CACHE["workspace"].get(key)
    if hit is not None and hit[0] == stamp:
        return hit
    data = {}
    if path and stamp is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    rows = _manifest_rows(data) if data else []
    hit = (stamp, data, rows)
    _CACHE["workspace"][key] = hit
    _trim(_CACHE["workspace"])
    _CACHE["items"].pop(key, None)
    # texture rows/items also depend on the tree's cached format table, so they
    # are keyed by (workspace, that string) and skipped here when the file moves
    return hit


def manifest(folder: str) -> dict:
    return _workspace(folder)[1]


def submeshes(folder: str) -> list:
    """`[{index, alias, mesh, bundle, file, verts, bones}]` in the app's order.

    The app writes `index`/`alias` on every entry of `meshes` when it exports, so
    the index a slot refers to is decided **there**, once. Workspaces exported
    before that fall back to the mesh order (which is the same thing).
    """
    return _workspace(folder)[2]


def submesh_items(folder: str) -> list:
    """EnumProperty items: identifier = index as a string, label = `0. body_lod0`."""
    key = _pathkey(folder)
    stamp, _data, rows = _workspace(folder)
    hit = _CACHE["items"].get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    if not rows:
        items = [("-1", "（没有工作空间）", "")]
    else:
        items = [
            (str(row["index"]), "%d. %s" % (row["index"], row["alias"]),
             "%s · %s 顶点" % (row["mesh"], row.get("verts", 0)))
            for row in rows
        ]
    _CACHE["items"][key] = (stamp, items)
    _trim(_CACHE["items"])
    return items


def _rows(tree) -> list:
    """Submeshes of the tree's workspace, or [] when it has none."""
    folder = getattr(tree, "workspace", "") or ""
    return submeshes(folder) if folder else []


def _items(tree):
    folder = getattr(tree, "workspace", "") or ""
    return submesh_items(folder) if folder else [("-1", "（没有工作空间）", "")]


def textures(folder: str) -> list:
    """The workspace's textures with what they expect (size / format / mips).

    `workspace.json` only records file + bundle, so the format comes from the
    bundle itself; the add-on caches the answer on the tree (`texture_meta`) when
    it imports, which keeps the node drawing instant.
    """
    rows = manifest(folder).get("textures")
    return rows if isinstance(rows, list) else []


def texture_meta(tree) -> dict:
    raw = getattr(tree, "texture_meta", "") or ""
    if not raw:
        return {}
    hit = _CACHE["texture_meta"].get(raw)
    if hit is not None:
        return hit
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CACHE["texture_meta"][raw] = data
    _trim(_CACHE["texture_meta"])
    return data


def texture_rows(tree) -> tuple:
    """Textures of the tree's workspace merged with the cached format info."""
    folder = getattr(tree, "workspace", "") or ""
    stamp, data, _rows = _workspace(folder)
    key = (_pathkey(folder), stamp, getattr(tree, "texture_meta", "") or "")
    hit = _CACHE["texture_rows"].get(key)
    if hit is not None:
        return hit
    meta = texture_meta(tree)
    source = data.get("textures")
    if not isinstance(source, list):
        source = []
    rows = []
    for row in source:
        if not isinstance(row, dict):
            continue
        merged = dict(row)
        info = meta.get(row.get("name", ""))
        if isinstance(info, dict):
            merged.update(info)
        rows.append(merged)
    rows = tuple(rows)
    _CACHE["texture_rows"][key] = rows
    _trim(_CACHE["texture_rows"])
    return rows


_TEXTURE_KINDS = (
    ("_rmo", "粗糙/金属/遮挡"),
    ("_da", "漫反射+透明"),
    ("_dt", "漫反射+透明"),
    ("_d", "漫反射"),
    ("_n", "法线"),
    ("_nt", "法线"),
    ("_bt", "自发光/附加"),
    ("_e", "自发光"),
    ("_r", "粗糙度"),
    ("_m", "金属度"),
)


def texture_kind(name: str) -> str:
    """What a game texture is for, guessed from the suffix the game itself uses."""
    for suffix, kind in _TEXTURE_KINDS:
        if (name or "").endswith(suffix):
            return kind
    return "未知"


def texture_preview(tree, name: str):
    """The extracted PNG of a game texture, as an Image datablock (or None).

    Only drawn for the selected node (see `EXMTNodeTexture.draw_buttons`): loading
    a 2048² PNG and building its preview for all twenty texture nodes on every
    redraw was a large part of why the graph felt stuck, so the cache here mainly
    guarantees the image is loaded once and then reused.
    """
    folder = getattr(tree, "workspace", "") or ""
    for row in textures(folder):
        if row.get("name") != name:
            continue
        rel = row.get("file") or ""
        if not rel:
            return None
        path = os.path.join(folder, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            return None
        cached = _CACHE["images"].get(path)
        if cached is not None:
            try:
                # RNA returns a fresh wrapper on every lookup, so compare the
                # underlying datablock pointer, not the Python object
                live = bpy.data.images.get(cached.name)
                if live is not None and live.as_pointer() == cached.as_pointer():
                    return live
            except Exception:
                pass
        try:
            image = bpy.data.images.load(path, check_existing=True)
        except Exception:
            return None
        _CACHE["images"][path] = image
        _trim(_CACHE["images"])
        return image
    return None


def texture_items(tree) -> list:
    folder = getattr(tree, "workspace", "") or ""
    stamp = _workspace(folder)[0]
    key = (_pathkey(folder), stamp, getattr(tree, "texture_meta", "") or "")
    hit = _CACHE["texture_items"].get(key)
    if hit is not None:
        return hit
    rows = texture_rows(tree)
    if not rows:
        items = [("", "(no textures)", "")]
    else:
        # NB: keep enum labels ASCII.  A non-ASCII character (we used "·") makes Blender
        # 5.2 draw the enum field with a shifted glyph offset, so the field shows garbage
        # while the same text in a `label` renders fine — measured with a screen capture.
        # The Chinese kind goes in the *description* (same reason): the label stays ASCII
        # while the dropdown still says what the map is for.
        items = [
            (row.get("name", ""),
             "%s  %sx%s" % (row.get("name", "").replace("c_AlvaSSR0102_slg_", ""),
                            row.get("width", "?"), row.get("height", "?")),
             "%s  %s  %dx%d  %d mips" % (texture_kind(row.get("name", "")),
                                         row.get("format_name") or "?",
                                         row.get("width") or 0, row.get("height") or 0,
                                         row.get("mips") or 1))
            for row in rows
        ]
    _CACHE["texture_items"][key] = items
    _trim(_CACHE["texture_items"])
    return items


def _textures(tree) -> tuple:
    return texture_rows(tree)


def _texture_items(tree) -> list:
    return texture_items(tree)


# --------------------------------------------------------------------------- #
# tree
# --------------------------------------------------------------------------- #
class EXMTBlueprintTree(NodeTree):
    bl_idname = "EXMT_BlueprintTree"
    bl_label = "EXMT 蓝图"
    bl_icon = "NODETREE"

    workspace: StringProperty(
        name="工作空间", subtype="DIR_PATH",
        description="这个蓝图属于哪个工作空间（Submesh 索引由本体在「开始制作」时写进 workspace.json）",
    )
    label: StringProperty(name="名称")
    #: JSON: {texture name: {width, height, mips, format, format_name}} — filled on
    #: import by asking the core, so the texture node can show the target format
    texture_meta: StringProperty(name="贴图信息", default="")

    def submesh_rows(self) -> list:
        return _rows(self)


# --------------------------------------------------------------------------- #
# sockets
# --------------------------------------------------------------------------- #
class EXMTNodeSubmeshSocket(NodeSocket):
    bl_idname = "EXMT_NodeSubmeshSocket"
    bl_label = "网格"

    exmt_index: IntProperty(name="Submesh 索引", default=-1)

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.30, 0.72, 1.00, 1.0)


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
class EXMTNodeObject(Node):
    """One object → one submesh of the workspace."""

    bl_idname = "EXMTNodeObject"
    bl_label = "物体"
    bl_icon = "OBJECT_DATA"
    bl_width_default = 340

    object: PointerProperty(
        name="物体", type=bpy.types.Object,
        description="要放进这个 Submesh 的物体（可以是导入的游戏网格，也可以是你自己的模型）",
    )
    submesh: EnumProperty(
        name="Submesh", items=lambda self, context: _items(self.id_data),
        description="这个物体顶替哪个 Submesh（索引 + 别名，索引由本体分配）",
    )

    def init(self, context):
        self.outputs.new("EXMT_NodeSubmeshSocket", "网格")

    def draw_label(self):
        row = self._row()
        if row is None:
            return "物体"
        return "%d. %s" % (row["index"], row["alias"])

    def _row(self):
        # Read the enum *once*: a dynamic EnumProperty re-evaluates its items
        # callback on every read (measured ~0.017 ms), so reading it inside this
        # loop made a label cost 20× more than the loop itself.
        wanted = str(self.submesh)
        for row in _rows(self.id_data):
            if str(row["index"]) == wanted:
                return row
        return None

    def draw_buttons(self, context, layout):
        layout.prop(self, "object", text="")
        layout.prop(self, "submesh", text="")
        # No mesh name / vertex count / link counters here: the node editor draws
        # every node on every mouse move, and the graph is only *parsed* when
        # 「生成 Mod」 runs.  A label that repeats what the header already says is
        # pure redraw cost.
        if self.object is not None and str(self.submesh) in ("", "-1"):
            layout.label(text="先选一个 Submesh", icon="ERROR")
        if self.object is not None and self.object.type != "MESH":
            layout.label(text="不是网格物体", icon="ERROR")


class EXMTNodeGenerate(Node):
    """Collects whatever is plugged in and writes the Mod.

    Its inputs mean **nothing**: which submesh a pack replaces comes from each
    `物体` node's own `submesh`, so an object node that is only there for
    reference (checking for clipping, say) is simply left unplugged and never
    packed. A `分组` node can stand between them to keep the graph readable.
    """

    bl_idname = "EXMTNodeGenerate"
    bl_label = "生成 Mod"
    bl_icon = "PACKAGE"
    bl_width_default = 330

    #: live progress while a run is going ("编码贴图 3/12"), drawn on the node so it
    #: is visible where the user is looking (the status bar is a thin strip)
    progress_text: StringProperty(name="进度", default="")

    def init(self, context):
        self.ensure_slots(count=4)

    def draw_label(self):
        if self.progress_text:
            return "生成 Mod · %s" % self.progress_text
        # Deliberately static: counting sockets here walked the graph on every
        # redraw.  The run reports what it actually packed.
        return "生成 Mod"

    def ensure_slots(self, spare: int = 1, count: int = 1):
        """Keep `count` free inputs available.

        Called with `count=1` after a link is made (one spare is enough to keep
        wiring) and with `count=4` from the 「＋ 输入口」 button, so pressing it when
        only one slot is free really does add three more. Extra free sockets are
        never trimmed: someone wiring ten objects in one go wants ten empty inputs
        waiting, and cutting them back made the button look like it did nothing.
        """
        want = max(int(spare), int(count))
        free = len([s for s in self.inputs if not s.is_linked])
        for _ in range(max(0, want - free)):
            self.inputs.new("EXMT_NodeSubmeshSocket", "网格 %d" % (len(self.inputs) + 1))

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.operator("exmt.generate_mod", text="生成 Mod", icon="PLAY").blueprint = self.id_data.name
        row.operator("exmt.blueprint_sync", text="＋ 输入口", icon="ADD")
        if not (getattr(self.id_data, "workspace", "") or ""):
            layout.label(text="蓝图没有工作空间", icon="ERROR")


class EXMTNodeGroup(Node):
    """Only grouping: `物体` → 分组 → `生成 Mod`, so a dozen objects stay tidy."""

    bl_idname = "EXMTNodeGroup"
    bl_label = "分组"
    bl_icon = "GROUP"
    bl_width_default = 200

    def init(self, context):
        for index in range(4):
            self.inputs.new("EXMT_NodeSubmeshSocket", "网格 %d" % (index + 1))
        self.outputs.new("EXMT_NodeSubmeshSocket", "网格")

    def draw_label(self):
        return "分组"

    def ensure_slots(self, spare: int = 1, count: int = 1):
        """Keep `count` free inputs available (see `EXMTNodeGenerate.ensure_slots`)."""
        want = max(int(spare), int(count))
        free = len([s for s in self.inputs if not s.is_linked])
        for _ in range(max(0, want - free)):
            self.inputs.new("EXMT_NodeSubmeshSocket", "网格 %d" % (len(self.inputs) + 1))

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.operator("exmt.generate_mod", text="生成 Mod", icon="PLAY").blueprint = self.id_data.name
        row.operator("exmt.blueprint_sync", text="＋ 输入口", icon="ADD")


class EXMTNodeTexture(Node):
    """One game texture; pick an image and it replaces it in the target format."""

    bl_idname = "EXMTNodeTexture"
    bl_label = "贴图"
    bl_icon = "TEXTURE"
    bl_width_default = 360

    texture: EnumProperty(
        name="贴图", items=lambda self, context: _texture_items(self.id_data),
        description="要替换的游戏贴图（名称、尺寸、格式都由本体导出的工作空间决定）",
    )
    image: PointerProperty(
        name="替换为", type=bpy.types.Image,
        description="用这张图替换上面的贴图；生成 Mod 时会自动转成目标贴图的格式和尺寸",
    )

    def draw_label(self):
        row = self._row()
        return (row or {}).get("name", "贴图").replace("c_AlvaSSR0102_slg_", "")

    def _row(self):
        # See `EXMTNodeObject._row`: one enum read, not one per texture row.
        wanted = self.texture
        for row in _textures(self.id_data):
            if row.get("name") == wanted:
                return row
        return None

    def draw_buttons(self, context, layout):
        layout.prop(self, "texture", text="")
        row = self._row()
        if row is None:
            layout.label(text="先选一张贴图", icon="ERROR")
            return
        info = "%s · %dx%d · %s · %d mips" % (
            texture_kind(row.get("name", "")), row.get("width") or 0, row.get("height") or 0,
            row.get("format_name") or "?", row.get("mips") or 1)
        layout.label(text=info, icon="INFO" if row.get("supported", True) else "ERROR")

        # The map about to be replaced, so nobody has to recognise the name by
        # heart.  Only for the *active* node: a preview decodes a full-resolution
        # PNG, and drawing one for all twenty texture nodes on every redraw was a
        # large slice of the node editor's lag.  `self.select` is not enough —
        # box-selecting the whole texture frame would bring them all back.
        active = getattr(self.id_data.nodes, "active", None)
        if active is not None and active.as_pointer() == self.as_pointer():
            preview = texture_preview(self.id_data, self.texture)
            if preview is not None:
                try:
                    layout.template_preview(preview)
                except Exception:
                    # a preview that cannot draw must never take the node down with it
                    layout.label(text="（这张贴图没有预览）", icon="IMAGE_DATA")

        # `template_ID` gives the image field a browse button (opens the file
        # selector) instead of only the datablock dropdown
        layout.template_ID(self, "image", open="image.open", text="")
        if self.image is None:
            layout.label(text="未选择替换图（不替换）", icon="INFO")
        elif row.get("supported", True):
            layout.label(text="生成时自动转成 %s" % row.get("format_name"), icon="FILE_REFRESH")


def walk_objects(node, seen=None) -> list:
    """Object nodes reachable from `node` (through any 分组 node)."""
    seen = seen or set()
    if node.bl_idname in seen:
        return []
    seen.add(node.bl_idname + str(node.as_pointer()))
    found = []
    for socket in node.inputs:
        for link in socket.links:
            source = link.from_node
            if source.bl_idname == "EXMTNodeObject":
                found.append(source)
            elif source.bl_idname == "EXMTNodeGroup":
                found.extend(walk_objects(source, seen))
    return found


def wired_objects(generate_node) -> list:
    return walk_objects(generate_node)


# `exmt_index` lives on the socket class itself (declared above): assigning a
# Property after class creation does not register it.


# --------------------------------------------------------------------------- #
# operators (creation / editing / slot sync)
# --------------------------------------------------------------------------- #
def _edited_tree(context):
    """The EXMT tree this node editor is showing, if any.

    4.x exposes `edit_tree`; 5.2 made it read-only and added `node_tree`.  Reading
    either is fine, so try both instead of guessing the Blender version.
    """
    space = getattr(context, "space_data", None)
    if space is None:
        return None
    for attr in ("edit_tree", "node_tree"):
        tree = getattr(space, attr, None)
        if isinstance(tree, EXMTBlueprintTree):
            return tree
    return None


def active_blueprint(context):
    tree = _edited_tree(context)
    if tree is not None:
        return tree
    scene = getattr(context, "scene", None)
    tree = getattr(scene, "exmt_blueprint", None) if scene else None
    if isinstance(tree, EXMTBlueprintTree):
        return tree
    return bpy.data.node_groups.get("EXMT 蓝图") if "EXMT 蓝图" in bpy.data.node_groups else None


def new_blueprint(workspace: str, label: str) -> EXMTBlueprintTree:
    """Get (or create) the blueprint of a workspace.

    An existing blueprint for the same workspace is **reused, never deleted**: it
    holds the user's wiring and their picked replacement textures, so re-importing
    has to add to it.  「删除蓝图」 is the explicit way to throw it away.
    """
    name = "EXMT %s" % (label or os.path.basename(workspace) or "蓝图")
    existing = bpy.data.node_groups.get(name)
    if isinstance(existing, EXMTBlueprintTree):
        existing.workspace = workspace
        existing.label = label
        if not any(node.bl_idname == "EXMTNodeGenerate" for node in existing.nodes):
            generate = existing.nodes.new("EXMTNodeGenerate")
            generate.location = (560, -80)
            generate.ensure_slots(count=4)
        return existing
    tree = bpy.data.node_groups.new(name, "EXMT_BlueprintTree")
    tree.workspace = workspace
    tree.label = label
    generate = tree.nodes.new("EXMTNodeGenerate")
    generate.location = (560, -80)
    generate.ensure_slots(count=4)
    return tree


class EXMT_OT_blueprint_new(Operator):
    bl_idname = "exmt.blueprint_new"
    bl_label = "新建蓝图"
    bl_description = "为当前工作空间新建一个空白蓝图"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from . import _active_workspace_info

        info, _source = _active_workspace_info()
        folder = (info or {}).get("dir") or ""
        if not folder:
            self.report({"ERROR"}, "先在 App 模型页选一个工作空间")
            return {"CANCELLED"}
        label = (info or {}).get("label") or os.path.basename(folder)
        tree = new_blueprint(folder, label)
        context.scene.exmt_blueprint = tree
        self.report({"INFO"}, "已新建蓝图：%s" % tree.name)
        return {"FINISHED"}


class EXMT_OT_blueprint_delete(Operator):
    bl_idname = "exmt.blueprint_delete"
    bl_label = "删除蓝图"
    bl_description = "删掉当前选择的蓝图（不影响已导入的网格）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        tree = getattr(context.scene, "exmt_blueprint", None)
        if tree is None:
            self.report({"ERROR"}, "没有选中蓝图")
            return {"CANCELLED"}
        name = tree.name
        if context.scene.exmt_blueprint == tree:
            context.scene.exmt_blueprint = None
        bpy.data.node_groups.remove(tree)
        self.report({"INFO"}, "已删除蓝图：%s" % name)
        return {"FINISHED"}


class EXMT_OT_blueprint_open(Operator):
    bl_idname = "exmt.blueprint_open"
    bl_label = "在节点编辑器里打开"
    bl_description = "把当前区域（或最大的区域）切成节点编辑器并显示这个蓝图"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        tree = getattr(context.scene, "exmt_blueprint", None)
        if tree is None:
            self.report({"ERROR"}, "没有选中蓝图")
            return {"CANCELLED"}
        area = context.area
        if area is None or area.type == "VIEW_3D":
            # called from the 3D view sidebar: take the biggest non-sidebar area
            areas = [a for a in context.screen.areas if a.type in {"VIEW_3D", "OUTLINER"}]
            area = max(areas, key=lambda a: a.width * a.height) if areas else area
        if area is None:
            self.report({"ERROR"}, "找不到可切换的区域")
            return {"CANCELLED"}
        area.type = "NODE_EDITOR"
        for space in area.spaces:
            if space.type != "NODE_EDITOR":
                continue
            # Blender 4.x exposes `ui_type`, 5.x dropped it in favour of a writable
            # `tree_type`; set whichever this build has, then bind the tree
            if hasattr(space, "ui_type"):
                try:
                    space.ui_type = EXMTBlueprintTree.bl_idname
                except Exception:
                    pass
            if hasattr(space, "tree_type"):
                try:
                    space.tree_type = EXMTBlueprintTree.bl_idname
                except Exception:
                    pass
            space.pin = False
            # 5.2 makes `edit_tree`/`id` read-only and exposes the writable
            # `node_tree` instead; 4.x only knows `edit_tree`
            for attr in ("node_tree", "edit_tree"):
                if not hasattr(space, attr):
                    continue
                try:
                    setattr(space, attr, tree)
                    if getattr(space, "edit_tree", None) == tree:
                        return {"FINISHED"}
                except Exception:
                    continue
            self.report({"ERROR"}, "这个区域切不成节点编辑器")
            return {"CANCELLED"}


class EXMT_OT_blueprint_sync(Operator):
    bl_idname = "exmt.blueprint_sync"
    bl_label = "补输入口"
    bl_description = "给「生成 Mod」和「分组」节点补出 4 个空输入口，方便一次连好几个物体"
    bl_options = {"REGISTER", "UNDO"}

    count: IntProperty(name="补几个", default=4, min=1, max=32)

    def execute(self, context):
        tree = _edited_tree(context)
        if tree is None:
            tree = getattr(context.scene, "exmt_blueprint", None)
        if tree is None:
            self.report({"ERROR"}, "没有选中蓝图")
            return {"CANCELLED"}
        touched = 0
        added = 0
        for node in tree.nodes:
            if node.bl_idname not in {"EXMTNodeGenerate", "EXMTNodeGroup"}:
                continue
            before = len(node.inputs)
            node.ensure_slots(count=self.count)
            added += len(node.inputs) - before
            touched += 1
        self.report({"INFO"}, "补了 %d 个输入口（%d 个节点）" % (added, touched))
        return {"FINISHED"}


class EXMT_OT_node_add(Operator):
    """Context-menu helper: right-click in the node editor to add a node."""

    bl_idname = "exmt.node_add"
    bl_label = "新建节点"
    bl_description = "在当前蓝图里新建一个节点"
    bl_options = {"REGISTER", "UNDO"}

    kind: StringProperty(default="object")

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = _edited_tree(context)
        if tree is None:
            tree = getattr(context.scene, "exmt_blueprint", None)
        if tree is None:
            self.report({"ERROR"}, "没有选中蓝图")
            return {"CANCELLED"}
        idname = {
            "object": "EXMTNodeObject",
            "group": "EXMTNodeGroup",
            "texture": "EXMTNodeTexture",
            "generate": "EXMTNodeGenerate",
        }.get(self.kind, "EXMTNodeObject")

        # Drop it under the mouse (the node editor keeps `cursor_location` current
        # in tree space).  The old code stacked new nodes below the lowest one in
        # the graph, which put them metres away from the cursor — the right-click
        # menu is a pointer verb, so the node has to appear where you clicked.
        where = None
        if space is not None and getattr(space, "type", "") == "NODE_EDITOR":
            try:
                where = tuple(space.cursor_location)
            except Exception:
                where = None

        node = tree.nodes.new(idname)
        if where is None:
            # no node-editor context (search menu / script): park it left of the
            # generate node instead of at the bottom of a possibly huge graph
            generate = next((n for n in tree.nodes if n.bl_idname == "EXMTNodeGenerate"), None)
            where = ((generate.location.x - 420.0, generate.location.y)
                     if generate is not None else (0.0, 0.0))
        node.location = where
        # RNA wrappers are recreated per access, so compare pointers — `other is
        # not node` is always true and would deselect the node we just made.
        pointer = node.as_pointer()
        for other in tree.nodes:
            if other.as_pointer() != pointer:
                other.select = False
        node.select = True
        tree.nodes.active = node
        if node.bl_idname in {"EXMTNodeGenerate", "EXMTNodeGroup"}:
            node.ensure_slots(count=4)
        return {"FINISHED"}


def _menu_add(self, context):
    """Right-click / Add menu entries."""
    self.layout.separator()
    self.layout.operator("exmt.node_add", text="EXMT 物体节点", icon="OBJECT_DATA").kind = "object"
    self.layout.operator("exmt.node_add", text="EXMT 分组节点", icon="GROUP").kind = "group"
    self.layout.operator("exmt.node_add", text="EXMT 贴图节点", icon="TEXTURE").kind = "texture"
    self.layout.operator("exmt.node_add", text="EXMT 生成 Mod 节点", icon="PACKAGE").kind = "generate"


CLASSES = (
    EXMTBlueprintTree,
    EXMTNodeSubmeshSocket,
    EXMTNodeObject,
    EXMTNodeGroup,
    EXMTNodeTexture,
    EXMTNodeGenerate,
    EXMT_OT_blueprint_new,
    EXMT_OT_blueprint_delete,
    EXMT_OT_blueprint_open,
    EXMT_OT_blueprint_sync,
    EXMT_OT_node_add,
)


def register_blueprint():
    for cls in CLASSES:
        register_class(cls)
    # scene pointer so the panel can show/pick the active blueprint
    bpy.types.Scene.exmt_blueprint = PointerProperty(
        type=EXMTBlueprintTree, name="蓝图",
        description="面板里选中的蓝图；导入工作空间时会新建一个",
    )
    # Blender's node editor menus differ between versions: hook whatever exists
    for name in ("NODE_MT_context_menu", "NODE_MT_add"):
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.append(_menu_add)


def unregister_blueprint():
    for name in ("NODE_MT_context_menu", "NODE_MT_add"):
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            try:
                menu.remove(_menu_add)
            except Exception:
                pass
    if hasattr(bpy.types.Scene, "exmt_blueprint"):
        del bpy.types.Scene.exmt_blueprint
    for cls in reversed(CLASSES):
        unregister_class(cls)
