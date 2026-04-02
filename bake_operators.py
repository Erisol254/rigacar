# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License...
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

bl_info = {
    "name": "Rigacar Baking Tools (4.5 Final Master)",
    "author": "Rigacar + AI Compatibility Patch",
    "version": (6, 3, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Car",
    "description": "Full feature restoration including interactive Clear tools for 4.5 LTS.",
    "category": "Animation",
}

import bpy
import bpy_extras.anim_utils
import mathutils
import math
import itertools
import re

# --- BLENDER 4.5 COMPATIBILITY WRAPPER ---
class RigacarBakeOptions:
    def __init__(self):
        self.only_selected = True
        self.do_pose = True
        self.do_object = False
        self.do_location = True
        self.do_rotation = True
        self.do_scale = True
        self.do_bbone = False 
        self.do_visual_keying = True
        self.do_constraint_clear = False
        self.do_parents_clear = False
        self.do_clean = False
        self.do_custom_props = False
        self.use_current_action = True

# --- UTILITIES ---

def cursor(cursor_mode):
    def cursor_decorator(func):
        def wrapper(self, context, *args, **kwargs):
            win = context.window
            if win: win.cursor_modal_set(cursor_mode)
            try:
                return func(self, context, *args, **kwargs)
            finally:
                if win: win.cursor_modal_restore()
        return wrapper
    return cursor_decorator

def bone_name(prefix, position, side, index=0):
    if index == 0: return '%s.%s.%s' % (prefix, position, side)
    return '%s.%s.%s.%03d' % (prefix, position, side, index)

def bone_range(bones, name_prefix, position, side):
    for index in itertools.count():
        name = bone_name(name_prefix, position, side, index)
        if name in bones: yield bones[name]
        else: break

def find_wheelbrake_bone(bones, position, side, index):
    other_side = 'R' if side == 'L' else 'L'
    name_prefix = 'WheelBrake'
    bone = bones.get(bone_name(name_prefix, position, side, index))
    if bone: return bone
    bone = bones.get(bone_name(name_prefix, position, other_side, index))
    if bone: return bone
    backward_compatible_bone_name = '%s Wheels' % ('Front' if position == 'Ft' else 'Back')
    return bones.get(backward_compatible_bone_name)

def clear_property_animation(context, property_name, remove_keyframes=True):
    obj = context.object
    if remove_keyframes and obj.animation_data and obj.animation_data.action:
        fcurve_datapath = '["%s"]' % property_name
        action = obj.animation_data.action
        fcurve = action.fcurves.find(fcurve_datapath)
        if fcurve: action.fcurves.remove(fcurve)
    obj[property_name] = .0

def create_property_animation(context, property_name):
    obj = context.object
    if obj.animation_data is None: obj.animation_data_create()
    if obj.animation_data.action is None:
        obj.animation_data.action = bpy.data.actions.new(f"{obj.name}Action")
    action = obj.animation_data.action
    fcurve_datapath = f'["{property_name}"]'
    fc = action.fcurves.find(fcurve_datapath)
    if fc is None:
        fc = action.fcurves.new(fcurve_datapath, index=0, action_group='Wheels rotation')
    return fc

def fix_old_steering_rotation(rig_object):
    if rig_object.pose and rig_object.pose.bones:
        if 'MCH-Steering.rotation' in rig_object.pose.bones:
            rig_object.pose.bones['MCH-Steering.rotation'].rotation_mode = 'QUATERNION'

# --- EVALUATION ---

class FCurvesEvaluator(object):
    def __init__(self, fcurves, default_value):
        self.default_value = default_value
        self.fcurves = fcurves
    def evaluate(self, f):
        result = []
        for fcurve, value in zip(self.fcurves, self.default_value):
            result.append(fcurve.evaluate(f) if fcurve is not None else value)
        return result

class VectorFCurvesEvaluator(object):
    def __init__(self, eval): self.eval = eval
    def evaluate(self, f): return mathutils.Vector(self.eval.evaluate(f))

class EulerToQuaternionFCurvesEvaluator(object):
    def __init__(self, eval): self.eval = eval
    def evaluate(self, f): return mathutils.Euler(self.eval.evaluate(f)).to_quaternion()

class QuaternionFCurvesEvaluator(object):
    def __init__(self, eval): self.eval = eval
    def evaluate(self, f): return mathutils.Quaternion(self.eval.evaluate(f))

# --- BAKING BASE ---

class BakingOperator(object):
    frame_start: bpy.props.IntProperty(name='Start Frame', min=1)
    frame_end: bpy.props.IntProperty(name='End Frame', min=1)
    keyframe_tolerance: bpy.props.FloatProperty(name='Keyframe tolerance', min=0, default=.01)

    @classmethod
    def poll(cls, context):
        return (context.object and 'Car Rig' in context.object.data and 
                context.object.mode in ('POSE', 'OBJECT'))

    def invoke(self, context, event):
        if context.object.animation_data and context.object.animation_data.action:
            action = context.object.animation_data.action
            self.frame_start = int(action.frame_range[0])
            self.frame_end = int(action.frame_range[1])
        else:
            self.frame_start = context.scene.frame_start
            self.frame_end = context.scene.frame_end
        return context.window_manager.invoke_props_dialog(self)

    def _bake_action(self, context, *source_bones):
        obj = context.object
        prev_mode = obj.mode
        bpy.ops.object.mode_set(mode='POSE')
        for pb in obj.pose.bones: pb.bone.select = False
        for bone in source_bones:
            if bone.name in obj.pose.bones: obj.pose.bones[bone.name].bone.select = True
        
        baked_action = bpy_extras.anim_utils.bake_action(
            obj,
            action=obj.animation_data.action if (obj.animation_data and obj.animation_data.action) else None,
            frames=range(self.frame_start, self.frame_end + 1),
            bake_options=RigacarBakeOptions()
        )
        bpy.ops.object.mode_set(mode=prev_mode)
        return baked_action

# --- MAIN OPERATORS ---

class ANIM_OT_carWheelsRotationBake(bpy.types.Operator, BakingOperator):
    bl_idname = 'anim.car_wheels_rotation_bake'
    bl_label = 'Bake wheels rotation'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        self._bake_wheels_rotation(context)
        return {'FINISHED'}

    @cursor('WAIT')
    def _bake_wheels_rotation(self, context):
        bones = context.object.data.bones
        wheel_bones, brake_bones = [], []
        for position, side in itertools.product(('Ft', 'Bk'), ('L', 'R')):
            for index, wheel_bone in enumerate(bone_range(bones, 'MCH-Wheel.rotation', position, side)):
                wheel_bones.append(wheel_bone)
                brake_bones.append(find_wheelbrake_bone(bones, position, side, index) or wheel_bone)

        for prop_name in [b.name.replace('MCH-', '') for b in wheel_bones]:
            clear_property_animation(context, prop_name)

        baked_action = self._bake_action(context, *set(wheel_bones + brake_bones))
        if baked_action is None: return

        try:
            for wb, bb in zip(wheel_bones, brake_bones):
                fc = create_property_animation(context, wb.name.replace('MCH-', ''))
                l_ev = VectorFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{wb.name}"].location', index=i) for i in range(3)], (.0, .0, .0)))
                r_ev = EulerToQuaternionFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{wb.name}"].rotation_euler', index=i) for i in range(3)], (.0, .0, .0)))
                s_ev = VectorFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{bb.name}"].scale', index=i) for i in range(3)], (1.0, 1.0, 1.0)))
                
                radius, dist = wb.length if wb.length > 0 else 1.0, 0
                init_v = (wb.head_local - wb.tail_local).normalized()
                prev_p = l_ev.evaluate(self.frame_start)

                for f in range(self.frame_start + 1, self.frame_end + 1):
                    p = l_ev.evaluate(f)
                    speed = math.copysign((p - prev_p).magnitude, (r_ev.evaluate(f) @ init_v).dot((p - prev_p))) * (2 * s_ev.evaluate(f).y - 1)
                    dist += speed / radius
                    fc.keyframe_points.insert(f, dist).interpolation = 'LINEAR'
                    prev_p = p
        finally:
            if baked_action != context.object.animation_data.action: bpy.data.actions.remove(baked_action)

class ANIM_OT_carSteeringBake(bpy.types.Operator, BakingOperator):
    bl_idname = 'anim.car_steering_bake'
    bl_label = 'Bake car steering'
    bl_options = {'REGISTER', 'UNDO'}
    rotation_factor: bpy.props.FloatProperty(name='Rotation factor', min=.1, default=1)

    def execute(self, context):
        if 'Steering' in context.object.data.bones and 'MCH-Steering.rotation' in context.object.data.bones:
            steering = context.object.data.bones['Steering']
            mch = context.object.data.bones['MCH-Steering.rotation']
            self._bake_steering_rotation(context, abs(steering.head_local.y - mch.head_local.y), mch)
        return {'FINISHED'}

    @cursor('WAIT')
    def _bake_steering_rotation(self, context, bone_offset, bone):
        clear_property_animation(context, 'Steering.rotation')
        fix_old_steering_rotation(context.object)
        fc = create_property_animation(context, 'Steering.rotation')
        baked_action = self._bake_action(context, bone)
        if baked_action is None: return

        try:
            l_ev = VectorFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{bone.name}"].location', index=i) for i in range(3)], (.0, .0, .0)))
            r_ev = QuaternionFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{bone.name}"].rotation_quaternion', index=i) for i in range(4)], (1.0, .0, .0, .0)))
            dir_v, curr_p = (bone.head_local - bone.tail_local).normalized(), l_ev.evaluate(self.frame_start)

            for f in range(self.frame_start, self.frame_end):
                next_p = l_ev.evaluate(f + 1)
                s_dir = next_p - curr_p
                if s_dir.length_squared > 0.0001:
                    q = r_ev.evaluate(f)
                    w_d, w_n = q @ dir_v, q @ mathutils.Vector((1, 0, 0))
                    proj = s_dir.dot(w_d)
                    if proj != 0:
                        val = mathutils.geometry.distance_point_to_plane(s_dir * (bone_offset * self.rotation_factor / proj), w_d, w_n)
                        fc.keyframe_points.insert(f, val).interpolation = 'LINEAR'
                curr_p = next_p
        finally:
            if baked_action != context.object.animation_data.action: bpy.data.actions.remove(baked_action)

# --- RESTORED INTERACTIVE CLEAR OPERATOR ---

class ANIM_OT_carClearSteeringWheelsRotation(bpy.types.Operator):
    bl_idname = "anim.car_clear_steering_wheels_rotation"
    bl_label = "Clear baked animation"
    bl_description = "Clear generated rotation for steering and wheels"
    bl_options = {'REGISTER', 'UNDO'}

    clear_steering: bpy.props.BoolProperty(name="Steering", default=True)
    clear_wheels: bpy.props.BoolProperty(name="Wheels", default=True)

    def draw(self, context):
        layout = self.layout
        layout.label(text='Clear generated keyframes for')
        layout.prop(self, 'clear_steering')
        layout.prop(self, 'clear_wheels')

    @classmethod
    def poll(cls, context):
        return context.object and context.object.data and context.object.data.get('Car Rig')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        re_wheel = re.compile(r'^Wheel\.rotation\.(Ft|Bk)\.[LR](\.\d+)?$')
        for prop in list(context.object.keys()):
            if prop == 'Steering.rotation':
                clear_property_animation(context, prop, remove_keyframes=self.clear_steering)
            elif re_wheel.match(prop):
                clear_property_animation(context, prop, remove_keyframes=self.clear_wheels)
        
        # Refresh 4.5 Depsgraph
        mode = context.object.mode
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode=mode)
        return {'FINISHED'}

