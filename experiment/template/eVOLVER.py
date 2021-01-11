#!/usr/bin/env python3

##### IMPORTANT #####
# Read the README.md file before touching this file.

import os
import sys
import time
import pickle
import shutil
import logging
import argparse
import numpy as np
import json
import traceback
from scipy import stats
from socketIO_client import SocketIO, BaseNamespace

import custom_script
from custom_script import EVOLVER_IP, EVOLVER_PORT

# See get_options() for config of options.
options = None
OPERATION_MODE = None
EXP_NAME = None
# tab delimited, mL/s with 16 influx pumps on first row, etc.
PUMP_CAL_FILE = 'pump_cal.txt'
SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
EXP_DIR = None
TEMP_INITIAL = None
OD_CAL_PATH = os.path.join(SAVE_PATH, 'od_cal.json')
TEMP_CAL_PATH = os.path.join(SAVE_PATH, 'temp_cal.json')
# Should not be changed. Vials to be considered/excluded should be handled inside the custom functions.
VIALS = [x for x in range(16)]
SIGMOID = 'sigmoid'
LINEAR = 'linear'
THREE_DIMENSION = '3d'

logger = logging.getLogger('eVOLVER')

EVOLVER_NS = None


class EvolverNamespace(BaseNamespace):
    start_time = None
    use_blank = False
    OD_initial = None

    def on_connect(self, *args):
        print("Connected to eVOLVER as client")
        logger.info('connected to eVOLVER as client')

    def on_disconnect(self, *args):
        print("Disconected from eVOLVER as client")
        logger.info('disconnected to eVOLVER as client')

    def on_reconnect(self, *args):
        print("Reconnected to eVOLVER as client")
        logger.info("reconnected to eVOLVER as client")

    def on_broadcast(self, data):
        logger.debug('broadcast received')
        elapsed_time = round((time.time() - self.start_time) / 3600, 4)
        logger.debug('elapsed time: %.4f hours' % elapsed_time)
        print("{0}: {1} Hours".format(EXP_NAME, elapsed_time))
        # are the calibrations in yet?
        if not self.check_for_calibrations():
            logger.warning('calibration files still missing, skipping custom '
                           'functions')
            return

        with open(OD_CAL_PATH) as f:
            od_cal = json.load(f)
        with open(TEMP_CAL_PATH) as f:
            temp_cal = json.load(f)

        # apply calibrations
        # update temperatures if needed
        data = self.transform_data(data, VIALS, od_cal, temp_cal)
        if data is None:
            logger.error('could not tranform raw data, skipping user-'
                         'defined functions')
            return

        # should we "blank" the OD?
        if self.use_blank and self.OD_initial is None:
            logger.info('setting initial OD reading')
            self.OD_initial = data['transformed']['od']
        elif self.OD_initial is None:
            self.OD_initial = np.zeros(len(VIALS))
        data['transformed']['od'] = (data['transformed']['od'] -
                                     self.OD_initial)
        # save data
        self.save_data(data['transformed']['od'], elapsed_time,
                       VIALS, 'OD')
        self.save_data(data['transformed']['temp'], elapsed_time,
                       VIALS, 'temp')
        for param in od_cal['params']:
            self.save_data(data['data'].get(param, []), elapsed_time,
                           VIALS, param + '_raw')
        for param in temp_cal['params']:
            self.save_data(data['data'].get(param, []), elapsed_time,
                           VIALS, param + '_raw')
        # run custom functions
        self.custom_functions(data, VIALS, elapsed_time)
        # save variables
        self.save_variables(self.start_time, self.OD_initial)

    def on_activecalibrations(self, data):
        print('Calibrations recieved')
        for calibration in data:
            if calibration['calibrationType'] == 'od':
                file_path = OD_CAL_PATH
            elif calibration['calibrationType'] == 'temperature':
                file_path = TEMP_CAL_PATH
            else:
                continue
            for fit in calibration['fits']:
                if fit['active']:
                    with open(file_path, 'w') as f:
                        json.dump(fit, f)
                    # Create raw data directories and files for params needed
                    for param in fit['params']:
                        if not os.path.isdir(os.path.join(EXP_DIR, param + '_raw')):
                            os.makedirs(os.path.join(EXP_DIR, param + '_raw'))
                            for x in range(len(fit['coefficients'])):
                                exp_str = "Experiment: {0} vial {1}, {2}".format(EXP_NAME,
                                                                                 x,
                                                                                 time.strftime("%c"))
                                self._create_file(
                                    x, param + '_raw', defaults=[exp_str])
                    break

    def request_calibrations(self):
        logger.debug('requesting active calibrations')
        self.emit('getactivecal',
                  {}, namespace='/dpu-evolver')

    # Where OD and temperature calibrations are applied to raw data readings.
    def transform_data(self, data, vials, od_cal, temp_cal):
        od_data_2 = None
        if od_cal['type'] == THREE_DIMENSION:
            od_data_2 = data['data'].get(od_cal['params'][1], None)

        od_data = data['data'].get(od_cal['params'][0], None)
        temp_data = data['data'].get(temp_cal['params'][0], None)
        set_temp_data = data['config'].get('temp', {}).get('value', None)

        if od_data is None or temp_data is None or set_temp_data is None:
            print('Incomplete data recieved, Error with measurement')
            logger.error('Incomplete data received, error with measurements')
            return None
        if 'NaN' in od_data or 'NaN' in temp_data or 'NaN' in set_temp_data:
            print('NaN recieved, Error with measurement')
            logger.error('NaN received, error with measurements')
            return None

        od_data = np.array([float(x) for x in od_data])
        if od_data_2:
            od_data_2 = np.array([float(x) for x in od_data_2])
        temp_data = np.array([float(x) for x in temp_data])
        set_temp_data = np.array([float(x) for x in set_temp_data])

        temps = []
        for x in vials:
            file_name = "vial{0}_temp_config.txt".format(x)
            file_path = os.path.join(EXP_DIR, 'temp_config', file_name)
            temp_set_data = np.genfromtxt(file_path, delimiter=',')
            temp_set = temp_set_data[len(temp_set_data)-1][1]
            temps.append(temp_set)
            od_coefficients = od_cal['coefficients'][x]
            temp_coefficients = temp_cal['coefficients'][x]
            try:
                if od_cal['type'] == SIGMOID:
                    # convert raw photodiode data into ODdata using calibration curve
                    od_data[x] = np.real(od_coefficients[2] -
                                         ((np.log10((od_coefficients[1] -
                                                     od_coefficients[0]) /
                                                    (float(od_data[x]) -
                                                     od_coefficients[0])-1)) /
                                          od_coefficients[3]))
                    if not np.isfinite(od_data[x]):
                        od_data[x] = 'NaN'
                        logger.debug('OD from vial %d: %s' % (x, od_data[x]))
                    else:
                        logger.debug('OD from vial %d: %.3f' % (x, od_data[x]))
                elif od_cal['type'] == THREE_DIMENSION:
                    od_data[x] = np.real(od_coefficients[0] +
                                         (od_coefficients[1]*od_data[x]) +
                                         (od_coefficients[2]*od_data_2[x]) +
                                         (od_coefficients[3]*(od_data[x]**2)) +
                                         (od_coefficients[4]*od_data[x]*od_data_2[x]) +
                                         (od_coefficients[5]*(od_data_2[x]**2)))
                else:
                    logger.error('OD calibration not of supported type!')
                    od_data[x] = 'NaN'
            except ValueError:
                print("OD Read Error")
                logger.error('OD read error for vial %d, setting to NaN' % x)
                od_data[x] = 'NaN'
            try:
                temp_data[x] = (float(temp_data[x]) *
                                temp_coefficients[0]) + temp_coefficients[1]
                logger.debug('temperature from vial %d: %.3f' %
                             (x, temp_data[x]))
            except ValueError:
                print("Temp Read Error")
                logger.error('temperature read error for vial %d, setting to NaN'
                             % x)
                temp_data[x] = 'NaN'
            try:
                set_temp_data[x] = (float(set_temp_data[x]) *
                                    temp_coefficients[0]) + temp_coefficients[1]
                logger.debug('set_temperature from vial %d: %.3f' % (x,
                                                                     set_temp_data[x]))
            except ValueError:
                print("Set Temp Read Error")
                logger.error('set temperature read error for vial %d, setting to NaN'
                             % x)
                set_temp_data[x] = 'NaN'

        temps = np.array(temps)
        # update temperatures only if difference with expected
        # value is above 0.2 degrees celsius
        delta_t = np.abs(set_temp_data - temps).max()
        if delta_t > 0.2:
            logger.info('updating temperatures (max. deltaT is %.2f)' %
                        delta_t)
            coefficients = temp_cal['coefficients']
            raw_temperatures = [str(int((temps[x] - temp_cal['coefficients'][x][1]) /
                                        temp_cal['coefficients'][x][0]))
                                for x in vials]
            self.update_temperature(raw_temperatures)
        else:
            # config from server agrees with local config
            # report if actual temperature doesn't match
            delta_t = np.abs(temps - temp_data).max()
            if delta_t > 0.2:
                logger.info('actual temperature doesn\'t match configuration '
                            '(yet? max deltaT is %.2f)' % delta_t)
                logger.debug('temperature config: %s' % temps)
                logger.debug('actual temperatures: %s' % temp_data)

        # add a new field in the data dictionary
        data['transformed'] = {}
        data['transformed']['od'] = od_data
        data['transformed']['temp'] = temp_data
        return data

    def update_stir_rate(self, stir_rates, immediate=False):
        data = {'param': 'stir', 'value': stir_rates,
                'immediate': immediate, 'recurring': True}
        logger.debug('stir rate command: %s' % data)
        self.emit('command', data, namespace='/dpu-evolver')

    def update_temperature(self, temperatures, immediate=False):
        data = {'param': 'temp', 'value': temperatures,
                'immediate': immediate, 'recurring': True}
        logger.debug('temperature command: %s' % data)
        self.emit('command', data, namespace='/dpu-evolver')

    def fluid_command(self, MESSAGE):
        logger.debug('fluid command: %s' % MESSAGE)
        command = {'param': 'pump', 'value': MESSAGE,
                   'recurring': False, 'immediate': True}
        self.emit('command', command, namespace='/dpu-evolver')

    def update_chemo(self, data, vials, bolus_in_s, period_config, immediate=False):
        current_pump = data['config']['pump']['value']

        MESSAGE = {'fields_expected_incoming': 49,
                   'fields_expected_outgoing': 49,
                   'recurring': True,
                   'immediate': immediate,
                   'value': ['--'] * 48,
                   'param': 'pump'}
        for x in vials:
            # stop pumps if period is zero
            if period_config[x] == 0:
                # influx
                MESSAGE['value'][x] = '0|0'
                # efflux
                MESSAGE['value'][x + 16] = '0|0'
            else:
                # media
                MESSAGE['value'][x] = '%.2f|%d' % (
                    bolus_in_s[x], period_config[x])
        # suction
        MESSAGE['value'][-1] = '%.2f|%d' % (max(bolus_in_s) * 2,
                                            max(period_config))

        if MESSAGE['value'] != current_pump:
            logger.info('updating chemostat: %s' % MESSAGE)
            self.emit('command', MESSAGE, namespace='/dpu-evolver')

    def stop_all_pumps(self, ):
        data = {'param': 'pump',
                'value': ['0'] * 48,
                'recurring': False,
                'immediate': True}
        logger.info('stopping all pumps')
        self.emit('command', data, namespace='/dpu-evolver')

    def _create_file(self, vial, param, directory=None, defaults=None):
        if defaults is None:
            defaults = []
        if directory is None:
            directory = param
        file_name = "vial{0}_{1}.txt".format(vial, param)
        file_path = os.path.join(EXP_DIR, directory, file_name)
        text_file = open(file_path, "w")
        for default in defaults:
            text_file.write(default + '\n')
        text_file.close()

    def initialize_exp(self, vials, always_yes=False):
        logger.debug('initializing experiment')

        if os.path.exists(EXP_DIR):
            logger.info('found an existing experiment')
            exp_continue = None
            if always_yes:
                exp_continue = 'y'
            else:
                while exp_continue not in ['y', 'n']:
                    exp_continue = input(
                        'Continue from existing experiment? (y/n): ')
        else:
            exp_continue = 'n'

        if exp_continue == 'n':
            if os.path.exists(EXP_DIR):
                exp_overwrite = None
                if always_yes:
                    exp_overwrite = 'y'
                else:
                    while exp_overwrite not in ['y', 'n']:
                        exp_overwrite = input('Directory aleady exists. '
                                              'Overwrite with new experiment? (y/n): ')
                logger.info('data directory already exists')
                if exp_overwrite == 'y':
                    logger.info('deleting existing data directory')
                    shutil.rmtree(EXP_DIR)
                else:
                    print('Change experiment name in custom_script.py '
                          'and then restart...')
                    logger.warning(
                        'not deleting existing data directory, exiting')
                    sys.exit(1)

            start_time = time.time()

            self.request_calibrations()

            logger.debug('creating data directories')
            os.makedirs(os.path.join(EXP_DIR, 'OD'))
            os.makedirs(os.path.join(EXP_DIR, 'temp'))
            os.makedirs(os.path.join(EXP_DIR, 'temp_config'))
            os.makedirs(os.path.join(EXP_DIR, 'pump_log'))
            os.makedirs(os.path.join(EXP_DIR, 'ODset'))
            os.makedirs(os.path.join(EXP_DIR, 'growthrate'))
            if options.algo == 'chemostat':
                os.makedirs(os.path.join(EXP_DIR, 'chemo_config'))
            elif options.algo == 'morbidostat' or options.algo == 'old_morbidostat' or options.algo == 'timed_morbidostat':
                os.makedirs(os.path.join(EXP_DIR, 'morbido_log'))
            for x in vials:
                exp_str = "Experiment: {0} vial {1}, {2}".format(EXP_NAME,
                                                                 x,
                                                                 time.strftime("%c"))
                # make OD file
                self._create_file(x, 'OD', defaults=[exp_str])
                # make temperature data file
                self._create_file(x, 'temp')
                # make temperature configuration file
                self._create_file(x, 'temp_config',
                                  defaults=[exp_str,
                                            "0,{0}".format(TEMP_INITIAL[x])])
                # make pump log file [time, pump_duration, average (smoothed) OD].
                # Timed morbidostat uses the third column to track states instead of smoothed OD.
                self._create_file(x, 'pump_log',
                                  defaults=[exp_str,
                                            "0,0,-1,-1" if options.algo == 'timed_morbidostat' else "0,0,0"])
                # make ODset file
                self._create_file(x, 'ODset',
                                  defaults=[exp_str,
                                            "0,0"])
                # make growth rate file
                self._create_file(x, 'gr',
                                  defaults=[exp_str,
                                            "0,0"],
                                  directory='growthrate')
                # make chemostat file
                if options.algo == 'chemostat':
                    self._create_file(x, 'chemo_config',
                                      defaults=["0,0,0",
                                                "0,0,0"],
                                      directory='chemo_config')
                elif options.algo == 'morbidostat' or options.algo == 'old_morbidostat' or options.algo == 'timed_morbidostat':
                    # time, p, i, d, pid, drug a conc., drug b conc., phase
                    self._create_file(x, 'morbido_log',
                                      defaults=["0,0,0,0,0,0,0,I"],
                                      directory='morbido_log')
            self.update_stir_rate(options.stir_initial)

            if always_yes:
                exp_blank = 'y'
            else:
                exp_blank = input('Calibrate vials to blank? (y/n): ')
            if exp_blank == 'y':
                # will do it with first broadcast
                self.use_blank = True
                logger.info('will use initial OD measurement as blank')
            else:
                self.use_blank = False
                self.OD_initial = np.zeros(len(vials))
        else:
            # load existing experiment
            pickle_name = "{0}.pickle".format(EXP_NAME)
            pickle_path = os.path.join(EXP_DIR, pickle_name)
            logger.info('loading previous experiment data: %s' % pickle_path)
            with open(pickle_path, 'rb') as f:
                loaded_var = pickle.load(f)
            x = loaded_var
            start_time = x[0]
            self.OD_initial = x[1]

        # copy current custom script to txt file
        backup_filename = '{0}_{1}.txt'.format(EXP_NAME,
                                               time.strftime('%y%m%d_%H%M'))
        shutil.copy('custom_script.py', os.path.join(EXP_DIR,
                                                     backup_filename))
        logger.info('saved a copy of current custom_script.py as %s' %
                    backup_filename)

        return start_time

    def check_for_calibrations(self):
        result = True
        if not os.path.exists(OD_CAL_PATH) or not os.path.exists(TEMP_CAL_PATH):
            # log and request again
            logger.warning('Calibrations not received yet, requesting again')
            self.request_calibrations()
            result = False
        return result

    def save_data(self, data, elapsed_time, vials, parameter):
        if len(data) == 0:
            return
        for x in vials:
            file_name = "vial{0}_{1}.txt".format(x, parameter)
            file_path = os.path.join(EXP_DIR, parameter, file_name)
            text_file = open(file_path, "a+")
            text_file.write("{0},{1}\n".format(elapsed_time, data[x]))
            text_file.close()

    def save_variables(self, start_time, OD_initial):
        # save variables needed for restarting experiment later
        save_path = os.path.dirname(os.path.realpath(__file__))
        pickle_name = "{0}.pickle".format(EXP_NAME)
        pickle_path = os.path.join(EXP_DIR, pickle_name)
        logger.debug('saving all variables: %s' % pickle_path)
        with open(pickle_path, 'wb') as f:
            pickle.dump([start_time, OD_initial], f)

    def get_flow_rate(self):
        file_path = os.path.join(SAVE_PATH, PUMP_CAL_FILE)
        flow_calibration = np.loadtxt(file_path, delimiter="\t")
        if len(flow_calibration) == 16:
            flow_rate = flow_calibration
        else:
            # Currently just implementing influx flow rate
            flow_rate = flow_calibration[0, :]
        return flow_rate

    def calc_growth_rate(self, vial, gr_start, elapsed_time):
        ODfile_name = "vial{0}_OD.txt".format(vial)
        # Grab Data and make setpoint
        OD_path = os.path.join(EXP_DIR, 'OD', ODfile_name)
        OD_data = np.genfromtxt(OD_path, delimiter=',')
        raw_time = OD_data[:, 0]
        raw_OD = OD_data[:, 1]
        raw_time = raw_time[np.isfinite(raw_OD)]
        raw_OD = raw_OD[np.isfinite(raw_OD)]

        # Trim points prior to gr_start
        trim_time = raw_time[np.nonzero(np.where(raw_time > gr_start, 1, 0))]
        trim_OD = raw_OD[np.nonzero(np.where(raw_time > gr_start, 1, 0))]

        # Take natural log, calculate slope
        log_OD = np.log(trim_OD)
        slope, intercept, r_value, p_value, std_err = stats.linregress(
            trim_time[np.isfinite(log_OD)],
            log_OD[np.isfinite(log_OD)])
        logger.debug('growth rate for vial %s: %.2f' % (vial, slope))

        # Save slope to file
        file_name = "vial{0}_gr.txt".format(vial)
        gr_path = os.path.join(EXP_DIR, 'growthrate', file_name)
        text_file = open(gr_path, "a+")
        text_file.write("{0},{1}\n".format(elapsed_time, slope))
        text_file.close()

    def tail_to_np(self, path, window=10, BUFFER_SIZE=512):
        """
        Reads file from the end and returns a numpy array with the data of the last 'window' lines.
        Alternative to np.genfromtxt(path) by loading only the needed lines instead of the whole file.
        """
        f = open(path, 'rb')
        if window == 0:
            return []

        f.seek(0, os.SEEK_END)
        remaining_bytes = f.tell()
        size = window + 1  # Read one more line to avoid broken lines
        block = -1
        data = []

        while size > 0 and remaining_bytes > 0:
            if remaining_bytes - BUFFER_SIZE > 0:
                # Seek back one whole BUFFER_SIZE
                f.seek(block * BUFFER_SIZE, os.SEEK_END)
                # read BUFFER
                bunch = f.read(BUFFER_SIZE)
            else:
                # file too small, start from beginning
                f.seek(0, 0)
                # only read what was not read
                bunch = f.read(remaining_bytes)

            bunch = bunch.decode('utf-8')
            data.append(bunch)
            size -= bunch.count('\n')
            remaining_bytes -= BUFFER_SIZE
            block -= 1

        data = ''.join(reversed(data)).splitlines()[-window:]

        if len(data) < window:
            # Not enough data
            return np.asarray([])

        for c, v in enumerate(data):
            data[c] = v.split(',')

        try:
            data = np.asarray(data, dtype=np.float64)
            return data
        except ValueError:
            # It is reading the header
            return np.asarray([])

    def custom_functions(self, data, vials, elapsed_time):
        # load user script from custom_script.py
        if OPERATION_MODE == 'turbidostat':
            custom_script.turbidostat(self, data, vials, elapsed_time, options)
        elif OPERATION_MODE == 'chemostat':
            custom_script.chemostat(self, data, vials, elapsed_time, options)
        elif OPERATION_MODE == 'morbidostat':
            custom_script.morbidostat(self, data, vials, elapsed_time, options)
        elif OPERATION_MODE == 'timed_morbidostat':
            custom_script.timed_morbidostat(
                self, data, vials, elapsed_time, options)
        elif OPERATION_MODE == 'old_morbidostat':
            custom_script.old_morbidostat(
                self, data, vials, elapsed_time, options)
        else:
            # try to load the user function
            # if failing report to user
            logger.info('user-defined operation mode %s' % OPERATION_MODE)
            try:
                func = getattr(custom_script, OPERATION_MODE)
                func(self, data, vials, elapsed_time)
            except AttributeError:
                logger.error('could not find function %s in custom_script.py' %
                             OPERATION_MODE)
                print('Could not find function %s in custom_script.py '
                      '- Skipping user defined functions' %
                      OPERATION_MODE)

    def stop_exp(self):
        self.stop_all_pumps()


