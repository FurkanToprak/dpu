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
    
    MESSAGE = ["10"]*48 
    #changes terminal tab title in OSX
    print('\x1B]0;eVOLVER EXPERIMENT: PRESS Ctrl-C TO PAUSE\x07')

    # silence logging until experiment is initialized
    logging.level = logging.CRITICAL + 10
    
    socketIO = SocketIO('192.168.1.2', 8081)
    EVOLVER_NS = socketIO.define(EvolverNamespace, '/dpu-evolver')

    # start by stopping any existing chemostat
    
    #
    EVOLVER_NS.fluid_command(MESSAGE)
