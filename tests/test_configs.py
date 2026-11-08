"""Offline template/sequence checks, not an emulator of QIDI firmware or hardware.

The runner expands the changed macros and models G-code coordinate modes and
homing dispatch. Vendor Box/heater/UI commands are recorded, not implemented.
"""
import ast
import configparser
import copy
import itertools
from pathlib import Path
import re
import unittest

import jinja2
from jinja2 import meta, nodes

ROOT = Path(__file__).resolve().parents[1]
ENV = jinja2.Environment('{%', '%}', '{', '}')


def parse(path):
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    cp.read_string('\n'.join(line.split('#', 1)[0] for line in path.read_text(encoding='utf-8').splitlines()))
    return cp


def load():
    sections, origins, external = {}, {}, []

    def visit(path):
        cp = parse(path)
        for name in cp.sections():
            if name.startswith('include '):
                spec = name[8:]
                if spec in ('MCU_ID.cfg', 'timelapse.cfg', 'box.cfg'):
                    external.append(spec)
                    continue
                if spec == 'klipper-macros-qd/gcode_macro.cfg':
                    target = ROOT / 'custom/configs/gcode_macro.cfg'
                elif spec == 'KAMP_custom/KAMP_Settings.cfg':
                    target = ROOT / 'custom/KAMP/KAMP_Settings.cfg'
                else:
                    target = path.parent / spec
                if not target.is_file():
                    raise AssertionError(f'Missing repository include: {target}')
                visit(target)
                continue
            if name in sections:
                raise AssertionError(f'Duplicate {name}: {origins[name]} and {path}')
            sections[name] = dict(cp[name])
            origins[name] = path
    visit(ROOT / 'custom/printer.cfg')
    return sections, external


SECTIONS, EXTERNAL = load()
COMPILED = {name: ENV.from_string(data.get('gcode', '')) for name, data in SECTIONS.items()}
MACROS = {name.split(' ', 1)[1].upper(): data for name, data in SECTIONS.items()
          if name.startswith('gcode_macro ')}


def vector(x=0.0, y=0.0, z=0.0, e=0.0):
    return dict(x=x, y=y, z=z, e=e)


