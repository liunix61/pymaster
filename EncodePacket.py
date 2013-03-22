#!/usr/bin/python
#
# vim: tabstop=4 expandtab shiftwidth=4 noautoindent
#
# nymserv.py - A Basic Nymserver for delivering messages to a shared mailbox
# such as alt.anonymous.messages.
#
# Copyright (C) 2012 Steve Crook <steve@mixmin.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 3, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTIBILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.

import struct
import sys
import logging
from Crypto.Cipher import DES3, PKCS1_v1_5
from Crypto.Hash import MD5
from Crypto.PublicKey import RSA
import Crypto.Random
from Config import config
import timing
import Chain
import email
import KeyManager


log = logging.getLogger("Pymaster.EncodePacket")


class ValidationError(Exception):
    pass

class FinalHop():
    """Packet type 1 (final hop):
       Message ID                     [ 16 bytes]
       Initialization vector          [  8 bytes]
    """
    def __init__(self):
        self.messageid = Crypto.Random.get_random_bytes(16)
        self.iv = Crypto.Random.get_random_bytes(8)
    

class EncryptedHeader():
    def __init__(self, msg_type):
        self.make_header(msg_type)

    def make_header(self, msg_type):
        """Packet ID                            [ 16 bytes]
           Triple-DES key                       [ 24 bytes]
           Packet type identifier               [  1 byte ]
           Packet information      [depends on packet type]
           Timestamp                            [  7 bytes]
           Message digest                       [ 16 bytes]
           Random padding               [fill to 328 bytes]
        """
        packetid = Crypto.Random.get_random_bytes(16)
        des3key = Crypto.Random.get_random_bytes(24)
        ts_sig = struct.pack('BBBBB', 48, 48, 48, 48, 0)
        timestamp = ts_sig + struct.pack('<H', timing.epoch_days())
        if msg_type == 1:
            info = FinalHop()
            packet = struct.pack('16s24sB16s8s7s',
                                 packetid,
                                 des3key,
                                 msg_type,
                                 info.messageid,
                                 info.iv,
                                 timestamp)
        digest = MD5.new(data=packet).digest()
        packet += digest
        pad = 328 - len(packet)
        packet += Crypto.Random.get_random_bytes(pad)
        assert len(packet) == 328
        self.des3key = des3key
        self.info = info
        self.packet = packet


class OuterHeader():
    """Public key ID                [  16 bytes]
       Length of RSA-encrypted data [   1 byte ]
       RSA-encrypted session key    [ 128 bytes]
       Initialization vector        [   8 bytes]
       Encrypted header part        [ 328 bytes]
       Padding                      [  31 bytes]
    """
    def __init__(self, rem_data, msg_type):
        self.rem_data = rem_data
        self.make_outer(msg_type)

    def make_outer(self, msg_type):
        keyid = self.rem_data[1].decode('hex')
        des3key = Crypto.Random.get_random_bytes(24)
        pkcs1 = PKCS1_v1_5.new(self.rem_data[4])
        rsakey = pkcs1.encrypt(des3key)
        lenrsa =  len(rsakey)
        assert lenrsa == 128
        iv = Crypto.Random.get_random_bytes(8)
        inner = EncryptedHeader(msg_type)
        desobj = DES3.new(des3key, DES3.MODE_CBC, IV=iv)
        outer_header = struct.pack('16sB128s8s328s31s',
                                   keyid,
                                   lenrsa,
                                   rsakey,
                                   iv,
                                   desobj.encrypt(inner.packet),
                                   Crypto.Random.get_random_bytes(31))
        assert len(outer_header) == 512
        self.inner_header = inner
        self.outer_header = outer_header


class Body():
    def __init__(self, msgobj):
        plain = msgobj.get_payload()
        length = len(plain)
        payload = struct.pack('<L', length)
        payload += self.encode_header(msgobj['To'])
        #TODO Somehow the above process needs to be repeated for header lines.
        payload += plain
        payload += Crypto.Random.get_random_bytes(10240 - len(payload))
        assert len(payload) == 10240
        self.payload = payload

    def encode_header(self, header):
        """This function takes a standard comma-separated header, such as the
        To: header and converts it into the format required by Mixmaster,
        which is:
        Number of destination fields   [        1 byte]
        Destination fields             [ 80 bytes each]
        Number of header line fields   [        1 byte]
        Header lines fields            [ 80 bytes each]
        """
        fields = header.split(',')
        # The return string begins with the single-Byte count of the fields.
        headstr = struct.pack('B', len(fields))
        for field in fields:
            field = field.strip()
            padlen = 80 - len(field)
            headstr += field + ("\x00" * padlen)
        return headstr

class RandHop():
    def __init__(self):
        self.chain = Chain.Chain()
        self.pubring = KeyManager.Pubring()

    def randhop(self, packet):
        rem_data = self.exitnode()
        self.header = OuterHeader(rem_data, 1)
        payload = (self.header.outer_header +
                   Crypto.Random.get_random_bytes(9728))
        assert len(payload) == 10240
        desobj = DES3.new(self.header.inner_header.des3key,
                          DES3.MODE_CBC,
                          IV=self.header.inner_header.info.iv)
        payload += desobj.encrypt(packet.dhead)
        msgobj = email.message.Message()
        msgobj.add_header('To', rem_data[0])
        msgobj.set_payload(self.mixprep(payload))
        return msgobj
        
    def exitnode(self):
        # pubring[0]    Email Address
        # pubring[1]    Key ID (Hex encoded)
        # pubring[2]    Version
        # pubring[3]    Capabilities
        # pubring[4]    Pycrypto Key Object
        name = self.chain.randexit()
        rem_data = self.pubring[name]
        return rem_data

    def mixprep(self, binary):
        """Take a binary string, encode it as Base64 and wrap it to lines of
           length n.
        """
        # This is the wrap width for Mixmaster Base64
        n = 40
        s = binary.encode("base64")
        s = ''.join(s.split("\n"))
        payload = "::\n"
        payload += "Remailer-Type: %s\n\n" % config.get('general', 'version')
        payload += "-----BEGIN REMAILER MESSAGE-----\n"
        while len(s) > 0:
            payload += s[:n] + "\n"
            s = s[n:]
        payload += "-----END REMAILER MESSAGE-----\n"
        return payload


if (__name__ == "__main__"):
    logfmt = config.get('logging', 'format')
    datefmt = config.get('logging', 'datefmt')
    log = logging.getLogger("Pymaster")
    log.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=logfmt, datefmt=datefmt))
    log.addHandler(handler)

    #rem_name = "banana"
    #rem_data = pubring[rem_name]
    #header = OuterHeader(rem_data, 1)
    f = open('/opt/steve/pymaster/testmsg.txt', 'r')
    msg = email.message_from_file(f)
    f.close()
    randhop = RandHop()
    msg = randhop.randhop(msg)
    print msg.as_string()