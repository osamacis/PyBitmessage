import base64
from binascii import hexlify
import hashlib
import math
import time
from pprint import pprint
import socket
import struct
import random
import traceback

from addresses import calculateInventoryHash
from debug import logger
from inventory import Inventory
import knownnodes
from network.advanceddispatcher import AdvancedDispatcher
from network.bmobject import BMObject, BMObjectInsufficientPOWError, BMObjectInvalidDataError, BMObjectExpiredError, BMObjectUnwantedStreamError, BMObjectInvalidError, BMObjectAlreadyHaveError
import network.connectionpool
from network.downloadqueue import DownloadQueue
from network.node import Node
import network.asyncore_pollchoose as asyncore
from network.objectracker import ObjectTracker
from network.proxy import Proxy, ProxyError, GeneralProxyError
from network.uploadqueue import UploadQueue, UploadElem, AddrUploadQueue, ObjUploadQueue

import addresses
from bmconfigparser import BMConfigParser
from queues import objectProcessorQueue, portCheckerQueue, UISignalQueue
import shared
import state
import protocol

class BMProtoError(ProxyError): pass


class BMProtoInsufficientDataError(BMProtoError): pass


class BMProtoExcessiveDataError(BMProtoError): pass


class BMProto(AdvancedDispatcher, ObjectTracker):
    # ~1.6 MB which is the maximum possible size of an inv message.
    maxMessageSize = 1600100
    # 2**18 = 256kB is the maximum size of an object payload
    maxObjectPayloadSize = 2**18
    # protocol specification says max 1000 addresses in one addr command
    maxAddrCount = 1000
    # protocol specification says max 50000 objects in one inv command
    maxObjectCount = 50000
    # address is online if online less than this many seconds ago
    addressAlive = 10800
    # maximum time offset
    maxTimeOffset = 3600