class Rig:
    def __init__(self, homed='xyz', hot=True, box=False, firmware_retraction=True):
        self.printer = {
            'gcode': dict(commands={'E_LOAD': {}, 'E_UNLOAD': {}}),
            'toolhead': dict(homed_axes=homed, axis_minimum=vector(-1, -1, -2),
                             axis_maximum=vector(275, 295, 265), position=vector(135, 135, 10),
                             max_velocity=600, max_accel=10000, square_corner_velocity=8, minimum_cruise_ratio=0.5),
            'gcode_move': dict(position=vector(135, 135, 10), gcode_position=vector(135, 135, 10),
                               homing_origin=vector(), absolute_coordinates=True,
                               absolute_extrude=False),
            'extruder': dict(can_extrude=hot, target=220 if hot else 0),
            'save_variables': dict(variables=dict(box_count=int(box), enable_box=int(box))),
            'configfile': dict(settings=dict(idle_timeout=dict(timeout=600),
                extruder=dict(max_extrude_cross_section=500),
                bed_mesh=dict(mesh_min=[10, 10], mesh_max=[260, 260], probe_count=[6, 6]),
                **{'tmc2240 stepper_x': dict(run_current=1.2)})),
            'exclude_object': dict(objects=[]),
            'pause_resume': dict(is_paused=True),
            'filament_switch_sensor filament_switch_sensor': dict(filament_detected=True),
            'print_stats': dict(state='standby'),
            'fan_generic cooling_fan': dict(speed=0),
            'fan_generic auxiliary_cooling_fan': dict(speed=0),
            'fan_generic chamber_circulation_fan': dict(speed=0),
            'heater_generic chamber': dict(target=0),
            'output_pin polar_cooler': dict(value=0),
        }
        if box:
            self.printer['box_extras'] = {}
        if firmware_retraction:
            self.printer['firmware_retraction'] = {}
        for name, data in SECTIONS.items():
            if name.startswith('gcode_macro '):
                self.printer[name] = {k[9:]: ast.literal_eval(v) for k, v in data.items() if k.startswith('variable_')}
        self.trace, self.raw_homes, self.moves = [], [], []
        self.states, self.timers = {}, {}
        self.motion_limits = []
        self.homing_override_active = False
        self.retracted = False
        self.objects(100, 100, 150, 150)

    def objects(self, x0, y0, x1, y1):
        self.printer['exclude_object']['objects'] = [dict(polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]])]

    def position(self, x=None, y=None, z=None, offset=None):
        if offset is not None:
            self.printer['gcode_move']['homing_origin'].update(offset)
        for key, value in dict(x=x, y=y, z=z).items():
            if value is not None:
                self.printer['toolhead']['position'][key] = value
                self.printer['gcode_move']['position'][key] = value
            self.printer['gcode_move']['gcode_position'][key] = (
                self.printer['gcode_move']['position'][key] - self.printer['gcode_move']['homing_origin'][key])

    def render(self, section, params=None, rawparams=''):
        data = SECTIONS[section]
        ctx = copy.deepcopy(self.printer)
        variables = ctx.get(section, {})

        def fail(message):
            raise ValueError(message)
        return COMPILED[section].render(
            **variables, printer=ctx, params=params or {}, rawparams=rawparams,
            action_raise_error=fail, action_respond_info=lambda *_: '')

    def run(self, script, depth=0):
        if depth > 40:
            raise AssertionError('Recursive macro dispatch')
        for raw in script.splitlines():
            line = raw.split('#', 1)[0].split(';', 1)[0].strip()
            if not line:
                continue
            self.trace.append(line)
            command, _, rest = line.partition(' ')
            command = command.upper()
            tokens = rest.split()
            params = {}
            for token in tokens:
                if '=' in token:
                    key, value = token.split('=', 1)
                else:
                    key, value = token[:1], token[1:]
                params[key.upper()] = value
            gm = self.printer['gcode_move']
            if command == 'G28.6245197':
                if self.homing_override_active:
                    axes = ''.join(a for a in 'XYZ' if a in params) or 'XYZ'
                    self.raw_homes.append(axes)
                    for axis in axes.lower():
                        self.position(**{axis: {'x': 271, 'y': -1, 'z': 0}[axis]})
                    self.printer['toolhead']['homed_axes'] = ''.join(a for a in 'xyz' if a in self.printer['toolhead']['homed_axes'] or a.upper() in axes)
                else:
                    self.homing_override_active = True
                    try:
                        self.run(self.render('homing_override', params), depth + 1)
                    finally:
                        self.homing_override_active = False
            elif command in MACROS:
                key = next(s for s in SECTIONS if s.startswith('gcode_macro ') and s.split(' ', 1)[1].upper() == command)
                self.run(self.render(key, params, rest), depth + 1)
            elif command == 'SET_GCODE_VARIABLE':
                key = next(s for s in self.printer if s.startswith('gcode_macro ') and s.split(' ', 1)[1].upper() == params['MACRO'].upper())
                self.printer[key][params['VARIABLE']] = ast.literal_eval(params['VALUE'])
            elif command == 'SET_VELOCITY_LIMIT':
                th = self.printer['toolhead']
                for arg, key in [('VELOCITY', 'max_velocity'), ('ACCEL', 'max_accel'),
                                 ('SQUARE_CORNER_VELOCITY', 'square_corner_velocity'),
                                 ('MINIMUM_CRUISE_RATIO', 'minimum_cruise_ratio')]:
                    if arg in params:
                        th[key] = float(params[arg])
                if 'ACCEL_TO_DECEL' in params:
                    th['minimum_cruise_ratio'] = 1 - min(float(params['ACCEL_TO_DECEL']) / th['max_accel'], 1)
            elif command in ('G90', 'G91'):
                gm['absolute_coordinates'] = command == 'G90'
            elif command in ('M82', 'M83'):
                gm['absolute_extrude'] = command == 'M82'
            elif command == 'SAVE_GCODE_STATE':
                self.states[params['NAME']] = copy.deepcopy(gm)
            elif command == 'RESTORE_GCODE_STATE':
                saved = self.states[params['NAME']]
                for key in ('absolute_coordinates', 'absolute_extrude', 'homing_origin'):
                    gm[key] = copy.deepcopy(saved[key])
                self.position()
            elif command == 'G92':
                for axis in 'xyze':
                    if axis.upper() in params:
                        gm['homing_origin'][axis] = gm['position'][axis] - float(params[axis.upper()])
                self.position()
            elif command == 'SET_GCODE_OFFSET':
                for axis in 'xyz':
                    if axis.upper() in params:
                        gm['homing_origin'][axis] = float(params[axis.upper()])
                self.position()
            elif command == 'SET_KINEMATIC_POSITION':
                self.position(**{a: float(params[a.upper()]) for a in 'xyz' if a.upper() in params})
                # Standard Klipper's legacy default marks XYZ homed.
                self.printer['toolhead']['homed_axes'] = 'xyz'
            elif command in ('G0', 'G1'):
                before = copy.deepcopy(self.printer['toolhead']['position'])
                for axis in 'xyz':
                    if axis.upper() not in params:
                        continue
                    value = float(params[axis.upper()])
                    target = value + gm['homing_origin'][axis] if gm['absolute_coordinates'] else before[axis] + value
                    lo = self.printer['toolhead']['axis_minimum'][axis]
                    hi = self.printer['toolhead']['axis_maximum'][axis]
                    if not lo - 1e-6 <= target <= hi + 1e-6:
                        raise AssertionError(f'Out of bounds: {line}; {axis}={target}')
                    self.position(**{axis: target})
                if 'E' in params and float(params['E']) and not self.printer['extruder']['can_extrude']:
                    raise AssertionError('Cold extrusion: ' + line)
                self.moves.append((line, before, copy.deepcopy(self.printer['toolhead']['position'])))
                self.motion_limits.append((line, copy.deepcopy(self.printer['toolhead'])))
            elif command in ('G10', 'G11'):
                self.retracted = command == 'G10'
            elif command == 'UPDATE_DELAYED_GCODE':
                self.timers[params['ID']] = float(params['DURATION'])
            elif command == 'M84.1':
                self.printer['toolhead']['homed_axes'] = ''
            # Unmodelled vendor commands are trace entries, not simulated hardware.


