"""Real packet I/O for MRC emulator using scapy.

Provides send and receive capabilities on raw interfaces for live
Containerlab mode.  Requires CAP_NET_RAW + CAP_NET_ADMIN.

Usage::

    pio = PacketIO()
    pio.bind_interface('eth1')
    pio.bind_interface('eth2')
    pio.start_receiver(callback=on_packet)
    pio.send(raw_bytes, iface='eth1')
    pio.stop_receiver()
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ROCE_UDP_PORT = 4971


class PacketIO:
    """Send and receive raw MRC packets on network interfaces.

    Uses scapy for raw packet injection and asynchronous sniffing.
    Falls back gracefully if scapy is unavailable or interfaces
    cannot be opened (e.g. in offline mode).
    """

    def __init__(self) -> None:
        self._interfaces: list[str] = []
        self._sniffers: dict[str, object] = {}
        self._callback: Optional[Callable] = None
        self._running: bool = False
        self._lock = threading.Lock()
        self._scapy_available: bool = False
        self._stats: dict[str, int] = {
            'packets_sent': 0,
            'packets_received': 0,
            'send_errors': 0,
            'receive_errors': 0,
        }

        try:
            import scapy.all  # noqa: F401
            self._scapy_available = True
        except ImportError:
            logger.warning('scapy not available — packet I/O disabled')

    @property
    def is_available(self) -> bool:
        return self._scapy_available

    def bind_interface(self, iface: str) -> None:
        """Register an interface for sending and receiving."""
        if iface not in self._interfaces:
            self._interfaces.append(iface)
            logger.info('Bound interface %s for packet I/O', iface)

    def send(self, raw_bytes: bytes, iface: str) -> bool:
        """Send a raw Ethernet frame on the specified interface.

        Returns True on success, False on error.
        """
        if not self._scapy_available:
            return False
        try:
            from scapy.all import sendp, Ether
            sendp(Ether(raw_bytes), iface=iface, verbose=False)
            with self._lock:
                self._stats['packets_sent'] += 1
            return True
        except Exception as exc:
            logger.error('Send error on %s: %s', iface, exc)
            with self._lock:
                self._stats['send_errors'] += 1
            return False

    def start_receiver(
        self,
        callback: Callable[[bytes, str], None],
        bpf_filter: str = f'udp dst port {ROCE_UDP_PORT}',
    ) -> None:
        """Start asynchronous packet sniffing on all bound interfaces.

        *callback* is called with ``(raw_bytes, iface_name)`` for each
        received packet matching the BPF filter.
        """
        if not self._scapy_available:
            logger.warning('Cannot start receiver — scapy unavailable')
            return
        if self._running:
            return

        self._callback = callback
        self._running = True

        from scapy.all import AsyncSniffer

        for iface in self._interfaces:
            try:
                sniffer = AsyncSniffer(
                    iface=iface,
                    filter=bpf_filter,
                    prn=lambda pkt, _iface=iface: self._on_packet(pkt, _iface),
                    store=False,
                )
                sniffer.start()
                self._sniffers[iface] = sniffer
                logger.info('Receiver started on %s', iface)
            except Exception as exc:
                logger.error('Failed to start sniffer on %s: %s', iface, exc)

    def stop_receiver(self) -> None:
        """Stop all active sniffers."""
        self._running = False
        for iface, sniffer in self._sniffers.items():
            try:
                sniffer.stop()
                logger.info('Receiver stopped on %s', iface)
            except Exception as exc:
                logger.error('Error stopping sniffer on %s: %s', iface, exc)
        self._sniffers.clear()

    def _on_packet(self, scapy_pkt, iface: str) -> None:
        """Internal callback from scapy sniffer."""
        try:
            raw = bytes(scapy_pkt)
            with self._lock:
                self._stats['packets_received'] += 1
            if self._callback is not None:
                self._callback(raw, iface)
        except Exception as exc:
            logger.error('Receive processing error on %s: %s', iface, exc)
            with self._lock:
                self._stats['receive_errors'] += 1

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0