# --- UI ---

class VIEW3D_PT_RigacarPanel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Car'
    bl_label = 'Rigacar Baking'

    def draw(self, context):
        layout = self.layout
        if context.object and 'Car Rig' in context.object.data:
            col = layout.column(align=True)
            col.operator("anim.car_wheels_rotation_bake", icon='ORIENTATION_GIMBAL')
            col.operator("anim.car_steering_bake", icon='AUTOMERGE_ON')
            layout.separator()
            layout.operator("anim.car_clear_steering_wheels_rotation", icon='X', text="Clear Baked...")

# --- REG ---

classes = (ANIM_OT_carWheelsRotationBake, ANIM_OT_carSteeringBake, 
           ANIM_OT_carClearSteeringWheelsRotation, VIEW3D_PT_RigacarPanel)

def register():
    for cls in classes: bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()

bl_info = {
    "name": "Rigacar Baking Tools (4.5 Final Master)",
    "author": "Rigacar + AI Compatibility Patch",
    "version": (6, 3, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Car",
    "description": "Full feature restoration including interactive Clear tools for 4.5 LTS.",
    "category": "Animation",
}

import bpy
import bpy_extras.anim_utils
import mathutils
import math
import itertools
import re

# --- BLENDER 4.5 COMPATIBILITY WRAPPER ---
class RigacarBakeOptions:
    def __init__(self):
        self.only_selected = True
        self.do_pose = True
        self.do_object = False
        self.do_location = True
        self.do_rotation = True
        self.do_scale = True
        self.do_bbone = False 
        self.do_visual_keying = True
        self.do_constraint_clear = False
        self.do_parents_clear = False
        self.do_clean = False
        self.do_custom_props = False
        self.use_current_action = True

# --- UTILITIES ---

def cursor(cursor_mode):
    def cursor_decorator(func):
        def wrapper(self, context, *args, **kwargs):
            win = context.window
            if win: win.cursor_modal_set(cursor_mode)
            try:
                return func(self, context, *args, **kwargs)
            finally:
                if win: win.cursor_modal_restore()
        return wrapper
    return cursor_decorator

def bone_name(prefix, position, side, index=0):
    if index == 0: return '%s.%s.%s' % (prefix, position, side)
    return '%s.%s.%s.%03d' % (prefix, position, side, index)

def bone_range(bones, name_prefix, position, side):
    for index in itertools.count():
        name = bone_name(name_prefix, position, side, index)
        if name in bones: yield bones[name]
        else: break

def find_wheelbrake_bone(bones, position, side, index):
    other_side = 'R' if side == 'L' else 'L'
    name_prefix = 'WheelBrake'
    bone = bones.get(bone_name(name_prefix, position, side, index))
    if bone: return bone
    bone = bones.get(bone_name(name_prefix, position, other_side, index))
    if bone: return bone
    backward_compatible_bone_name = '%s Wheels' % ('Front' if position == 'Ft' else 'Back')
    return bones.get(backward_compatible_bone_name)

def clear_property_animation(context, property_name, remove_keyframes=True):
    obj = context.object
    if remove_keyframes and obj.animation_data and obj.animation_data.action:
        fcurve_datapath = '["%s"]' % property_name
        action = obj.animation_data.action
        fcurve = action.fcurves.find(fcurve_datapath)
        if fcurve: action.fcurves.remove(fcurve)
    obj[property_name] = .0

def create_property_animation(context, property_name):
    obj = context.object
    if obj.animation_data is None: obj.animation_data_create()
    if obj.animation_data.action is None:
        obj.animation_data.action = bpy.data.actions.new(f"{obj.name}Action")
    action = obj.animation_data.action
    fcurve_datapath = f'["{property_name}"]'
    fc = action.fcurves.find(fcurve_datapath)
    if fc is None:
        fc = action.fcurves.new(fcurve_datapath, index=0, action_group='Wheels rotation')
    return fc

def fix_old_steering_rotation(rig_object):
    if rig_object.pose and rig_object.pose.bones:
        if 'MCH-Steering.rotation' in rig_object.pose.bones:
            rig_object.pose.bones['MCH-Steering.rotation'].rotation_mode = 'QUATERNION'

# --- EVALUATION ---

class FCurvesEvaluator(object):
    def __init__(self, fcurves, default_value):
        self.default_value = default_value
        self.fcurves = fcurves
    def evaluate(self, f):
        result = []
        for fcurve, value in zip(self.fcurves, self.default_value):
            result.append(fcurve.evaluate(f) if fcurve is not None else value)
        return result

class VectorFCurvesEvaluator(object):
    def __init__(self, eval): self.eval = eval
    def evaluate(self, f): return mathutils.Vector(self.eval.evaluate(f))

class EulerToQuaternionFCurvesEvaluator(object):
    def __init__(self, eval): self.eval = eval
    def evaluate(self, f): return mathutils.Euler(self.eval.evaluate(f)).to_quaternion()

class QuaternionFCurvesEvaluator(object):
    def __init__(self, eval): self.eval = eval
    def evaluate(self, f): return mathutils.Quaternion(self.eval.evaluate(f))

# --- BAKING BASE ---

class BakingOperator(object):
    frame_start: bpy.props.IntProperty(name='Start Frame', min=1)
    frame_end: bpy.props.IntProperty(name='End Frame', min=1)
    keyframe_tolerance: bpy.props.FloatProperty(name='Keyframe tolerance', min=0, default=.01)

    @classmethod
    def poll(cls, context):
        return (context.object and 'Car Rig' in context.object.data and 
                context.object.mode in ('POSE', 'OBJECT'))

    def invoke(self, context, event):
        if context.object.animation_data and context.object.animation_data.action:
            action = context.object.animation_data.action
            self.frame_start = int(action.frame_range[0])
            self.frame_end = int(action.frame_range[1])
        else:
            self.frame_start = context.scene.frame_start
            self.frame_end = context.scene.frame_end
        return context.window_manager.invoke_props_dialog(self)

    def _bake_action(self, context, *source_bones):
        obj = context.object
        prev_mode = obj.mode
        bpy.ops.object.mode_set(mode='POSE')
        for pb in obj.pose.bones: pb.bone.select = False
        for bone in source_bones:
            if bone.name in obj.pose.bones: obj.pose.bones[bone.name].bone.select = True
        
        baked_action = bpy_extras.anim_utils.bake_action(
            obj,
            action=obj.animation_data.action if (obj.animation_data and obj.animation_data.action) else None,
            frames=range(self.frame_start, self.frame_end + 1),
            bake_options=RigacarBakeOptions()
        )
        bpy.ops.object.mode_set(mode=prev_mode)
        return baked_action

# --- MAIN OPERATORS ---

class ANIM_OT_carWheelsRotationBake(bpy.types.Operator, BakingOperator):
    bl_idname = 'anim.car_wheels_rotation_bake'
    bl_label = 'Bake wheels rotation'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        self._bake_wheels_rotation(context)
        return {'FINISHED'}

    @cursor('WAIT')
    def _bake_wheels_rotation(self, context):
        bones = context.object.data.bones
        wheel_bones, brake_bones = [], []
        for position, side in itertools.product(('Ft', 'Bk'), ('L', 'R')):
            for index, wheel_bone in enumerate(bone_range(bones, 'MCH-Wheel.rotation', position, side)):
                wheel_bones.append(wheel_bone)
                brake_bones.append(find_wheelbrake_bone(bones, position, side, index) or wheel_bone)

        for prop_name in [b.name.replace('MCH-', '') for b in wheel_bones]:
            clear_property_animation(context, prop_name)

        baked_action = self._bake_action(context, *set(wheel_bones + brake_bones))
        if baked_action is None: return

        try:
            for wb, bb in zip(wheel_bones, brake_bones):
                fc = create_property_animation(context, wb.name.replace('MCH-', ''))
                l_ev = VectorFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{wb.name}"].location', index=i) for i in range(3)], (.0, .0, .0)))
                r_ev = EulerToQuaternionFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{wb.name}"].rotation_euler', index=i) for i in range(3)], (.0, .0, .0)))
                s_ev = VectorFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{bb.name}"].scale', index=i) for i in range(3)], (1.0, 1.0, 1.0)))
                
                radius, dist = wb.length if wb.length > 0 else 1.0, 0
                init_v = (wb.head_local - wb.tail_local).normalized()
                prev_p = l_ev.evaluate(self.frame_start)

                for f in range(self.frame_start + 1, self.frame_end + 1):
                    p = l_ev.evaluate(f)
                    speed = math.copysign((p - prev_p).magnitude, (r_ev.evaluate(f) @ init_v).dot((p - prev_p))) * (2 * s_ev.evaluate(f).y - 1)
                    dist += speed / radius
                    fc.keyframe_points.insert(f, dist).interpolation = 'LINEAR'
                    prev_p = p
        finally:
            if baked_action != context.object.animation_data.action: bpy.data.actions.remove(baked_action)

class ANIM_OT_carSteeringBake(bpy.types.Operator, BakingOperator):
    bl_idname = 'anim.car_steering_bake'
    bl_label = 'Bake car steering'
    bl_options = {'REGISTER', 'UNDO'}
    rotation_factor: bpy.props.FloatProperty(name='Rotation factor', min=.1, default=1)

    def execute(self, context):
        if 'Steering' in context.object.data.bones and 'MCH-Steering.rotation' in context.object.data.bones:
            steering = context.object.data.bones['Steering']
            mch = context.object.data.bones['MCH-Steering.rotation']
            self._bake_steering_rotation(context, abs(steering.head_local.y - mch.head_local.y), mch)
        return {'FINISHED'}

    @cursor('WAIT')
    def _bake_steering_rotation(self, context, bone_offset, bone):
        clear_property_animation(context, 'Steering.rotation')
        fix_old_steering_rotation(context.object)
        fc = create_property_animation(context, 'Steering.rotation')
        baked_action = self._bake_action(context, bone)
        if baked_action is None: return

        try:
            l_ev = VectorFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{bone.name}"].location', index=i) for i in range(3)], (.0, .0, .0)))
            r_ev = QuaternionFCurvesEvaluator(FCurvesEvaluator([baked_action.fcurves.find(f'pose.bones["{bone.name}"].rotation_quaternion', index=i) for i in range(4)], (1.0, .0, .0, .0)))
            dir_v, curr_p = (bone.head_local - bone.tail_local).normalized(), l_ev.evaluate(self.frame_start)

            for f in range(self.frame_start, self.frame_end):
                next_p = l_ev.evaluate(f + 1)
                s_dir = next_p - curr_p
                if s_dir.length_squared > 0.0001:
                    q = r_ev.evaluate(f)
                    w_d, w_n = q @ dir_v, q @ mathutils.Vector((1, 0, 0))
                    proj = s_dir.dot(w_d)
                    if proj != 0:
                        val = mathutils.geometry.distance_point_to_plane(s_dir * (bone_offset * self.rotation_factor / proj), w_d, w_n)
                        fc.keyframe_points.insert(f, val).interpolation = 'LINEAR'
                curr_p = next_p
        finally:
            if baked_action != context.object.animation_data.action: bpy.data.actions.remove(baked_action)

# --- RESTORED INTERACTIVE CLEAR OPERATOR ---

class ANIM_OT_carClearSteeringWheelsRotation(bpy.types.Operator):
    bl_idname = "anim.car_clear_steering_wheels_rotation"
    bl_label = "Clear baked animation"
    bl_description = "Clear generated rotation for steering and wheels"
    bl_options = {'REGISTER', 'UNDO'}

    clear_steering: bpy.props.BoolProperty(name="Steering", default=True)
    clear_wheels: bpy.props.BoolProperty(name="Wheels", default=True)

    def draw(self, context):
        layout = self.layout
        layout.label(text='Clear generated keyframes for')
        layout.prop(self, 'clear_steering')
        layout.prop(self, 'clear_wheels')

    @classmethod
    def poll(cls, context):
        return context.object and context.object.data and context.object.data.get('Car Rig')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        re_wheel = re.compile(r'^Wheel\.rotation\.(Ft|Bk)\.[LR](\.\d+)?$')
        for prop in list(context.object.keys()):
            if prop == 'Steering.rotation':
                clear_property_animation(context, prop, remove_keyframes=self.clear_steering)
            elif re_wheel.match(prop):
                clear_property_animation(context, prop, remove_keyframes=self.clear_wheels)
        
        # Refresh 4.5 Depsgraph
        mode = context.object.mode
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode=mode)
        return {'FINISHED'}

# --- UI ---

class VIEW3D_PT_RigacarPanel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Car'
    bl_label = 'Rigacar Baking'

    def draw(self, context):
        layout = self.layout
        if context.object and 'Car Rig' in context.object.data:
            col = layout.column(align=True)
            col.operator("anim.car_wheels_rotation_bake", icon='ORIENTATION_GIMBAL')
            col.operator("anim.car_steering_bake", icon='AUTOMERGE_ON')
            layout.separator()
            layout.operator("anim.car_clear_steering_wheels_rotation", icon='X', text="Clear Baked...")

# --- REG ---

classes = (ANIM_OT_carWheelsRotationBake, ANIM_OT_carSteeringBake, 
           ANIM_OT_carClearSteeringWheelsRotation, VIEW3D_PT_RigacarPanel)

def register():
    for cls in classes: bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
