Python 3.6.8 (tags/v3.6.8:3c6b436a57, Dec 24 2018, 00:16:47) [MSC v.1916 64 bit (AMD64)] on win32
Type "help", "copyright", "credits" or "license()" for more information.
>>> import os
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
from custom_script import EXP_NAME, PUMP_CAL_FILE
from custom_script import EVOLVER_IP, EVOLVER_PORT, OPERATION_MODE
from custom_script import STIR_INITIAL, TEMP_INITIAL

def fluid_command(self, MESSAGE):
        logger.debug('fluid command: %s' % MESSAGE)
        command = {'param': 'pump', 'value': MESSAGE,
                   'recurring': False ,'immediate': True}
        self.emit('command', command, namespace='/dpu-evolver')
def stop_all_pumps(self, ):
        data = {'param': 'pump',
                'value': ['0'] * 48,
                'recurring': False,
                'immediate': True}
        logger.info('stopping all pumps')
        self.emit('command', data, namespace = '/dpu-evolver')

if __name__ == '__main__':
    options = get_options()
    MESSAGE = ["05"]*48 
    #changes terminal tab title in OSX
    print('\x1B]0;eVOLVER EXPERIMENT: PRESS Ctrl-C TO PAUSE\x07')

    # silence logging until experiment is initialized
    logging.level = logging.CRITICAL + 10
    
    socketIO = SocketIO(EVOLVER_IP, EVOLVER_PORT)
    EVOLVER_NS = socketIO.define(EvolverNamespace, '/dpu-evolver')

    # start by stopping any existing chemostat
    EVOLVER_NS.stop_all_pumps()
    #
    EVOLVER_NS.fluid_command(MESSAGE)
