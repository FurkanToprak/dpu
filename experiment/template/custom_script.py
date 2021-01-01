#!/usr/bin/env python3
import numpy as np
import logging
import os.path
import time
import argparse

##### IMPORTANT #####
# Read the README.md file before touching this file.

# logger setup
logger = logging.getLogger(__name__)

##### USER DEFINED GENERAL SETTINGS #####

# Our evolver only allows for 15 vials, as we've rewired the 16th to a centralized suction pump.
tubeCount = 15
EVOLVER_IP = '192.168.1.2'
EVOLVER_PORT = 8081

# if using a different mode, name your function as the OPERATION_MODE variable


def chemostat(eVOLVER, input_data, vials, elapsed_time, options):
    # OD_data = input_data['transformed']['od_90']
    start_od = options.start_od
    start_time = options.start_time
    # Number of values to calculate the OD average
    OD_values_to_average = options.to_avg
    chemostat_vials = vials
    # to set all vials to the same value, creates 16-value list
    rate_config = [options.rate_config] * 16
    ##### Chemostat Settings #####
    bolus = options.bolus
    exp_name = options.exp_name
    vial_volume = options.vial_volume
    ##### End of Chemostat Settings #####

    save_path = os.path.dirname(os.path.realpath(__file__))  # save path
    flow_rate = eVOLVER.get_flow_rate()  # read from calibration file
    period_config = [0, 0, 0, 0, 0, 0, 0, 0, 0,
                     0, 0, 0, 0, 0, 0, 0]  # initialize array
    bolus_in_s = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                  0, 0, 0, 0, 0, 0]  # initialize array

    ##### Chemostat Control Code Below #####

    for x in chemostat_vials:  # main loop through each vial

        # Update chemostat configuration files for each vial

        # initialize OD and find OD path
        file_name = "vial{0}_OD.txt".format(x)
        OD_path = os.path.join(save_path, exp_name, 'OD', file_name)
        data = eVOLVER.tail_to_np(OD_path, OD_values_to_average)
        average_OD = 0
        # enough_ODdata = (len(data) > 7) #logical, checks to see if enough data points (couple minutes) for sliding window

        # waits for seven OD measurements (couple minutes) for sliding window
        if data.size != 0:

            # calculate median OD
            od_values_from_file = data[:, 1]
            average_OD = float(np.median(od_values_from_file))

            # set chemostat config path and pull current state from file
            file_name = "vial{0}_chemo_config.txt".format(x)
            chemoconfig_path = os.path.join(save_path, exp_name,
                                            'chemo_config', file_name)
            chemo_config = np.genfromtxt(chemoconfig_path, delimiter=',')
            # should t=0 initially, changes each time a new command is written to file
            last_chemoset = chemo_config[len(chemo_config)-1][0]
            # should be zero initially, changes each time a new command is written to file
            last_chemophase = chemo_config[len(chemo_config)-1][1]
            # should be 0 initially, then period in seconds after new commands are sent
            last_chemorate = chemo_config[len(chemo_config)-1][2]

            # once start time has passed and culture hits start OD, if no command has been written, write new chemostat command to file
            if ((elapsed_time > start_time) & (average_OD > start_od)):

                # calculate time needed to pump bolus for each pump
                bolus_in_s[x] = bolus/flow_rate[x]

                # calculate the period (i.e. frequency of dilution events) based on user specified growth rate and bolus size
                if rate_config[x] > 0:
                    # scale dilution rate by bolus size and volume
                    period_config[x] = (3600*bolus) / \
                        ((rate_config[x])*vial_volume)
                else:  # if no dilutions needed, then just loops with no dilutions
                    period_config[x] = 0

                if (last_chemorate != period_config[x]):
                    print('Chemostat updated in vial {0}'.format(x))
                    logger.info('chemostat initiated for vial %d, period %.2f'
                                % (x, period_config[x]))
                    # writes command to chemo_config file, for storage
                    text_file = open(chemoconfig_path, "a+")
                    # note that this changes chemophase
                    text_file.write("{0},{1},{2}\n".format(
                        elapsed_time, (last_chemophase+1), period_config[x]))
                    text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)

    # compares computed chemostat config to the remote one
    eVOLVER.update_chemo(input_data, chemostat_vials,
                         bolus_in_s, period_config)


