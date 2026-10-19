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
# the workspace's submesh list (written by the app when it exports)
# --------------------------------------------------------------------------- #
def manifest(folder: str) -> dict:
    path = os.path.join(folder or "", "workspace.json")
    if not folder or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def submeshes(folder: str) -> list:
    """`[{index, alias, mesh, bundle, file, verts}]` in the app's order.

    The app writes `index`/`alias` on every entry of `meshes` when it exports, so
    the index a slot refers to is decided **there**, once. Workspaces exported
    before that fall back to the mesh order (which is the same thing).
    """
    data = manifest(folder)
    rows = data.get("submeshes")
    if rows:
        return rows
    out = []
    for position, item in enumerate(data.get("meshes", [])):
        mesh_name = item.get("mesh", "")
        short = re.sub(r"^c_[A-Za-z0-9]+_(?:slg_)?", "", mesh_name) or mesh_name
        out.append({
            "index": int(item.get("index", position)),
            "alias": item.get("alias") or short,
            "file": item.get("file", ""),
            "mesh": mesh_name,
            "bundle": item.get("bundle", ""),
            "verts": item.get("verts", 0),
        })
    return out


def submesh_items(folder: str) -> list:
    """EnumProperty items: identifier = index as a string, label = `0. body_lod0`."""
    rows = submeshes(folder)
    if not rows:
        return [("-1", "（没有工作空间）", "")]
    return [
        (str(row["index"]), "%d. %s" % (row["index"], row["alias"]),
         "%s · %s 顶点" % (row["mesh"], row.get("verts", 0)))
        for row in rows
    ]


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
    return manifest(folder).get("textures") or []


def texture_meta(tree) -> dict:
    try:
        return json.loads(getattr(tree, "texture_meta", "") or "{}")
    except Exception:
        return {}


