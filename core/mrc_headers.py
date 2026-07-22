"""
OCP MRC 1.0 Header Definitions

All MRC packet headers as defined in the OCP Multipath Reliable Connection
Specification Revision 1.0 (03/21/26). Each header is a dataclass with
to_bytes() and from_bytes() serialization methods.

Bit ordering: network byte order (big-endian). Bit positions use the spec
convention where bit 0 is LSB and bit[m:n] is an inclusive range with m as MSB.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class MrcOpcode(IntEnum):
    RDMA_WRITE_FIRST = 0xC6
    RDMA_WRITE_MIDDLE = 0xC7
    RDMA_WRITE_LAST = 0xC8
    RDMA_WRITE_LAST_IMM = 0xC9
    RDMA_WRITE_ONLY = 0xCA
    RDMA_WRITE_ONLY_IMM = 0xCB
    ACKNOWLEDGE = 0xD1
    ENDPOINT_REQUEST = 0xD8
    ENDPOINT_RESPONSE = 0xD9
    RELIABILITY_SACK = 0xDC
    RELIABILITY_NACK = 0xDD
    RELIABILITY_PROBE_REQ = 0xDE


class NackReason(IntEnum):
    TRIMMED = 0x01
    TRIMMED_LASTHOP = 0x02
    NO_BITMAP = 0x06
    NO_PKT_BUFFER = 0x07
    NO_RESOURCE = 0x0A
    PSN_OOR_WINDOW = 0x0B
    UNEXP_EVENT = 0x19


class EVState(IntEnum):
    GOOD = 0
    DENIED = 1
    SKIP = 2
    ASSUMED_BAD = 3


class SackMFlag(IntEnum):
    NONE = 0b00
    SKIP_ONCE = 0b01
    ALWAYS_SKIP = 0b10


class EndpointOp(IntEnum):
    PORT_STATUS_UPDATE = 0x00
    EV_PROBE = 0x01


class QPState(IntEnum):
    RESET = 0
    INIT = 1
    RTR = 2
    RTS = 3
    ERROR = 4


# ---------------------------------------------------------------------------
# BTH — Base Transport Header (12 bytes)
# Per Tables 6-3, 6-4 and 7-10, 7-11
# ---------------------------------------------------------------------------

@dataclass
class BTH:
    opcode: int = 0
    se: int = 0
    m: int = 0
    pad: int = 0
    tver: int = 0
    p_key: int = 0
    var_res: int = 0
    dqp: int = 0
    a: int = 0
    r: int = 0
    rtx: int = 0
    ts: int = 0
    inv_res: int = 0
    psn: int = 0

    SIZE = 12

    def to_bytes(self) -> bytes:
        b0 = self.opcode & 0xFF
        b1 = ((self.m & 1) << 7) | ((self.se & 1) << 6) | ((self.pad & 3) << 4) | (self.tver & 0xF)
        p_key = self.p_key & 0xFFFF
        var_res = self.var_res & 0xFF
        dqp_bytes = (self.dqp & 0xFFFFFF).to_bytes(3, 'big')
        flags = ((self.a & 1) << 7) | ((self.r & 1) << 6) | ((self.rtx & 1) << 5) | \
                ((self.ts & 1) << 4) | (self.inv_res & 0xF)
        psn_bytes = (self.psn & 0xFFFFFF).to_bytes(3, 'big')
        return struct.pack('!BBH', b0, b1, p_key) + bytes([var_res]) + dqp_bytes + \
               bytes([flags]) + psn_bytes

    @classmethod
    def from_bytes(cls, data: bytes) -> BTH:
        b0, b1, p_key = struct.unpack_from('!BBH', data, 0)
        var_res = data[4]
        dqp = int.from_bytes(data[5:8], 'big')
        flags = data[8]
        psn = int.from_bytes(data[9:12], 'big')
        return cls(
            opcode=b0,
            m=(b1 >> 7) & 1,
            se=(b1 >> 6) & 1,
            pad=(b1 >> 4) & 3,
            tver=b1 & 0xF,
            p_key=p_key,
            var_res=var_res,
            dqp=dqp,
            a=(flags >> 7) & 1,
            r=(flags >> 6) & 1,
            rtx=(flags >> 5) & 1,
            ts=(flags >> 4) & 1,
            inv_res=flags & 0xF,
            psn=psn,
        )

    def to_dict(self) -> dict:
        return {
            'opcode': f'0x{self.opcode:02X}',
            'opcode_name': MrcOpcode(self.opcode).name if self.opcode in MrcOpcode._value2member_map_ else 'UNKNOWN',
            'm': self.m, 'se': self.se, 'pad': self.pad, 'tver': self.tver,
            'p_key': f'0x{self.p_key:04X}', 'var_res': self.var_res,
            'dqp': self.dqp, 'a': self.a, 'r': self.r, 'rtx': self.rtx,
            'ts': self.ts, 'inv_res': self.inv_res, 'psn': self.psn,
        }


# ---------------------------------------------------------------------------
# METH — Message Extended Transport Header (4 bytes)
# Per Tables 6-8, 6-9
# ---------------------------------------------------------------------------

@dataclass
class METH:
    rqmsn: int = 0
    msn: int = 0

    SIZE = 4

    def to_bytes(self) -> bytes:
        return struct.pack('!HH', self.rqmsn & 0xFFFF, self.msn & 0xFFFF)

    @classmethod
    def from_bytes(cls, data: bytes) -> METH:
        rqmsn, msn = struct.unpack_from('!HH', data, 0)
        return cls(rqmsn=rqmsn, msn=msn)

    def to_dict(self) -> dict:
        return {'rqmsn': self.rqmsn, 'msn': self.msn}


# ---------------------------------------------------------------------------
# TSETH — Requestor Timestamp Extended Header (4 bytes)
# Per Tables 6-6, 6-7
# ---------------------------------------------------------------------------

@dataclass
class TSETH:
    tx_timestamp: int = 0
    tsr: int = 0
    reserved: int = 0
    ftype: int = 0x1

    SIZE = 4

    def to_bytes(self) -> bytes:
        w0 = (self.tx_timestamp & 0xFFFF) << 16
        w0 |= (self.tsr & 1) << 15
        w0 |= (self.reserved & 0x7FF) << 4
        w0 |= self.ftype & 0xF
        return struct.pack('!I', w0)

    @classmethod
    def from_bytes(cls, data: bytes) -> TSETH:
        w0 = struct.unpack_from('!I', data, 0)[0]
        return cls(
            tx_timestamp=(w0 >> 16) & 0xFFFF,
            tsr=(w0 >> 15) & 1,
            reserved=(w0 >> 4) & 0x7FF,
            ftype=w0 & 0xF,
        )

    def to_dict(self) -> dict:
        return {'tx_timestamp': self.tx_timestamp, 'tsr': self.tsr, 'ftype': self.ftype}


# ---------------------------------------------------------------------------
# RETH — RDMA Extended Transport Header (16 bytes)
# Per Table 6-5
# ---------------------------------------------------------------------------

@dataclass
class RETH:
    va: int = 0
    r_key: int = 0
    dmalen: int = 0

    SIZE = 16

    def to_bytes(self) -> bytes:
        return struct.pack('!QII', self.va, self.r_key, self.dmalen)

    @classmethod
    def from_bytes(cls, data: bytes) -> RETH:
        va, r_key, dmalen = struct.unpack_from('!QII', data, 0)
        return cls(va=va, r_key=r_key, dmalen=dmalen)

    def to_dict(self) -> dict:
        return {
            'va': f'0x{self.va:016X}', 'r_key': f'0x{self.r_key:08X}',
            'dmalen': self.dmalen,
        }


# ---------------------------------------------------------------------------
# CC_STATE — Congestion Control State (8 bytes)
# Per Tables 7-12, 7-13
# ---------------------------------------------------------------------------

@dataclass
class CCState:
    tx_timestamp: int = 0
    reserved: int = 0
    ooo_count: int = 0
    r_c: int = 0
    rcv_cwnd_pen: int = 0
    rcvd_bytes: int = 0

    SIZE = 8

    def to_bytes(self) -> bytes:
        w0 = (self.tx_timestamp & 0xFFFF) << 16
        w0 |= (self.reserved & 1) << 15
        w0 |= self.ooo_count & 0x7FFF
        w1 = (self.r_c & 1) << 31
        w1 |= (self.rcv_cwnd_pen & 0x7F) << 24
        w1 |= self.rcvd_bytes & 0xFFFFFF
        return struct.pack('!II', w0, w1)

    @classmethod
    def from_bytes(cls, data: bytes) -> CCState:
        w0, w1 = struct.unpack_from('!II', data, 0)
        return cls(
            tx_timestamp=(w0 >> 16) & 0xFFFF,
            reserved=(w0 >> 15) & 1,
            ooo_count=w0 & 0x7FFF,
            r_c=(w1 >> 31) & 1,
            rcv_cwnd_pen=(w1 >> 24) & 0x7F,
            rcvd_bytes=w1 & 0xFFFFFF,
        )

    def to_dict(self) -> dict:
        return {
            'tx_timestamp': self.tx_timestamp, 'ooo_count': self.ooo_count,
            'r_c': self.r_c, 'rcv_cwnd_pen': self.rcv_cwnd_pen,
            'rcvd_bytes': self.rcvd_bytes,
        }


# ---------------------------------------------------------------------------
# SETH — Reliability SACK Header (28 bytes, followed by 8-byte CC_STATE)
# Per Tables 7-14, 7-15
# ---------------------------------------------------------------------------

@dataclass
class SETH:
    res_type: int = 0
    res_nxt: int = 0
    m: int = 0
    pr: int = 0
    ack_psn_offset: int = 0
    entropy: int = 0
    spdcid: int = 0
    dpdcid: int = 0
    res_byte: int = 0
    cack_psn: int = 0
    cc_type: int = 0
    cc_fl: int = 0
    mpr: int = 0
    sack_offset: int = 0
    sack_bitmap: int = 0
    cc_state: CCState = field(default_factory=CCState)

    SIZE = 36

    def to_bytes(self) -> bytes:
        b0 = ((self.res_type & 0xF) << 4) | (self.res_nxt & 0x1F) >> 1
        b0_full = (self.res_type & 0xF) << 4 | (self.res_nxt & 0x1F) >> 1
        w0_hi = ((self.res_type & 0xF) << 12) | ((self.res_nxt & 0x1F) << 7) | ((self.m & 0x3) << 5)
        w0_lo = ((self.pr & 1) << 1)
        b0 = (w0_hi >> 8) & 0xFF
        b1 = w0_hi & 0xFF
        b2_flags = (w0_lo & 0xFF)
        ack_off_hi = (self.ack_psn_offset >> 8) & 0xFF
        ack_off_lo = self.ack_psn_offset & 0xFF

        buf = bytearray(28)
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nxt >> 1) & 0xF)
        buf[1] = ((self.res_nxt & 1) << 7) | ((self.m & 3) << 5)
        buf[2] = (self.pr & 1) << 3
        buf[2] |= (self.ack_psn_offset >> 12) & 0x07
        buf[3] = (self.ack_psn_offset >> 4) & 0xFF
        # ack_psn_offset lower 4 bits go into buf[4] upper nibble...
        # Actually, let me redo this more carefully from the spec wire format.

        # The SETH is complex. Let me serialize field by field per Table 7-14:
        # Byte 0: type(4) | res_nxt(5)[4:1]
        # Byte 1: res_nxt(5)[0] | m(2) | r(1) | r(1) | r(1) | pr(1) | r(1)
        # Bytes 2-3: ack_psn_offset (16b, signed)
        # Bytes 4-7: entropy (32b)
        # Bytes 8-9: spdcid (16b)
        # Bytes 10-11: dpdcid (16b)
        # Byte 12: res (8b)
        # Bytes 13-15: cack_psn (24b) — but byte 12 has upper 2 bits as res(6) and cack_psn is 24b
        # Actually per the table layout:
        # Row 5 (bytes 12-15): res(8) | cack_psn(24)
        # Row 6 (bytes 16-19): cc_type(4) | cc_fl(4) | mpr(8) | sack_offset(16)
        # Row 7 (bytes 20-23): sack_bitmap[63:32]
        # Row 8 (bytes 24-27): sack_bitmap[31:0]
        # Then 8 bytes CC_STATE

        buf = bytearray(28)
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nxt >> 1) & 0xF)
        buf[1] = ((self.res_nxt & 1) << 7) | ((self.m & 3) << 5)
        buf[2] = (self.pr & 1) << 3
        struct.pack_into('!h', buf, 2, self.ack_psn_offset & 0xFFFF)
        # Overwrite buf[2] upper bits with flags
        buf[2] = (buf[2] & 0x00) | ((self.pr & 1) << 3)
        # Simpler: pack ack_psn_offset as signed 16-bit at offset 2
        ack_off_bytes = struct.pack('!h', self.ack_psn_offset)
        buf[2] = ((self.pr & 1) << 3) | ((ack_off_bytes[0] >> 4) & 0x0F)
        # This is getting messy with bit-packing. Let me use a cleaner approach.

        return self._serialize()

    def _serialize(self) -> bytes:
        buf = bytearray(28)
        # Byte 0: type[7:4] | nxt[4:1]
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nxt >> 1) & 0xF)
        # Byte 1: nxt[0] | m[1:0] | r | r | r | pr | r
        buf[1] = ((self.res_nxt & 1) << 7) | ((self.m & 3) << 5) | ((self.pr & 1) << 1)
        # Bytes 2-3: ack_psn_offset (16-bit signed)
        struct.pack_into('!h', buf, 2, self.ack_psn_offset)
        # Bytes 4-7: entropy (32-bit)
        struct.pack_into('!I', buf, 4, self.entropy)
        # Bytes 8-9: spdcid
        struct.pack_into('!H', buf, 8, self.spdcid)
        # Bytes 10-11: dpdcid
        struct.pack_into('!H', buf, 10, self.dpdcid)
        # Byte 12: reserved
        buf[12] = self.res_byte & 0xFF
        # Bytes 13-15: cack_psn (24-bit)
        cack = (self.cack_psn & 0xFFFFFF).to_bytes(3, 'big')
        buf[13:16] = cack
        # Byte 16: cc_type[7:4] | cc_fl[3:0]
        buf[16] = ((self.cc_type & 0xF) << 4) | (self.cc_fl & 0xF)
        # Byte 17: mpr
        buf[17] = self.mpr & 0xFF
        # Bytes 18-19: sack_offset (16-bit signed)
        struct.pack_into('!h', buf, 18, self.sack_offset)
        # Bytes 20-27: sack_bitmap (64-bit)
        struct.pack_into('!Q', buf, 20, self.sack_bitmap)

        return bytes(buf) + self.cc_state.to_bytes()

    def to_bytes(self) -> bytes:
        return self._serialize()

    @classmethod
    def from_bytes(cls, data: bytes) -> SETH:
        res_type = (data[0] >> 4) & 0xF
        res_nxt = ((data[0] & 0xF) << 1) | ((data[1] >> 7) & 1)
        m = (data[1] >> 5) & 3
        pr = (data[1] >> 1) & 1
        ack_psn_offset = struct.unpack_from('!h', data, 2)[0]
        entropy = struct.unpack_from('!I', data, 4)[0]
        spdcid = struct.unpack_from('!H', data, 8)[0]
        dpdcid = struct.unpack_from('!H', data, 10)[0]
        res_byte = data[12]
        cack_psn = int.from_bytes(data[13:16], 'big')
        cc_type_fl = data[16]
        cc_type = (cc_type_fl >> 4) & 0xF
        cc_fl = cc_type_fl & 0xF
        mpr = data[17]
        sack_offset = struct.unpack_from('!h', data, 18)[0]
        sack_bitmap = struct.unpack_from('!Q', data, 20)[0]
        cc_state = CCState.from_bytes(data[28:36]) if len(data) >= 36 else CCState()
        return cls(
            res_type=res_type, res_nxt=res_nxt, m=m, pr=pr,
            ack_psn_offset=ack_psn_offset, entropy=entropy,
            spdcid=spdcid, dpdcid=dpdcid, res_byte=res_byte,
            cack_psn=cack_psn, cc_type=cc_type, cc_fl=cc_fl,
            mpr=mpr, sack_offset=sack_offset, sack_bitmap=sack_bitmap,
            cc_state=cc_state,
        )

    @property
    def ack_psn(self) -> int:
        return (self.cack_psn + self.ack_psn_offset) & 0xFFFFFF

    @property
    def sack_base_psn(self) -> int:
        return (self.cack_psn + self.sack_offset) & 0xFFFFFF

    def to_dict(self) -> dict:
        return {
            'm': self.m, 'm_name': SackMFlag(self.m).name if self.m in SackMFlag._value2member_map_ else 'UNKNOWN',
            'pr': self.pr, 'ack_psn_offset': self.ack_psn_offset,
            'ack_psn': self.ack_psn,
            'entropy': f'0x{self.entropy:08X}',
            'spdcid': self.spdcid, 'dpdcid': self.dpdcid,
            'cack_psn': self.cack_psn, 'cc_type': self.cc_type,
            'mpr': self.mpr, 'mpr_packets': self.mpr * 128,
            'sack_offset': self.sack_offset,
            'sack_base_psn': self.sack_base_psn,
            'sack_bitmap': f'0x{self.sack_bitmap:016X}',
            'sack_bitmap_bits': format(self.sack_bitmap, '064b'),
            'cc_state': self.cc_state.to_dict(),
        }


# ---------------------------------------------------------------------------
# NETH — Reliability NACK Header (20 bytes)
# Per Tables 7-16, 7-17
# ---------------------------------------------------------------------------

@dataclass
class NETH:
    res_type: int = 0
    res_nxt: int = 0
    reserved: int = 0
    nack_reason: int = 0
    vendor_info: int = 0
    entropy: int = 0
    spdcid: int = 0
    dpdcid: int = 0
    res_byte: int = 0
    nack_psn: int = 0
    cc_type: int = 0x2
    cc_fl: int = 0
    res2: int = 0
    tx_timestamp: int = 0

    SIZE = 20

    def to_bytes(self) -> bytes:
        buf = bytearray(20)
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nxt >> 1) & 0xF)
        buf[1] = ((self.res_nxt & 1) << 7) | (self.reserved & 0x7F)
        buf[2] = self.nack_reason & 0xFF
        buf[3] = self.vendor_info & 0xFF
        struct.pack_into('!I', buf, 4, self.entropy)
        struct.pack_into('!H', buf, 8, self.spdcid)
        struct.pack_into('!H', buf, 10, self.dpdcid)
        buf[12] = self.res_byte & 0xFF
        nack = (self.nack_psn & 0xFFFFFF).to_bytes(3, 'big')
        buf[13:16] = nack
        buf[16] = ((self.cc_type & 0xF) << 4) | (self.cc_fl & 0xF)
        buf[17] = self.res2 & 0xFF
        struct.pack_into('!H', buf, 18, self.tx_timestamp)
        return bytes(buf)

    @classmethod
    def from_bytes(cls, data: bytes) -> NETH:
        res_type = (data[0] >> 4) & 0xF
        res_nxt = ((data[0] & 0xF) << 1) | ((data[1] >> 7) & 1)
        reserved = data[1] & 0x7F
        nack_reason = data[2]
        vendor_info = data[3]
        entropy = struct.unpack_from('!I', data, 4)[0]
        spdcid = struct.unpack_from('!H', data, 8)[0]
        dpdcid = struct.unpack_from('!H', data, 10)[0]
        res_byte = data[12]
        nack_psn = int.from_bytes(data[13:16], 'big')
        cc_type = (data[16] >> 4) & 0xF
        cc_fl = data[16] & 0xF
        res2 = data[17]
        tx_timestamp = struct.unpack_from('!H', data, 18)[0]
        return cls(
            res_type=res_type, res_nxt=res_nxt, reserved=reserved,
            nack_reason=nack_reason, vendor_info=vendor_info,
            entropy=entropy, spdcid=spdcid, dpdcid=dpdcid,
            res_byte=res_byte, nack_psn=nack_psn,
            cc_type=cc_type, cc_fl=cc_fl, res2=res2,
            tx_timestamp=tx_timestamp,
        )

    def to_dict(self) -> dict:
        reason_name = NackReason(self.nack_reason).name if self.nack_reason in NackReason._value2member_map_ else f'0x{self.nack_reason:02X}'
        return {
            'nack_reason': self.nack_reason, 'nack_reason_name': reason_name,
            'vendor_info': self.vendor_info,
            'entropy': f'0x{self.entropy:08X}',
            'spdcid': self.spdcid, 'dpdcid': self.dpdcid,
            'nack_psn': self.nack_psn, 'cc_type': self.cc_type,
            'tx_timestamp': self.tx_timestamp,
        }


# ---------------------------------------------------------------------------
# PETH — Reliability Probe Request Header (16 bytes)
# Per Tables 7-18, 7-19
# ---------------------------------------------------------------------------

@dataclass
class PETH:
    res_type: int = 0
    res_nh: int = 0
    reserved1: int = 0
    reserved2: int = 0
    vendor_info: int = 0
    probe_id: int = 0
    reserved3: int = 0
    spdcid: int = 0
    dpdcid: int = 0
    tx_timestamp: int = 0
    tsr: int = 0
    reserved4: int = 0
    ftype: int = 0x1

    SIZE = 16

    def to_bytes(self) -> bytes:
        buf = bytearray(16)
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nh >> 1) & 0xF)
        buf[1] = ((self.res_nh & 1) << 7) | (self.reserved1 & 0x7F)
        buf[2] = self.reserved2 & 0xFF
        buf[3] = self.vendor_info & 0xFF
        struct.pack_into('!H', buf, 4, self.probe_id)
        struct.pack_into('!H', buf, 6, self.reserved3)
        struct.pack_into('!H', buf, 8, self.spdcid)
        struct.pack_into('!H', buf, 10, self.dpdcid)
        struct.pack_into('!H', buf, 12, self.tx_timestamp)
        ts_flags = ((self.tsr & 1) << 15) | ((self.reserved4 & 0x7FF) << 4) | (self.ftype & 0xF)
        struct.pack_into('!H', buf, 14, ts_flags)
        return bytes(buf)

    @classmethod
    def from_bytes(cls, data: bytes) -> PETH:
        res_type = (data[0] >> 4) & 0xF
        res_nh = ((data[0] & 0xF) << 1) | ((data[1] >> 7) & 1)
        reserved1 = data[1] & 0x7F
        reserved2 = data[2]
        vendor_info = data[3]
        probe_id = struct.unpack_from('!H', data, 4)[0]
        reserved3 = struct.unpack_from('!H', data, 6)[0]
        spdcid = struct.unpack_from('!H', data, 8)[0]
        dpdcid = struct.unpack_from('!H', data, 10)[0]
        tx_timestamp = struct.unpack_from('!H', data, 12)[0]
        ts_flags = struct.unpack_from('!H', data, 14)[0]
        tsr = (ts_flags >> 15) & 1
        reserved4 = (ts_flags >> 4) & 0x7FF
        ftype = ts_flags & 0xF
        return cls(
            res_type=res_type, res_nh=res_nh, reserved1=reserved1,
            reserved2=reserved2, vendor_info=vendor_info,
            probe_id=probe_id, reserved3=reserved3,
            spdcid=spdcid, dpdcid=dpdcid,
            tx_timestamp=tx_timestamp, tsr=tsr,
            reserved4=reserved4, ftype=ftype,
        )

    def to_dict(self) -> dict:
        return {
            'probe_id': self.probe_id, 'spdcid': self.spdcid,
            'dpdcid': self.dpdcid, 'tx_timestamp': self.tx_timestamp,
            'tsr': self.tsr, 'ftype': self.ftype,
        }


# ---------------------------------------------------------------------------
# ERTH — Endpoint Request Header (16 bytes)
# Per Tables 7-20, 7-21
# ---------------------------------------------------------------------------

@dataclass
class ERTH:
    res_type: int = 0
    res_nxt: int = 0
    reserved1: int = 0
    op: int = 0
    reserved2: int = 0
    vendor_info: int = 0
    port_status_mask: int = 0
    reserved3: int = 0
    tx_timestamp: int = 0
    tsr: int = 0
    reserved4: int = 0
    ftype: int = 0x1

    SIZE = 16

    def to_bytes(self) -> bytes:
        buf = bytearray(16)
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nxt >> 1) & 0xF)
        b1_hi = ((self.res_nxt & 1) << 7) | ((self.reserved1 & 0x1F) << 2) | (self.op & 0x3)
        buf[1] = b1_hi
        buf[2] = self.reserved2 & 0xFF
        buf[3] = self.vendor_info & 0xFF
        struct.pack_into('!I', buf, 4, self.port_status_mask)
        struct.pack_into('!I', buf, 8, self.reserved3)
        struct.pack_into('!H', buf, 12, self.tx_timestamp)
        ts_flags = ((self.tsr & 1) << 15) | ((self.reserved4 & 0x7FF) << 4) | (self.ftype & 0xF)
        struct.pack_into('!H', buf, 14, ts_flags)
        return bytes(buf)

    @classmethod
    def from_bytes(cls, data: bytes) -> ERTH:
        res_type = (data[0] >> 4) & 0xF
        res_nxt = ((data[0] & 0xF) << 1) | ((data[1] >> 7) & 1)
        reserved1 = (data[1] >> 2) & 0x1F
        op = data[1] & 0x3
        reserved2 = data[2]
        vendor_info = data[3]
        port_status_mask = struct.unpack_from('!I', data, 4)[0]
        reserved3 = struct.unpack_from('!I', data, 8)[0]
        tx_timestamp = struct.unpack_from('!H', data, 12)[0]
        ts_flags = struct.unpack_from('!H', data, 14)[0]
        tsr = (ts_flags >> 15) & 1
        reserved4 = (ts_flags >> 4) & 0x7FF
        ftype = ts_flags & 0xF
        return cls(
            res_type=res_type, res_nxt=res_nxt, reserved1=reserved1,
            op=op, reserved2=reserved2, vendor_info=vendor_info,
            port_status_mask=port_status_mask, reserved3=reserved3,
            tx_timestamp=tx_timestamp, tsr=tsr,
            reserved4=reserved4, ftype=ftype,
        )

    def to_dict(self) -> dict:
        op_name = EndpointOp(self.op).name if self.op in EndpointOp._value2member_map_ else f'0x{self.op:02X}'
        return {
            'op': self.op, 'op_name': op_name,
            'port_status_mask': f'0x{self.port_status_mask:08X}',
            'port_status_bits': format(self.port_status_mask, '032b'),
            'tx_timestamp': self.tx_timestamp, 'tsr': self.tsr, 'ftype': self.ftype,
        }


# ---------------------------------------------------------------------------
# EETH — Endpoint Response Header (36 bytes)
# Per Tables 7-22, 7-23
# ---------------------------------------------------------------------------

@dataclass
class EETH:
    res_type: int = 0
    res_nxt: int = 0
    r1: int = 0
    op: int = 0
    r2: int = 0
    reserved_words: list = field(default_factory=lambda: [0] * 6)
    tx_timestamp: int = 0
    reserved_tail: int = 0
    reserved_word: int = 0

    SIZE = 36

    def to_bytes(self) -> bytes:
        buf = bytearray(36)
        buf[0] = ((self.res_type & 0xF) << 4) | ((self.res_nxt >> 1) & 0xF)
        buf[1] = ((self.res_nxt & 1) << 7) | ((self.r1 & 1) << 6) | ((self.op & 3) << 4) | \
                 ((self.r2 & 1) << 3)
        for i, w in enumerate(self.reserved_words[:6]):
            struct.pack_into('!I', buf, 4 + i * 4, w)
        struct.pack_into('!H', buf, 28, self.tx_timestamp)
        struct.pack_into('!H', buf, 30, self.reserved_tail)
        struct.pack_into('!I', buf, 32, self.reserved_word)
        return bytes(buf)

    @classmethod
    def from_bytes(cls, data: bytes) -> EETH:
        res_type = (data[0] >> 4) & 0xF
        res_nxt = ((data[0] & 0xF) << 1) | ((data[1] >> 7) & 1)
        r1 = (data[1] >> 6) & 1
        op = (data[1] >> 4) & 3
        r2 = (data[1] >> 3) & 1
        reserved_words = [struct.unpack_from('!I', data, 4 + i * 4)[0] for i in range(6)]
        tx_timestamp = struct.unpack_from('!H', data, 28)[0]
        reserved_tail = struct.unpack_from('!H', data, 30)[0]
        reserved_word = struct.unpack_from('!I', data, 32)[0]
        return cls(
            res_type=res_type, res_nxt=res_nxt, r1=r1, op=op, r2=r2,
            reserved_words=reserved_words, tx_timestamp=tx_timestamp,
            reserved_tail=reserved_tail, reserved_word=reserved_word,
        )

    def to_dict(self) -> dict:
        op_name = EndpointOp(self.op).name if self.op in EndpointOp._value2member_map_ else f'0x{self.op:02X}'
        return {'op': self.op, 'op_name': op_name, 'tx_timestamp': self.tx_timestamp}


# ---------------------------------------------------------------------------
# AETH — ACK Extended Transport Header (4 bytes)
# Standard IBTA AETH used for opcode 0xD1
# ---------------------------------------------------------------------------

@dataclass
class AETH:
    syndrome: int = 0
    msn: int = 0

    SIZE = 4

    def to_bytes(self) -> bytes:
        w = ((self.syndrome & 0xFF) << 24) | (self.msn & 0xFFFFFF)
        return struct.pack('!I', w)

    @classmethod
    def from_bytes(cls, data: bytes) -> AETH:
        w = struct.unpack_from('!I', data, 0)[0]
        return cls(syndrome=(w >> 24) & 0xFF, msn=w & 0xFFFFFF)

    def to_dict(self) -> dict:
        return {'syndrome': f'0x{self.syndrome:02X}', 'msn': self.msn}


# ---------------------------------------------------------------------------
# ImmDt — Immediate Data (4 bytes)
# ---------------------------------------------------------------------------

@dataclass
class ImmDt:
    value: int = 0

    SIZE = 4

    def to_bytes(self) -> bytes:
        return struct.pack('!I', self.value)

    @classmethod
    def from_bytes(cls, data: bytes) -> ImmDt:
        return cls(value=struct.unpack_from('!I', data, 0)[0])

    def to_dict(self) -> dict:
        return {'value': f'0x{self.value:08X}'}


# ---------------------------------------------------------------------------
# Header stack helpers
# ---------------------------------------------------------------------------

OPCODE_HEADER_STACKS = {
    MrcOpcode.RDMA_WRITE_FIRST:    ['METH', 'TSETH?', 'RETH'],
    MrcOpcode.RDMA_WRITE_MIDDLE:   ['METH', 'TSETH?', 'RETH'],
    MrcOpcode.RDMA_WRITE_LAST:     ['METH', 'TSETH?', 'RETH'],
    MrcOpcode.RDMA_WRITE_LAST_IMM: ['METH', 'TSETH?', 'RETH', 'ImmDt'],
    MrcOpcode.RDMA_WRITE_ONLY:     ['METH', 'TSETH?', 'RETH'],
    MrcOpcode.RDMA_WRITE_ONLY_IMM: ['METH', 'TSETH?', 'RETH', 'ImmDt'],
    MrcOpcode.ACKNOWLEDGE:         ['AETH'],
    MrcOpcode.ENDPOINT_REQUEST:    ['ERTH'],
    MrcOpcode.ENDPOINT_RESPONSE:   ['EETH'],
    MrcOpcode.RELIABILITY_SACK:    ['SETH'],
    MrcOpcode.RELIABILITY_NACK:    ['NETH'],
    MrcOpcode.RELIABILITY_PROBE_REQ: ['PETH'],
}

HEADER_CLASSES = {
    'BTH': BTH, 'METH': METH, 'TSETH': TSETH, 'RETH': RETH,
    'SETH': SETH, 'NETH': NETH, 'PETH': PETH,
    'ERTH': ERTH, 'EETH': EETH, 'AETH': AETH, 'ImmDt': ImmDt,
    'CCState': CCState,
}


def get_header_stack(opcode: int, has_tseth: bool = False) -> list[str]:
    stack = OPCODE_HEADER_STACKS.get(opcode, [])
    result = []
    for h in stack:
        if h.endswith('?'):
            if has_tseth:
                result.append(h[:-1])
        else:
            result.append(h)
    return result
