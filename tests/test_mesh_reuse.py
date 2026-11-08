"""Model cache decisions and probe ordering; not a physics/probe emulator."""
import copy
import unittest
from test_configs import Rig


class MeshRig(Rig):
    def __init__(self):
        super().__init__()
        self.printer.update({
            'bed_mesh': dict(profile_name='', mesh_min=[0, 0], mesh_max=[0, 0], probed_matrix=[[]], profiles={}),
            'probe': dict(last_z_result=0), 'z_tilt': dict(applied=True),
            'heater_bed': dict(target=100, temperature=100),
        })
        self.printer['heater_generic chamber'].update(target=55, temperature=55)
        self.printer['extruder'].update(target=140, temperature=140)
        self.printer['toolhead']['estimated_print_time'] = 1000.0
        self.probes = []
        self.calibrations = 0
        self.shift = 0.0
        self.local_bump = False
        self.fail_probe = False

    @property
    def cache(self):
        return self.printer['gcode_macro _QIDI_MESH_CACHE']

    def run(self, script, depth=0):
        for raw in script.splitlines():
            line = raw.strip()
            command, _, rest = line.partition(' ')
            params = dict(token.split('=', 1) for token in rest.split() if '=' in token)
            mesh = self.printer['bed_mesh']
            if command == '_BED_MESH_CALIBRATE':
                self.trace.append(line)
                self.calibrations += 1
                low = list(map(float, params['mesh_min'].split(',')))
                high = list(map(float, params['mesh_max'].split(',')))
                matrix = [[self.calibrations * 0.01] * 4 for _ in range(4)]
                name = params['PROFILE']
                mesh.update(profile_name=name, mesh_min=low, mesh_max=high, probed_matrix=matrix)
                mesh['profiles'][name] = dict(points=copy.deepcopy(matrix), mesh_params=dict(
                    min_x=low[0], min_y=low[1], max_x=high[0], max_y=high[1]))
            elif command == 'BED_MESH_CLEAR':
                self.trace.append(line)
                mesh.update(profile_name='', mesh_min=[0, 0], mesh_max=[0, 0], probed_matrix=[[]])
            elif command == 'BED_MESH_PROFILE':
                self.trace.append(line)
                profile = mesh['profiles'][params['LOAD']]
                p = profile['mesh_params']
                mesh.update(profile_name=params['LOAD'], mesh_min=[p['min_x'], p['min_y']],
                            mesh_max=[p['max_x'], p['max_y']], probed_matrix=copy.deepcopy(profile['points']))
            elif command == 'PROBE':
                self.trace.append(line)
                if self.fail_probe:
                    raise ValueError('Simulated probe failure')
                assert not mesh['profile_name'], 'Control probe ran with mesh enabled'
                pos = self.printer['toolhead']['position']
                assert pos['z'] >= 5 - 1e-6
                self.probes.append((pos['x'], pos['y']))
                z = 0.02 + pos['x'] * .0001 + pos['y'] * .0001 + self.shift
                if self.local_bump and len(self.probes) % 5 == 3:
                    z += .06
                self.printer['probe']['last_z_result'] = z
                self.position(z=z)
            else:
                super().run(line, depth)