#    def __init__(self, address=None, sock=None):
#        AdvancedDispatcher.__init__(self, sock)
#        self.verackReceived = False
#        self.verackSent = False
#        self.lastTx = time.time()
#        self.streams = [0]
#        self.fullyEstablished = False
#        self.connectedAt = 0
#        self.skipUntil = 0
#        if address is None and sock is not None:
#            self.destination = state.Peer(sock.getpeername()[0], sock.getpeername()[1])
#            self.isOutbound = False
#            TLSDispatcher.__init__(self, sock, server_side=True)
#            self.connectedAt = time.time()
#            #print "received connection in background from %s:%i" % (self.destination.host, self.destination.port)
#        else:
#            self.destination = address
#            self.isOutbound = True
#            if ":" in address.host:
#                self.create_socket(socket.AF_INET6, socket.SOCK_STREAM)
#            else:
#                self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
#            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
#            TLSDispatcher.__init__(self, sock, server_side=False)
#            self.connect(self.destination)
#            #print "connecting in background to %s:%i" % (self.destination.host, self.destination.port)
#        shared.connectedHostsList[self.destination] = 0
#        ObjectTracker.__init__(self)
#        UISignalQueue.put(('updateNetworkStatusTab', 'no data'))

    def bm_proto_reset(self):
        self.magic = None
        self.command = None
        self.payloadLength = 0
        self.checksum = None
        self.payload = None
        self.invalid = False
        self.payloadOffset = 0
        self.object = None

    def state_bm_header(self):
        #print "%s:%i: header" % (self.destination.host, self.destination.port)
        if len(self.read_buf) < protocol.Header.size:
            #print "Length below header size"
            return False
        self.magic, self.command, self.payloadLength, self.checksum = protocol.Header.unpack(self.read_buf[:protocol.Header.size])
        self.command = self.command.rstrip('\x00')
        if self.magic != 0xE9BEB4D9:
            # skip 1 byte in order to sync
            self.bm_proto_reset()
            self.set_state("bm_header", 1)
            print "Bad magic"
            self.close()
            return False
        if self.payloadLength > BMProto.maxMessageSize:
            self.invalid = True
        self.set_state("bm_command", protocol.Header.size)
        return True
        
    def state_bm_command(self):
        if len(self.read_buf) < self.payloadLength:
            #print "Length below announced object length"
            return False
        print "%s:%i: command %s (%ib)" % (self.destination.host, self.destination.port, self.command, self.payloadLength)
        self.payload = self.read_buf[:self.payloadLength]
        if self.checksum != hashlib.sha512(self.payload).digest()[0:4]:
            print "Bad checksum, ignoring"
            self.invalid = True
        retval = True
        if not self.fullyEstablished and self.command not in ("version", "verack"):
            logger.error("Received command %s before connection was fully established, ignoring", self.command)
            self.invalid = True
        if not self.invalid:
            try:
                retval = getattr(self, "bm_command_" + str(self.command).lower())()
            except AttributeError:
                # unimplemented command
                print "unimplemented command %s" % (self.command)
            except BMProtoInsufficientDataError:
                print "packet length too short, skipping"
            except BMProtoExcessiveDataError:
                print "too much data, skipping"
            except BMObjectInsufficientPOWError:
                print "insufficient PoW, skipping"
            except BMObjectInvalidDataError:
                print "object invalid data, skipping"
            except BMObjectExpiredError:
                print "object expired, skipping"
            except BMObjectUnwantedStreamError:
                print "object not in wanted stream, skipping"
            except BMObjectInvalidError:
                print "object invalid, skipping"
            except BMObjectAlreadyHaveError:
                print "already got object, skipping"
            except struct.error:
                print "decoding error, skipping"
        else:
            #print "Skipping command %s due to invalid data" % (self.command)
            print "Closing due to invalid data" % (self.command)
            self.close()
            return False
        if retval:
            self.set_state("bm_header", self.payloadLength)
            self.bm_proto_reset()
        # else assume the command requires a different state to follow
        return True

    def decode_payload_string(self, length):
        value = self.payload[self.payloadOffset:self.payloadOffset+length]
        self.payloadOffset += length
        return value

    def decode_payload_varint(self):
        value, offset = addresses.decodeVarint(self.payload[self.payloadOffset:])
        self.payloadOffset += offset
        return value

    def decode_payload_node(self):
        services, host, port = self.decode_payload_content("Q16sH")
        if host[0:12] == '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xFF\xFF':
            host = socket.inet_ntop(socket.AF_INET, host[12:])
        elif host[0:6] == '\xfd\x87\xd8\x7e\xeb\x43':
            # Onion, based on BMD/bitcoind
            host = base64.b32encode(host[6:]).lower() + ".onion"
        else:
            host = socket.inet_ntop(socket.AF_INET6, host)
        if host == "":
            # This can happen on Windows systems which are not 64-bit compatible 
            # so let us drop the IPv6 address. 
            host = socket.inet_ntop(socket.AF_INET, host[12:])

        return Node(services, host, port)

    def decode_payload_content(self, pattern = "v"):
        # l = varint indicating the length of the next array
        # L = varint indicating the length of the next item
        # v = varint (or array)
        # H = uint16
        # I = uint32
        # Q = uint64
        # i = net_addr (without time and stream number)
        # s = string
        # 0-9 = length of the next item
        # , = end of array

        retval = []
        size = None
        insideDigit = False
        i = 0

        while i < len(pattern):
            if pattern[i] in "0123456789" and (i == 0 or pattern[i-1] not in "lL"):
                if size is None:
                    size = 0
                size = size * 10 + int(pattern[i])
                i += 1
                continue
            elif pattern[i] == "l" and size is None:
                size = self.decode_payload_varint()
                i += 1
                continue
            elif pattern[i] == "L" and size is None:
                size = self.decode_payload_varint()
                i += 1
                continue
            if size is not None:
                if pattern[i] == "s":
                    retval.append(self.payload[self.payloadOffset:self.payloadOffset + size])
                    self.payloadOffset += size
                    i += 1
                else:
                    if "," in pattern[i:]:
                        subpattern = pattern[i:pattern.index(",")]
                    else:
                        subpattern = pattern[i:]

                    for j in range(size):
                        if pattern[i-1:i] == "L":
                            retval.extend(self.decode_payload_content(subpattern))
                        else:
                            retval.append(self.decode_payload_content(subpattern))
                    i += len(subpattern)
                size = None
            else:
                if pattern[i] == "v":
                    retval.append(self.decode_payload_varint())
                if pattern[i] == "i":
                    retval.append(self.decode_payload_node())
                if pattern[i] == "H":
                    retval.append(struct.unpack(">H", self.payload[self.payloadOffset:self.payloadOffset+2])[0])
                    self.payloadOffset += 2
                if pattern[i] == "I":
                    retval.append(struct.unpack(">I", self.payload[self.payloadOffset:self.payloadOffset+4])[0])
                    self.payloadOffset += 4
                if pattern[i] == "Q":
                    retval.append(struct.unpack(">Q", self.payload[self.payloadOffset:self.payloadOffset+8])[0])
                    self.payloadOffset += 8
                i += 1
        if self.payloadOffset > self.payloadLength:
            print "Insufficient data %i/%i" % (self.payloadOffset, self.payloadLength)
            raise BMProtoInsufficientDataError()
        return retval

    def bm_command_error(self):
        fatalStatus, banTime, inventoryVector, errorText = self.decode_payload_content("vvlsls")
        print "%s:%i error: %i, %s" % (self.destination.host, self.destination.port, fatalStatus, errorText)
        return True

    def bm_command_getdata(self):
        items = self.decode_payload_content("L32s")