def texture_rows(tree) -> list:
    """Textures of the tree's workspace merged with the cached format info."""
    meta = texture_meta(tree)
    rows = []
    for row in textures(getattr(tree, "workspace", "") or ""):
        merged = dict(row)
        merged.update(meta.get(row.get("name", ""), {}))
        rows.append(merged)
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

    `workspace.json` already records `textures[].file`, so a node can show the map
    it is about to replace instead of asking the user to recognise
    `c_VepleySSR01_slg_cloth01_d` by heart.
    """
    for row in textures(getattr(tree, "workspace", "") or ""):
        if row.get("name") != name:
            continue
        rel = row.get("file") or ""
        if not rel:
            return None
        path = os.path.join(getattr(tree, "workspace", "") or "", rel.replace("/", os.sep))
        if not os.path.isfile(path):
            return None
        try:
            return bpy.data.images.load(path, check_existing=True)
        except Exception:
            return None
    return None


def texture_items(tree) -> list:
    rows = texture_rows(tree)
    if not rows:
        return [("", "(no textures)", "")]
    # NB: keep enum labels ASCII.  A non-ASCII character (we used "·") makes Blender
    # 5.2 draw the enum field with a shifted glyph offset, so the field shows garbage
    # while the same text in a `label` renders fine — measured with a screen capture.
    # The Chinese kind goes in the *description* (same reason): the label stays ASCII
    # while the dropdown still says what the map is for.
    return [
        (row.get("name", ""),
         "%s  %sx%s" % (row.get("name", "").replace("c_AlvaSSR0102_slg_", ""),
                        row.get("width", "?"), row.get("height", "?")),
         "%s  %s  %dx%d  %d mips" % (texture_kind(row.get("name", "")),
                                     row.get("format_name") or "?", row.get("width", 0),
                                     row.get("height", 0), row.get("mips", 1)))
        for row in rows
    ]


def _textures(tree) -> list:
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
        for row in _rows(self.id_data):
            if str(row["index"]) == str(self.submesh):
                return row
        return None

    def draw_buttons(self, context, layout):
        layout.prop(self, "object", text="")
        layout.prop(self, "submesh", text="")
        row = self._row()
        if row is None:
            layout.label(text="先选一个 Submesh", icon="ERROR")
        else:
            layout.label(text="%s · %s 顶点" % (row["mesh"], row.get("verts", 0)), icon="INFO")
        if self.object is not None and row is not None and self.object.type != "MESH":
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
        return "生成 Mod（%d 条连线）" % linked_count(self)

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
        rows = _rows(self.id_data)
        if not rows:
            layout.label(text="蓝图没有工作空间", icon="ERROR")
            return
        objects = wired_objects(self)
        lines = len([s for s in self.inputs if s.is_linked])
        layout.label(text="%d 条连线 · %d 个物体 → %d 个 Submesh" % (lines, len(objects), len(rows)),
                     icon="LINKED")
        missing = [s for s in self.inputs if s.is_linked and not socket_ok(s)]
        if missing:
            layout.label(text="%d 条连线没指定 Submesh" % len(missing), icon="ERROR")
        row = layout.row(align=True)
        row.operator("exmt.generate_mod", text="生成 Mod", icon="PLAY").blueprint = self.id_data.name
        row.operator("exmt.blueprint_sync", text="＋ 输入口", icon="ADD")


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
        return "分组（%d 条连线）" % linked_count(self)

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
        if not self.outputs[0].links:
            layout.label(text="还没连到「生成 Mod」", icon="INFO")


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
        for row in _textures(self.id_data):
            if row.get("name") == self.texture:
                return row
        return None

    def draw_buttons(self, context, layout):
        layout.prop(self, "texture", text="")
        row = self._row()
        if row is None:
            layout.label(text="先选一张贴图", icon="ERROR")
            return
        info = "%s · %dx%d · %s · %d mips" % (
            texture_kind(row.get("name", "")), row.get("width", 0), row.get("height", 0),
            row.get("format_name") or "?", row.get("mips", 1))
        layout.label(text=info, icon="INFO" if row.get("supported", True) else "ERROR")

        # the map about to be replaced, so nobody has to recognise the name
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


def linked_count(node) -> int:
    return len([s for s in node.inputs if s.is_linked])


def _source_submesh(socket):
    """The object node behind a link, if it has a usable submesh."""
    if not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node.bl_idname != "EXMTNodeObject":
        return None
    index = str(getattr(node, "submesh", "-1"))
    if index == "-1" or getattr(node, "object", None) is None:
        return None
    return node


def socket_objects(socket) -> list:
    """Object nodes behind one input, looking through `分组` nodes.

    `_source_submesh` only sees a *direct* object node, so a link that arrives via
    a 分组 used to be reported as "没指定 Submesh" even though packing resolved it
    fine — the warning must walk the group or it cries wolf on the normal layout.
    """
    if not socket.is_linked:
        return []
    source = socket.links[0].from_node
    if source.bl_idname == "EXMTNodeObject":
        return [source]
    if source.bl_idname == "EXMTNodeGroup":
        return walk_objects(source)
    return []


def socket_ok(socket) -> bool:
    """True when everything behind this input has an object and a submesh."""
    objects = socket_objects(socket)
    if not objects:
        return False
    for node in objects:
        if getattr(node, "object", None) is None:
            return False
        if str(getattr(node, "submesh", "-1")) == "-1":
            return False
    return True


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
def active_blueprint(context):
    tree = getattr(context.space_data, "edit_tree", None) if context.space_data else None
    if isinstance(tree, EXMTBlueprintTree):
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
        tree = context.space_data.edit_tree if context.space_data else None
        if not isinstance(tree, EXMTBlueprintTree):
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
        tree = context.space_data.edit_tree if context.space_data else None
        if not isinstance(tree, EXMTBlueprintTree):
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
        node = tree.nodes.new(idname)
        # place it below the last node so repeated calls do not stack
        lowest = min((n.location.y for n in tree.nodes if n is not node), default=0.0)
        node.location = (0.0, lowest - 240.0)
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