def get_options():
    description = 'Custom eVOLVER script for Toprak Lab. (As a last resort) contact furkancemaltoprak@gmail.com for assistance.'
    parser = argparse.ArgumentParser(description=description)
    # Usage information for our lovely labmates
    parser.add_argument('--always-yes', action='store_true',
                        default=False,
                        help='Answer yes to all questions '
                             '(i.e. continues from existing experiment, '
                             'overwrites existing data and blanks OD '
                             'measurements)')
    parser.add_argument('--log-name',
                        default='evolver.log',
                        help='Log file name directory (default: %(default)s)')

    log_nolog = parser.add_mutually_exclusive_group()
    log_nolog.add_argument('--verbose', action='count',
                           default=0,
                           help='Increase logging verbosity level to DEBUG '
                                '(default: INFO)')
    log_nolog.add_argument('--quiet', action='store_true',
                           default=False,
                           help='Disable logging to file entirely')
    # Always necessary arguments
    algo_options = ['chemostat', 'turbidostat',
                    'morbidostat', 'timed_morbidostat', 'old_morbidostat']
    parser.add_argument(
        '--algo', help='Whether you want to use ' + '/'.join(algo_options))
    parser.add_argument(
        '--exp_name', help="The name of your experiment. Include `expt` in the name for the experiment to be graphed. Ex: expt_12_23_2020")
    parser.add_argument(
        '--vial_volume', help="The capacity of the vials in mL, determined by vial cap straw length", type=int)
    parser.add_argument(
        '--to_avg', help="Number of values to calculate the OD average", type=int)
    parser.add_argument(
        '--stir_initial', help="Initial stir speed (RPS)", type=int)
    parser.add_argument(
        '--temp_initial', help="Initial temperature (C).", type=int)
    # Turbidostat arguments
    parser.add_argument('--lower_threshold',
                        help="Lower OD threshold for all vials", type=float)
    parser.add_argument('--upper_threshold',
                        help="Upper OD threshold for all vials", type=float)
    parser.add_argument(
        '--time_out', help="Additional amount of time (sec) to run suction pump. Unlikely to change between experiments.", type=int)
    parser.add_argument(
        '--pump_wait', help="Minimum amount of time (min) to wait between pump events. Unlikely to change between experiments.", type=int)
    parser.add_argument(
        '--pump_for_max', help="Maximum amount of time (sec) that input pumps can run for. This is for overflow protection. Unlikely to change between experiments. Specify -1 for no maximum.", type=int)

    # Chemostat arguments
    parser.add_argument(
        '--start_od', help="~OD600, set to 0 to start chemostate dilutions at any positive OD", type=float)
    parser.add_argument(
        '--start_time', help="Amount of time (hours) Chemostat starts working after. Set 0 to start immediately", type=float)
    parser.add_argument(
        '--rate_config', help="Dilution rate ~ growth rate (1/hr, NOT mL/hr). Highly effects frequency of dilution rates.", type=float)
    parser.add_argument(
        '--bolus', help="Bolus volume (mL). Change with great caution. 0.2 is the absolute minimum. Unlikely to change between experiments. Highly effects frequency of dilution rates.", type=float)

    # Morbidostat arguments
    # concentration of A, B, and media in M
    parser.add_argument(
        '--a_conc', help="Concentration of Drug A (μM)", type=float)
    parser.add_argument(
        '--b_conc', help="Concentration of Drug B (μM)", type=float)
    parser.add_argument('--middle_threshold',
                        help="Midle OD threshold for all vials", type=float)
    parser.add_argument(
        '--same_drug', help='Whether or not drug A and drug B are the same drug.', type=bool)
    parser.add_argument(
        '--pump_a_for', help='How long to pump drug A for (sec)', type=int
    )
    parser.add_argument(
        '--pump_b_for', help='How long to pump drug A for (sec)', type=int
    )
    parser.add_argument(
        '--pump_media_for', help='How long to pump drug A for (sec)', type=int
    )
    parser.add_argument(
        '--suction_for', help='How long to pump drug A for (sec)', type=int
    )
    # Timed Morbidostat arguments (in addition to morbidostat args)
    parser.add_argument(
        '--freq_a', help="Frequency to administer drug A (hrs).", type=float
    )
    parser.add_argument(
        '--freq_b', help="Frequency to administer drug B (hrs). If use_b flag is disabled, this will be ignored.", type=float
    )
    parser.add_argument(
        '--init_a', help="Time to wait before administering drug A for the first time (hrs).", type=float
    )
    parser.add_argument(
        '--init_b', help="Time to wait before administering drug B for the first time (hrs). If use_b flag is disabled, this will be ignored.", type=float
    )
    parser.add_argument(
        '--times_a', help="Number of cycles in a row to apply drug A.", type=int
    )
    parser.add_argument(
        '--times_b', help="Number of cycles in a row to apply drug B. If use_b flag is disabled, this will be ignored.", type=int
    )
    parser.add_argument(
        '--use_b', help="Boolean flag to enable drug B.", type=bool
    )
    # Sanity check for required arguments
    args = parser.parse_args()
    if args.algo is None or not args.algo in algo_options:
        print('Specify algorithm within the available options.')
        exit(-1)
    if args.exp_name is None or len(args.exp_name) == 0:
        print('Specify experiment name.')
        exit(-1)
    if args.vial_volume is None or args.vial_volume <= 0:
        print('Specify positive vial volume.')
        exit(-1)
    if args.to_avg is None:
        print('Specify positive integer for to_avg')
        exit(-1)
    if args.stir_initial is None or args.stir_initial <= 0:
        print('Specify positive integer for stir_inital')
        exit(-1)
    if args.temp_initial is None:
        print('Specify integer for temp_initial')
        exit(-1)
    # Turbidostat arguments
    if args.algo == 'turbidostat':
        # Sanity check for turbidostat args
        if args.lower_threshold is None or args.lower_threshold < 0:
            print('Specify non-negative lower_threshold')
            exit(-1)
        if args.upper_threshold is None or args.upper_threshold < 0:
            print('Specify non-negative upper_threshold')
            exit(-1)
        if args.time_out is None or args.time_out < 0:
            print('Specify non-negative time_out')
            exit(-1)
        if args.pump_wait is None or args.pump_wait < 0:
            print('Specify non-negative pump_wait')
            exit(-1)
        if args.pump_for_max is None or args.pump_for_max < 0:
            print('Specify non-negative pump_for_max')
            exit(-1)
    # Chemostat arguments
    if args.algo == 'chemostat':
        # Sanity check for chemostat args
        if args.start_od is None:
            print('Specify non-negative start_OD')
            exit(-1)
        if args.start_time is None:
            print('Specify non-negative start_time')
            exit(-1)
        if args.rate_config is None:
            print('Specify rate_config.')
            exit(-1)
        if args.bolus is None or args.bolus < 0.2:
            print('Specify bolus >= 0.2 mL')
            exit(-1)
    # Morbidostat arguments. Old morbidostat has the same arguments.
    if args.algo == "morbidostat" or args.algo == "old_morbidostat" or args.algo == "timed_morbidostat":
        if args.pump_wait is None or args.pump_wait < 0:
            print('Specify non-negative pump_wait')
            exit(-1)
        if args.middle_threshold is None:
            print('Specify a middle_threshold.')
            exit(-1)
        if args.a_conc is None:
            print('Specify concentration for drug A (a_conc).')
            exit(-1)
        if args.b_conc is None:
            print('Specify concentration for drug B (b_conc).')
            exit(-1)
        if args.same_drug is None:
            print('Specify boolean for same_drug (True/False)')
            exit(-1)
    # Timed Morbidostat arguments
    if args.algo == "timed_morbidostat":
        if args.use_b is None:
            print('Specify boolean for use_b (True/False)')
            exit(-1)
        if args.init_a is None or args.init_a < 0:
            print('Specify non-negative init_a')
            exit(-1)
        if args.freq_a is None or args.freq_a < 0:
            print('Specify non-negative freq_a')
            exit(-1)
        if args.times_a is None or args.times_a <= 0:
            print('Specify positive integer for times_a.')
            exit(-1)
        if args.use_b is True:
            if args.init_b is None or args.init_b < 0:
                print('Specify non-negative init_a')
                exit(-1)
            if args.freq_b is None or args.freq_b < 0:
                print('Specify non-negative freq_b')
                exit(-1)
            if args.times_b is None or args.times_b <= 0:
                print('Specify positive integer for times_b.')
                exit(-1)
    return args