#        if time.time() < self.skipUntil:
#            print "skipping getdata"
#            return True
        for i in items:
            #print "received getdata request for item %s" % (hexlify(i))
            #logger.debug('received getdata request for item:' + hexlify(i))
            #if i in ObjUploadQueue.streamElems(1):
            if False:
                self.antiIntersectionDelay()
            else:
                self.receiveQueue.put(("object", i))
        return True

    def bm_command_inv(self):
        items = self.decode_payload_content("L32s")

        if len(items) >= BMProto.maxObjectCount:
            logger.error("Too many items in inv message!")
            raise BMProtoExcessiveDataError()
        else:
            pass

        for i in items:
            self.receiveQueue.put(("inv", i))
            self.handleReceivedInventory(i)

        payload = addresses.encodeVarint(len(self.objectsNewToMe)) + ''.join(self.objectsNewToMe.keys())
        self.writeQueue.put(protocol.CreatePacket('getdata', payload))
        return True

    def bm_command_object(self):
        objectOffset = self.payloadOffset
        nonce, expiresTime, objectType, version, streamNumber = self.decode_payload_content("QQIvv")
        self.object = BMObject(nonce, expiresTime, objectType, version, streamNumber, self.payload)

        if len(self.payload) - self.payloadOffset > BMProto.maxObjectPayloadSize:
            logger.info('The payload length of this object is too large (%s bytes). Ignoring it.' % len(self.payload) - self.payloadOffset)
            raise BMProtoExcessiveDataError()

        self.object.checkProofOfWorkSufficient()
        self.object.checkEOLSanity()
        self.object.checkStream()

        try:
            if self.object.objectType == protocol.OBJECT_GETPUBKEY:
                self.object.checkGetpubkey()
            elif self.object.objectType == protocol.OBJECT_PUBKEY:
                self.object.checkPubkey(self.payload[self.payloadOffset:self.payloadOffset+32])
            elif self.object.objectType == protocol.OBJECT_MSG:
                self.object.checkMessage()
            elif self.object.objectType == protocol.OBJECT_BROADCAST:
                self.object.checkBroadcast(self.payload[self.payloadOffset:self.payloadOffset+32])
            # other objects don't require other types of tests
        except BMObjectAlreadyHaveError:
            pass
        else:
            Inventory()[self.object.inventoryHash] = (
                    self.object.objectType, self.object.streamNumber, self.payload[objectOffset:], self.object.expiresTime, self.object.tag)
            objectProcessorQueue.put((self.object.objectType,self.object.data))
            #DownloadQueue().task_done(self.object.inventoryHash)
            network.connectionpool.BMConnectionPool().handleReceivedObject(self, self.object.streamNumber, self.object.inventoryHash)
            #ObjUploadQueue().put(UploadElem(self.object.streamNumber, self.object.inventoryHash))
            #broadcastToSendDataQueues((streamNumber, 'advertiseobject', inventoryHash))
        return True

    def _decode_addr(self):
        return self.decode_payload_content("lQIQ16sH")

    def bm_command_addr(self):
        addresses = self._decode_addr()
        for i in addresses:
            seenTime, stream, services, ip, port = i
            decodedIP = protocol.checkIPAddress(ip)
            if stream not in state.streamsInWhichIAmParticipating:
                continue
            #print "maybe adding %s in stream %i to knownnodes (%i)" % (decodedIP, stream, len(knownnodes.knownNodes[stream]))
            if decodedIP is not False and seenTime > time.time() - BMProto.addressAlive:
                peer = state.Peer(decodedIP, port)
                if peer in knownnodes.knownNodes[stream] and knownnodes.knownNodes[stream][peer] > seenTime:
                    continue
                knownnodes.knownNodes[stream][peer] = seenTime
                AddrUploadQueue().put((stream, peer))
        return True

    def bm_command_portcheck(self):
        portCheckerQueue.put(state.Peer(self.destination, self.peerNode.port))
        return True

    def bm_command_ping(self):
        self.writeQueue.put(protocol.CreatePacket('pong'))
        return True

    def bm_command_pong(self):
        # nothing really
        return True

    def bm_command_verack(self):
        self.verackReceived = True
        if self.verackSent:
            if self.isSSL:
                self.set_state("tls_init", self.payloadLength)
                self.bm_proto_reset()
                return False
            else:
                self.set_connection_fully_established()
                return True
        return True

    def bm_command_version(self):
        #self.remoteProtocolVersion, self.services, self.timestamp, padding1, self.myExternalIP, padding2, self.remoteNodeIncomingPort = protocol.VersionPacket.unpack(self.payload[:protocol.VersionPacket.size])
        self.remoteProtocolVersion, self.services, self.timestamp, self.sockNode, self.peerNode, self.nonce, self.userAgent, self.streams = self.decode_payload_content("IQQiiQlslv")
        self.nonce = struct.pack('>Q', self.nonce)
        self.timeOffset = self.timestamp - int(time.time())
        #print "remoteProtocolVersion: %i" % (self.remoteProtocolVersion)
        #print "services: %08X" % (self.services)
        #print "time offset: %i" % (self.timestamp - int(time.time()))
        #print "my external IP: %s" % (self.sockNode.host)
        #print "remote node incoming port: %i" % (self.peerNode.port)
        #print "user agent: %s" % (self.userAgent)
        #print "streams: [%s]" % (",".join(map(str,self.streams)))
        if not self.peerValidityChecks():
            # TODO ABORT
            return True
        #shared.connectedHostsList[self.destination] = self.streams[0]
        self.writeQueue.put(protocol.CreatePacket('verack'))
        self.verackSent = True
        if not self.isOutbound:
            self.writeQueue.put(protocol.assembleVersionMessage(self.destination.host, self.destination.port, network.connectionpool.BMConnectionPool().streams, True))
            print "%s:%i: Sending version"  % (self.destination.host, self.destination.port)
        if ((self.services & protocol.NODE_SSL == protocol.NODE_SSL) and
                protocol.haveSSL(not self.isOutbound)):
            self.isSSL = True
        if self.verackReceived:
            if self.isSSL:
                self.set_state("tls_init", self.payloadLength)
                self.bm_proto_reset()
                return False
            else:
                self.set_connection_fully_established()
                return True
        return True

    def peerValidityChecks(self):
        if self.remoteProtocolVersion < 3:
            self.writeQueue.put(protocol.assembleErrorMessage(fatal=2,
                errorText="Your is using an old protocol. Closing connection."))
            logger.debug ('Closing connection to old protocol version %s, node: %s',
                str(self.remoteProtocolVersion), str(self.destination))
            return False
        if self.timeOffset > BMProto.maxTimeOffset:
            self.writeQueue.put(protocol.assembleErrorMessage(fatal=2,
                errorText="Your time is too far in the future compared to mine. Closing connection."))
            logger.info("%s's time is too far in the future (%s seconds). Closing connection to it.",
                self.destination, self.timeOffset)
            shared.timeOffsetWrongCount += 1
            return False
        elif self.timeOffset < -BMProto.maxTimeOffset:
            self.writeQueue.put(protocol.assembleErrorMessage(fatal=2,
                errorText="Your time is too far in the past compared to mine. Closing connection."))
            logger.info("%s's time is too far in the past (timeOffset %s seconds). Closing connection to it.",
                self.destination, self.timeOffset)
            shared.timeOffsetWrongCount += 1
            return False
        else:
            shared.timeOffsetWrongCount = 0
        if len(self.streams) == 0:
            self.writeQueue.put(protocol.assembleErrorMessage(fatal=2,
                errorText="We don't have shared stream interests. Closing connection."))
            logger.debug ('Closed connection to %s because there is no overlapping interest in streams.',
                str(self.destination))
            return False
        if self.destination in network.connectionpool.BMConnectionPool().inboundConnections:
            try:
                if not protocol.checkSocksIP(self.destination.host):
                    self.writeQueue.put(protocol.assembleErrorMessage(fatal=2,
                        errorText="Too many connections from your IP. Closing connection."))
                    logger.debug ('Closed connection to %s because we are already connected to that IP.',
                        str(self.destination))
                    return False
            except:
                pass
        if self.nonce == protocol.eightBytesOfRandomDataUsedToDetectConnectionsToSelf:
            self.writeQueue.put(protocol.assembleErrorMessage(fatal=2,
                errorText="I'm connected to myself. Closing connection."))
            logger.debug ("Closed connection to %s because I'm connected to myself.",
                str(self.destination))

        return True

    @staticmethod
    def assembleAddr(peerList):
        if type(peerList) is state.Peer:
            peerList = (peerList)
        # TODO handle max length, now it's done by upper layers
        payload = addresses.encodeVarint(len(peerList))
        for address in peerList:
            stream, peer, timestamp = address
            payload += struct.pack(
                '>Q', timestamp)  # 64-bit time
            payload += struct.pack('>I', stream)
            payload += struct.pack(
                '>q', 1)  # service bit flags offered by this node
            payload += protocol.encodeHost(peer.host)
            payload += struct.pack('>H', peer.port)  # remote port
        return protocol.CreatePacket('addr', payload)

    def handle_connect_event(self):
        try:
            asyncore.dispatcher.handle_connect_event(self)
            self.connectedAt = time.time()
        except socket.error as e:
            #print "%s:%i: socket error: %s" % (self.destination.host, self.destination.port, str(e))
            self.close()

    def handle_read_event(self):
        try:
            asyncore.dispatcher.handle_read_event(self)
        except socket.error as e:
            #print "%s:%i: socket error: %s" % (self.destination.host, self.destination.port, str(e))
            self.close()

    def handle_write_event(self):
        try:
            asyncore.dispatcher.handle_write_event(self)
        except socket.error as e:
            #print "%s:%i: socket error: %s" % (self.destination.host, self.destination.port, str(e))
            self.close()

    def close(self, reason=None):
        self.set_state("close")
#        if reason is None:
#            print "%s:%i: closing" % (self.destination.host, self.destination.port)
#            #traceback.print_stack()
#        else:
#            print "%s:%i: closing, %s" % (self.destination.host, self.destination.port, reason)
        network.connectionpool.BMConnectionPool().removeConnection(self)
        AdvancedDispatcher.close(self)