class ConfigTests(unittest.TestCase):
    def test_preparation_scope_and_lower_limits(self):
        for initial in [(600, 10000, 8, 0.4), (100, 800, 3, 0.7)]:
            rig = Rig()
            keys = ('max_velocity', 'max_accel', 'square_corner_velocity', 'minimum_cruise_ratio')
            rig.printer['toolhead'].update(zip(keys, initial))
            rig.run('_PREP_MOTION_BEGIN\nM204 S5000\n_PREP_MOTION_BEGIN')
            th = rig.printer['toolhead']
            self.assertEqual(th['max_velocity'], min(150, initial[0]))
            self.assertEqual(th['max_accel'], min(2000, initial[1]))
            rig.run('SMART_PARK')
            self.assertEqual(rig.printer['gcode_macro _PREP_MOTION']['active'], 1)
            rig.run('_PREP_MOTION_END')
            self.assertEqual(tuple(th[k] for k in keys), initial)
            self.assertEqual(rig.printer['gcode_macro _PREP_MOTION']['active'], 0)

    def test_standalone_park_and_purge_limits_restore(self):
        for command in ['SMART_PARK', 'LINE_PURGE']:
            rig = Rig()
            rig.printer['toolhead']['minimum_cruise_ratio'] = 0.37
            rig.run(command)
            for line, th in rig.motion_limits:
                self.assertLessEqual(th['max_velocity'], 150, line)
                self.assertLessEqual(th['max_accel'], 2000, line)
                if ' X' in line or ' Y' in line:
                    self.assertLessEqual(float(re.search(r' F([0-9.]+)', line)[1]), 9000, line)
            th = rig.printer['toolhead']
            self.assertEqual((th['max_velocity'], th['max_accel'], th['minimum_cruise_ratio']), (600, 10000, 0.37))
            self.assertFalse(rig.printer['gcode_macro _PREP_MOTION']['active'])

    def test_preparation_homing_and_cancellation_restore(self):
        rig = Rig()
        rig.run('_PREP_MOTION_BEGIN\nG28')
        homing_moves = list(rig.motion_limits)
        self.assertTrue(homing_moves)
        self.assertTrue(all(th['max_velocity'] == 600 and th['max_accel'] == 10000
                            for _, th in homing_moves))
        self.assertEqual(rig.printer['toolhead']['max_accel'], 2000)
        self.assertEqual(rig.printer['toolhead']['max_velocity'], 150)
        rig.run('CANCEL_PRINT')
        self.assertEqual(rig.printer['toolhead']['max_accel'], 10000)
        self.assertEqual(rig.printer['toolhead']['max_velocity'], 600)
        self.assertFalse(rig.printer['gcode_macro _PREP_MOTION']['active'])
        start = rig.render('gcode_macro PRINT_START', dict(BED='60', HOTEND='220', CHAMBER='0'))
        self.assertLess(start.index('_KAMP_PREPARE_PURGE'), start.index('_PREP_MOTION_BEGIN'))
        self.assertLess(start.index('M109'), start.index('_PREP_MOTION_END'))
        self.assertNotIn('M204 S10000', start)
        rig.run('_PREP_MOTION_BEGIN')
        clean = rig.render('gcode_macro CLEAR_NOZZLE')
        self.assertIn('M204 S1500', clean)

    def test_includes_and_template_syntax(self):
        self.assertEqual(set(EXTERNAL), {'MCU_ID.cfg', 'timelapse.cfg', 'box.cfg'})
        self.assertEqual(sum(s == 'gcode_macro BED_MESH_CALIBRATE' for s in SECTIONS), 1)
        self.assertEqual(len(MACROS), sum(s.startswith('gcode_macro ') for s in SECTIONS))
        for section, data in SECTIONS.items():
            if 'gcode' in data:
                with self.subTest(section=section):
                    tree = ENV.parse(data['gcode'])
                    allowed = {'printer', 'params', 'rawparams', 'action_raise_error', 'action_respond_info',
                               'action_call_remote_method', 'action_emergency_stop', 'action_send_event'}
                    allowed.update(k[9:] for k in data if k.startswith('variable_'))
                    # Jinja meta treats assignments inside if blocks as potential
                    # inputs. Branch coverage is checked by the render tests.
                    allowed.update(n.name for n in tree.find_all(nodes.Name) if n.ctx == 'store')
                    self.assertFalse(meta.find_undeclared_variables(tree) - allowed)

    def test_factory_box_sections_and_commands(self):
        factory = parse(ROOT / 'default/printer.cfg')
        for key in ('box485', 'box2base'):
            self.assertEqual(SECTIONS[key], dict(factory[key]))
        rig = Rig()
        for number in range(16):
            self.assertEqual(rig.render(f'gcode_macro T{number}').strip(), f'T_MANAGER T={number}')
        self.assertIn('INIT_RFID_READ', rig.render('delayed_gcode PRINTER_INIT'))
        self.assertIn('G4 P1000', rig.render('gcode_macro TRY_INTO_EXTRUDER'))

    def test_homing_matrix(self):
        combinations = [''.join(p) for n in range(4) for p in itertools.combinations('XYZ', n)]
        for axes, homed, lazy in itertools.product(combinations, combinations, [False, True]):
            with self.subTest(axes=axes, homed=homed, lazy=lazy):
                rig = Rig(homed=homed.lower())
                rig.run('G28 ' + ' '.join(axes) + (' O' if lazy else ''))
                requested = set(axes or 'XYZ') - (set(homed) if lazy else set())
                expected = requested | (set('XY') - set(homed) if 'Z' in requested else set())
                self.assertEqual(set(''.join(rig.raw_homes)), expected)
                self.assertEqual(rig.raw_homes.count('Z'), int('Z' in requested))
                if not requested:
                    self.assertFalse(rig.moves)

    def test_z_only_preserves_xy(self):
        rig = Rig()
        rig.position(x=240, y=32)
        rig.run('G28 Z')
        self.assertEqual(rig.printer['toolhead']['position']['x'], 240)
        self.assertEqual(rig.printer['toolhead']['position']['y'], 32)
        self.assertEqual(rig.raw_homes, ['Z'])
        with self.assertRaises(ValueError):
            rig.run('LAZY_HOME AXES=E')

    def test_safe_lift_modes_offsets_and_limits(self):
        for z, offset, absolute, homed, lift in itertools.product(
                [0, 255.8, 256, 262, 265], [-1.5, 0, 0.3], [False, True], ['', 'z'], [-2, 0, 1, 50]):
            with self.subTest(z=z, offset=offset, absolute=absolute, homed=homed, lift=lift):
                rig = Rig(homed=homed)
                rig.position(z=z, offset=dict(z=offset))
                rig.printer['gcode_move']['absolute_coordinates'] = absolute
                rig.run(f'SAFE_Z_LIFT Z={lift}')
                expected = z + min(max(lift, 0), max(256 - z, 0)) if homed else z
                self.assertAlmostEqual(rig.printer['toolhead']['position']['z'], expected)
                self.assertEqual(rig.printer['gcode_move']['absolute_coordinates'], absolute)

    def test_purge_edges_and_retraction(self):
        for box, fw in itertools.product([(100, 100, 150, 150), (250, 250, 270, 270),
                                          (1, 245, 20, 269), (245, 1, 269, 20),
                                          (100, 1, 150, 20)], [False, True]):
            with self.subTest(box=box, firmware_retraction=fw):
                rig = Rig(firmware_retraction=fw)
                rig.objects(*box)
                rig.printer['gcode_move']['absolute_coordinates'] = False
                rig.printer['gcode_move']['absolute_extrude'] = True
                rig.run('LINE_PURGE')
                path = rig.printer['gcode_macro _KAMP_Purge_Path']
                for name, value in path.items():
                    self.assertTrue(1 <= value <= 269, (name, value))
                self.assertTrue(path['start_y'] <= box[1] - 2 or path['start_x'] <= box[0] - 2)
                self.assertFalse(rig.retracted)
                self.assertFalse(rig.printer['gcode_move']['absolute_coordinates'])
                self.assertTrue(rig.printer['gcode_move']['absolute_extrude'])

    def test_purge_rejects_missing_space_objects_and_heat(self):
        for scenario in ('no_objects', 'full_bed', 'cold', 'long_line'):
            rig = Rig(hot=scenario != 'cold')
            if scenario == 'no_objects':
                rig.printer['exclude_object']['objects'] = []
            elif scenario == 'full_bed':
                rig.objects(0, 0, 270, 270)
            elif scenario == 'long_line':
                rig.printer['gcode_macro _KAMP_Settings']['purge_amount'] = 280
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                rig.run('LINE_PURGE')
            self.assertFalse(any(' E' in m[0] for m in rig.moves))
            if scenario in ('no_objects', 'full_bed', 'long_line'):
                self.assertFalse(rig.moves)

    def test_park_and_purge_with_offsets(self):
        rig = Rig()
        rig.position(offset=dict(x=10, y=25, z=0.3))
        rig.objects(250, 250, 270, 270)
        rig.run('SMART_PARK\nLINE_PURGE')
        self.assertLessEqual(rig.printer['toolhead']['position']['x'], 275)
        self.assertLessEqual(rig.printer['toolhead']['position']['y'], 295)

    def test_cancel_cold_hot_unhomed_and_top(self):
        for hot, homed, box, z in itertools.product([False, True], ['', 'z', 'xyz'], [False, True], [10, 265]):
            rig = Rig(homed=homed, hot=hot, box=box)
            rig.position(z=z)
            rig.run('CANCEL_PRINT')
            self.assertTrue(all(before['x'] == after['x'] and before['y'] == after['y'] for _, before, after in rig.moves))
            self.assertIn('SET_PRINT_MAIN_STATUS MAIN_STATUS=ready', rig.trace)
            self.assertTrue(all('CUT_FILAMENT' not in cmd and 'UNLOAD' not in cmd for cmd in rig.trace))
            if rig.moves:
                self.assertLess(rig.trace.index('M104 S0'), rig.trace.index(rig.moves[0][0]))
            if not hot:
                self.assertFalse(any(' E' in m[0] for m in rig.moves))

    def test_end_at_top_and_cold(self):
        for hot, z in itertools.product([False, True], [10, 255.8, 256, 262, 265]):
            rig = Rig(hot=hot)
            rig.position(z=z)
            rig.run('PRINT_END')
            self.assertIn('SET_PRINT_MAIN_STATUS MAIN_STATUS=ready', rig.trace)
            self.assertTrue(all(after['z'] >= before['z'] for _, before, after in rig.moves))
            if z >= 255.8:
                self.assertTrue(all(after['x'] == before['x'] and after['y'] == before['y'] for _, before, after in rig.moves))

    def test_pause_and_manual_load_with_box_disabled_or_absent(self):
        for present, enabled in itertools.product([False, True], [False, True]):
            rig = Rig(box=present)
            rig.printer['save_variables']['variables']['enable_box'] = int(enabled)
            rig.printer['pause_resume']['is_paused'] = False
            body = rig.render('gcode_macro PAUSE')
            self.assertEqual('BUFFER_MONITORING ENABLE=0' in body, present and enabled)
            load = rig.render('gcode_macro M604', dict(T='0'))
            unload = rig.render('gcode_macro M603')
            self.assertEqual('E_LOAD SLOT=16' in load, present and enabled)
            self.assertEqual('E_UNLOAD SLOT=16' in unload, present and enabled)

    def test_fan_timer_and_start_policy(self):
        rig = Rig(box=True)
        rig.run('PRINT_END')
        self.assertEqual(rig.timers['TURN_OFF_CHAMBER_FAN'], 180)
        start = rig.render('gcode_macro PRINT_START', dict(BED='60', HOTEND='220', CHAMBER='0', EXTRUDER='0'))
        rig.run(next(line for line in start.splitlines() if line.strip()))
        self.assertEqual(rig.timers['TURN_OFF_CHAMBER_FAN'], 0)
        for state in ('printing', 'paused'):
            rig.printer['print_stats']['state'] = state
            self.assertNotIn('M106', rig.render('delayed_gcode TURN_OFF_CHAMBER_FAN'))
        rig.printer['print_stats']['state'] = 'standby'
        self.assertIn('M106 P3 S0', rig.render('delayed_gcode TURN_OFF_CHAMBER_FAN'))

    def test_box_and_external_start_resume(self):
        for box, filament, same_slot in itertools.product([False, True], [False, True], [False, True]):
            rig = Rig(box=box)
            sv = rig.printer['save_variables']['variables']
            sv.update(value_t0='slot0', slot_sync='slot0' if same_slot else 'slot1', last_load_slot='slot0')
            rig.printer['filament_switch_sensor filament_switch_sensor']['filament_detected'] = filament
            start = rig.render('gcode_macro PRINT_START', dict(BED='60', HOTEND='220', CHAMBER='0', EXTRUDER='0'))
            self.assertEqual('EXTRUSION_AND_FLUSH' in start, box)
            self.assertIn('PURGE=0' if box else 'PURGE=1', start)
            self.assertLess(start.index('M190'), start.index('G28 Z'))
            self.assertLess(start.index('M191'), start.index('G28 Z'))
            self.assertEqual('BUFFER_MONITORING ENABLE=1' in rig.render('gcode_macro ENABLE_BOX_BUFFER'), box)
            resume = rig.render('gcode_macro RESUME')
            expected = 'TRY_RESUME_PRINT' if box else ('RESUME_PRINT' if filament else 'Please load filament')
            self.assertIn(expected, resume)
        rig = Rig()
        rig.printer['save_variables']['variables'] = {}
        self.assertIn('RESUME_PRINT', rig.render('gcode_macro RESUME'))

    def test_missing_legacy_firmware_commands(self):
        rig = Rig(box=True)
        rig.printer['gcode']['commands'] = {}
        for name, params in [('M603', {}), ('M604', {'T': '0'})]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Use QIDI Studio'):
                rig.render('gcode_macro ' + name, params)
        self.assertIn('M104 S0', rig.render('gcode_macro M604', {'T': '2'}))
        start = rig.render('gcode_macro PRINT_START', dict(BED='60', HOTEND='220', CHAMBER='0'))
        self.assertNotIn('\nsave_last_file\n', start)
        self.assertIn('Optional SAVE_LAST_FILE', start)
        rig.printer['gcode']['commands']['SAVE_LAST_FILE'] = {}
        self.assertIn('save_last_file', rig.render('gcode_macro PRINT_START', dict(BED='60', HOTEND='220', CHAMBER='0')))
        self.assertIn('M84', rig.render('idle_timeout'))

    def test_manual_load_modes_and_slicer_order(self):
        rig = Rig()
        for mode in range(3):
            body = rig.render('gcode_macro M604', dict(T=str(mode)))
            self.assertIn('M83', body)
            self.assertEqual('M104 S0' in body, mode == 2)
        start = (ROOT / 'custom/slicer/start.cfg').read_text(encoding='utf-8')
        self.assertLess(start.index('T[initial_tool]'), start.index('ENABLE_BOX_BUFFER'))
        self.assertLess(start.index('ENABLE_BOX_BUFFER'), start.index('LINE_PURGE'))
        end = (ROOT / 'custom/slicer/end.cfg').read_text(encoding='utf-8')
        self.assertNotIn('SAFE_Z_LIFT', end)
        rig.run('M106 P4 S255')
        self.assertIn('SET_PIN PIN=polar_cooler VALUE=1', rig.trace)

    def test_mesh_render_bounds_and_point_count(self):
        for objects in [[], [dict(polygon=[[100, 100], [110, 110]])], [dict(polygon=[[0, 0], [270, 270]])]]:
            rig = Rig()
            rig.printer['exclude_object']['objects'] = objects
            body = rig.render('gcode_macro BED_MESH_CALIBRATE')
            line = next(line.strip() for line in body.splitlines() if line.strip().startswith('_BED_MESH_CALIBRATE'))
            fields = dict(token.split('=', 1) for token in line.split()[1:])
            low, high = [list(map(float, fields[k].split(','))) for k in ('mesh_min', 'mesh_max')]
            self.assertTrue(all(10 <= a < b <= 260 for a, b in zip(low, high)))
            self.assertTrue(all(4 <= int(n) <= 6 for n in fields['PROBE_COUNT'].split(',')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