if __name__ == '__main__':
    options = get_options()
    OPERATION_MODE = options.algo
    EXP_NAME = options.exp_name
    EXP_DIR = os.path.join(SAVE_PATH, EXP_NAME)
    TEMP_INITIAL = [options.temp_initial] * 16
    print(OPERATION_MODE)
    # changes terminal tab title in OSX
    print('\x1B]0;eVOLVER EXPERIMENT: PRESS Ctrl-C TO PAUSE\x07')

    # silence logging until experiment is initialized
    logging.level = logging.CRITICAL + 10

    socketIO = SocketIO(EVOLVER_IP, EVOLVER_PORT)
    EVOLVER_NS = socketIO.define(EvolverNamespace, '/dpu-evolver')

    # start by stopping any existing chemostat
    EVOLVER_NS.stop_all_pumps()
    #
    EVOLVER_NS.start_time = EVOLVER_NS.initialize_exp(
        VIALS, options.always_yes)

    # logging setup
    if options.quiet:
        logging.basicConfig(level=logging.CRITICAL + 10)
    else:
        if options.verbose == 0:
            level = logging.INFO
        elif options.verbose >= 1:
            level = logging.DEBUG
        logging.basicConfig(format='%(asctime)s - %(name)s - [%(levelname)s] '
                            '- %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            filename=os.path.join(EXP_DIR, options.log_name),
                            level=level)

    reset_connection_timer = time.time()
    while True:
        try:
            # infinite loop
            socketIO.wait(seconds=0.1)
            if time.time() - reset_connection_timer > 3600:
                # reset connection to avoid buildup of broadcast
                # messages (unlikely but could happen for very long
                # experiments with slow dpu code/computer)
                logger.info('resetting connection to eVOLVER to avoid '
                            'potential buildup of broadcast messages')
                socketIO.disconnect()
                socketIO.connect()
                reset_connection_timer = time.time()
        except KeyboardInterrupt:
            try:
                print('Ctrl-C detected, pausing experiment')
                logger.warning('interrupt received, pausing experiment')
                EVOLVER_NS.stop_exp()
                # stop receiving broadcasts
                socketIO.disconnect()
                while True:
                    key = input('Experiment paused. Press enter key to restart '
                                ' or hit Ctrl-C again to terminate experiment')
                    logger.warning('resuming experiment')
                    # no need to have something like "restart_chemo" here
                    # with the new server logic
                    socketIO.connect()
                    break
            except KeyboardInterrupt:
                print('Second Ctrl-C detected, shutting down')
                logger.warning('second interrupt received, terminating '
                               'experiment')
                EVOLVER_NS.stop_exp()
                print('Experiment stopped, goodbye!')
                logger.warning('experiment stopped, goodbye!')
                break
        except Exception as e:
            logger.critical('exception %s stopped the experiment' % str(e))
            print('error "%s" stopped the experiment' % str(e))
            traceback.print_exc(file=sys.stdout)
            EVOLVER_NS.stop_exp()
            print('Experiment stopped, goodbye!')
            logger.warning('experiment stopped, goodbye!')
            break

    # stop experiment one last time
    # covers corner case where user presses Ctrl-C twice quickly
    socketIO.connect()
    EVOLVER_NS.stop_exp()