def turbidostat(eVOLVER, input_data, vials, elapsed_time, options):
    # OD_data = input_data['transformed']['od']
    # Identify pump calibration files, define initial values for temperature, stirring, volume, power settings
    vial_volume = options.vial_volume  # mL, determined by vial cap straw length
    ##### USER DEFINED VARIABLES #####
    # vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials
    turbidostat_vials = vials
    # set to np.inf to never stop, or integer value to stop diluting after certain number of growth curves
    stop_after_n_curves = np.inf
    # Number of values to calculate the OD average
    OD_values_to_average = options.to_avg
    EXP_NAME = options.exp_name  # Name of experiment files
    # to set all vials to the same value, creates 16-value list
    lower_thresh = [options.lower_threshold] * len(vials)
    # to set all vials to the same value, creates 16-value list
    upper_thresh = [options.upper_threshold] * len(vials)
    ##### Turbidostat Settings #####
    # Tunable settings for overflow protection, pump scheduling etc.
    # (sec) additional amount of time to run efflux pump
    time_out = options.time_out
    # (min) minimum amount of time to wait between pump events
    pump_wait = options.pump_wait
    # (sec) max amount to run influx pumps
    pump_max = options.pump_max
    ##### End of Turbidostat Settings #####
    save_path = os.path.dirname(os.path.realpath(__file__))  # save path
    flow_rate = eVOLVER.get_flow_rate()  # read from calibration file
    ##### Turbidostat Control Code Below #####
    # maximum of all pump times (to prevent overflow of vials)
    max_time_in = 0
    # fluidic message: initialized so that no change is sent
    MESSAGE = ['--'] * 48
    for x in turbidostat_vials:  # main loop through each vial
        # Update turbidostat configuration files for each vial
        # initialize OD and find OD path
        file_name = "vial{0}_ODset.txt".format(x)
        ODset_path = os.path.join(save_path, EXP_NAME, 'ODset', file_name)
        data = np.genfromtxt(ODset_path, delimiter=',')
        ODset = data[len(data)-1][1]
        ODsettime = data[len(data)-1][0]
        num_curves = len(data)/2
        file_name = "vial{0}_OD.txt".format(x)
        OD_path = os.path.join(save_path, EXP_NAME, 'OD', file_name)
        data = eVOLVER.tail_to_np(OD_path, OD_values_to_average)
        average_OD = 0

        # Determine whether turbidostat dilutions are needed
        # enough_ODdata = (len(data) > 7) #logical, checks to see if enough data points (couple minutes) for sliding window
        # logical, checks to see if enough growth curves have happened
        collecting_more_curves = (num_curves <= (stop_after_n_curves + 2))

        if data.size != 0:
            # Take median to avoid outlier
            od_values_from_file = data[:, 1]
            average_OD = float(np.median(od_values_from_file))

            # if recently exceeded upper threshold, note end of growth curve in ODset, allow dilutions to occur and growthrate to be measured
            if (average_OD > upper_thresh[x]) and (ODset != lower_thresh[x]):
                text_file = open(ODset_path, "a+")
                text_file.write("{0},{1}\n".format(
                    elapsed_time, lower_thresh[x]))
                text_file.close()
                ODset = lower_thresh[x]
                # calculate growth rate
                eVOLVER.calc_growth_rate(x, ODsettime, elapsed_time)

            # if have approx. reached lower threshold, note start of growth curve in ODset
            if (average_OD < (lower_thresh[x] + (upper_thresh[x] - lower_thresh[x]) / 3)) and (ODset != upper_thresh[x]):
                text_file = open(ODset_path, "a+")
                text_file.write("{0},{1}\n".format(
                    elapsed_time, upper_thresh[x]))
                text_file.close()
                ODset = upper_thresh[x]

            # if need to dilute to lower threshold, then calculate amount of time to pump
            if average_OD > ODset and collecting_more_curves:

                time_in = - \
                    (np.log(lower_thresh[x]/average_OD)
                     * vial_volume)/flow_rate[x]
                # If pump_for_max is -1, then not set.
                if pump_for_max < 0 and time_in > pump_for_max:
                    time_in = pump_for_max

                time_in = round(time_in, 2)
                max_time_in = max(max_time_in, time_in)
                file_name = "vial{0}_pump_log.txt".format(x)
                file_path = os.path.join(save_path, EXP_NAME,
                                         'pump_log', file_name)
                data = np.genfromtxt(file_path, delimiter=',')
                last_pump = data[len(data)-1][0]
                # if sufficient time since last pump, send command to Arduino
                if ((elapsed_time - last_pump)*60) >= pump_wait:
                    logger.info('turbidostat dilution for vial %d' % x)
                    # media pump
                    MESSAGE[x] = str(time_in)

                    file_name = "vial{0}_pump_log.txt".format(x)
                    file_path = os.path.join(
                        save_path, EXP_NAME, 'pump_log', file_name)

                    text_file = open(file_path, "a+")
                    text_file.write("{0},{1}\n".format(elapsed_time, time_in))
                    text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)

    # here lives the code that controls the suction pump
    MESSAGE[-1] = str(max_time_in + time_out)
    # send fluidic command only if we are actually turning on any of the pumps
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)


def morbidostat(eVOLVER, input_data, vials, elapsed_time, options):
    # Identify pump calibration files, define initial values for temperature, stirring, volume, power settings
    vial_volume = options.vial_volume  # mL, determined by vial cap straw length
    ##### USER DEFINED VARIABLES #####
    # vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials
    morbidostat_vials = vials
    # set to np.inf to never stop, or integer value to stop diluting after certain number of growth curves
    stop_after_n_curves = np.inf
    # Number of values to calculate the OD average
    OD_values_to_average = options.to_avg
    EXP_NAME = options.exp_name  # Name of experiment files
    # Lower threshold to minimize logic arising from noise
    lower_thresh = [options.lower_threshold] * len(vials)
    # Middle threshold
    middle_thresh = [options.middle_threshold] * len(vials)
    # Upper threshold
    upper_thresh = [options.upper_threshold] * len(vials)
    # Drug A Concentration
    a_conc = options.a_conc
    # Drug B Concentration
    b_conc = options.b_conc
    # Media Concentration
    media_conc = options.media_conc
    ##### End of Morbidostat Settings #####
    save_path = os.path.dirname(os.path.realpath(__file__))  # save path
    flow_rate = eVOLVER.get_flow_rate()  # read from calibration file
    ##### Morbidostat Control Code Below #####
    # maximum of all pump times (to prevent overflow of vials)
    max_time_in = 0
    # fluidic message: initialized so that no change is sent
    MESSAGE = ['--'] * 48
    for x in morbidostat_vials:  # main loop through each vial
        pass

    # send fluidic command only if we are actually turning on any of the pumps
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)


if __name__ == '__main__':
    print('Please run eVOLVER.py instead')