class MeshReuseTests(unittest.TestCase):
    def test_new_and_repeat_same_or_smaller_area(self):
        rig = MeshRig()
        rig.run('QIDI_MESH_PREPARE')
        self.assertEqual((rig.calibrations, len(rig.probes), rig.cache['result']), (1, 5, 'calibrated'))
        baseline = list(rig.cache['baseline'])
        rig.run('BED_MESH_CLEAR\nQIDI_MESH_PREPARE')
        self.assertEqual((rig.calibrations, len(rig.probes), rig.cache['result']), (1, 10, 'reused'))
        rig.objects(110, 110, 130, 130)
        rig.run('QIDI_MESH_PREPARE')
        self.assertEqual(rig.calibrations, 1)
        self.assertEqual(rig.cache['baseline'], baseline)
        self.assertEqual(rig.probes[:5], rig.probes[5:10])
        self.assertEqual(rig.printer['toolhead']['max_velocity'], 600)
        self.assertEqual(rig.printer['bed_mesh']['profile_name'], 'q2_cached')

    def test_drift_or_local_bump_triggers_new_mesh(self):
        for drift, bump in [(.04, False), (-.04, False), (0, True)]:
            rig = MeshRig()
            rig.run('QIDI_MESH_PREPARE')
            rig.shift, rig.local_bump = drift, bump
            rig.run('QIDI_MESH_PREPARE')
            self.assertEqual((rig.calibrations, len(rig.probes)), (2, 15))
            self.assertEqual(rig.cache['result'], 'calibrated')

    def test_small_deviation_reuses_without_changing_baseline(self):
        rig = MeshRig()
        rig.run('QIDI_MESH_PREPARE')
        baseline = list(rig.cache['baseline'])
        rig.shift = .02
        rig.run('QIDI_MESH_PREPARE')
        self.assertEqual(rig.calibrations, 1)
        self.assertEqual(rig.cache['baseline'], baseline)

    def test_changed_conditions_recalibrate_without_control_check(self):
        for change in ['bed_target', 'bed_actual', 'chamber_target', 'chamber_actual', 'nozzle',
                       'expired', 'missing_profile', 'changed_matrix', 'changed_bounds',
                       'larger_area', 'missing_objects', 'invalidated', 'disabled', 'force']:
            with self.subTest(change=change):
                rig = MeshRig()
                rig.run('QIDI_MESH_PREPARE')
                if change == 'bed_target': rig.printer['heater_bed']['target'] = 95
                elif change == 'bed_actual': rig.printer['heater_bed']['temperature'] = 96
                elif change == 'chamber_target': rig.printer['heater_generic chamber']['target'] = 50
                elif change == 'chamber_actual': rig.printer['heater_generic chamber']['temperature'] = 50
                elif change == 'nozzle': rig.printer['extruder']['temperature'] = 130
                elif change == 'expired': rig.printer['toolhead']['estimated_print_time'] += 14401
                elif change == 'missing_profile': rig.printer['bed_mesh']['profiles'] = {}
                elif change == 'changed_matrix': rig.printer['bed_mesh']['profiles']['q2_cached']['points'][0][0] += .1
                elif change == 'changed_bounds': rig.printer['bed_mesh']['profiles']['q2_cached']['mesh_params']['min_x'] += 1
                elif change == 'larger_area': rig.objects(200, 200, 220, 220)
                elif change == 'missing_objects': rig.printer['exclude_object']['objects'] = []
                elif change == 'invalidated': rig.run('QIDI_MESH_INVALIDATE')
                elif change == 'disabled': rig.cache['enabled'] = 0
                rig.run('QIDI_MESH_PREPARE' + (' FORCE=1' if change == 'force' else ''))
                self.assertEqual((rig.calibrations, len(rig.probes)), (2, 10))

    def test_safety_guards_and_aborted_probe(self):
        for change in ['unhomed', 'untilted', 'offset', 'hot']:
            rig = MeshRig()
            if change == 'unhomed': rig.printer['toolhead']['homed_axes'] = ''
            elif change == 'untilted': rig.printer['z_tilt']['applied'] = False
            elif change == 'offset': rig.printer['gcode_move']['homing_origin']['z'] = .1
            else: rig.printer['extruder']['temperature'] = 275
            with self.assertRaises(ValueError): rig.run('QIDI_MESH_PREPARE')
            self.assertFalse(rig.moves)
        rig = MeshRig()
        rig.run('QIDI_MESH_PREPARE')
        rig.fail_probe = True
        with self.assertRaises(ValueError): rig.run('QIDI_MESH_PREPARE')
        self.assertFalse(rig.cache['valid'])

    def test_no_outside_reuse_even_with_clipped_margin(self):
        rig = MeshRig()
        rig.objects(250, 250, 270, 270)
        rig.run('QIDI_MESH_PREPARE\nQIDI_MESH_PREPARE')
        self.assertEqual(rig.calibrations, 2)


if __name__ == '__main__':
    unittest.main()
