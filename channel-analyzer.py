#!/usr/bin/env python3
"""Shows live in the terminal how busy each channel is.

The log is read from standard input (`-`) or from a file, which is followed while it grows.
You should also be able to use the logs from the rssi example https://github.com/HannHank/hophop/blob/rx-invalid-header/examples/src/bin/rssi.rs
"""

# INFO: I used claude to help me write this visualizer and I was not able to verify the correctness of the computed values.
# I am planning to verify it and change some of the graphical representations, but I wanted to share it as is for now. 
# Any help is welcome.

import argparse
from collections import deque
import queue
import re
import sys
import threading
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

TICKS_PER_SECOND = 69_120_000
TICKS_PER_FRAME = 691_200
SLOTS_PER_FRAME = 24

# Range of levels shown, from an empty bar to a full one
FLOOR_DBM = -110
CEILING_DBM = -40

# Level of samples taken while a packet was being received, which have no RSSI.
# Above every real level, so that maxima pick it up.
RECEIVING = float("inf")

BAR_WIDTH = 13
BLOCKS = " ▁▂▃▄▅▆▇█"
PARTIAL_BARS = " ▏▎▍▌▋▊▉"
# Terminal width from which a history column is shown to the right of the others
MIN_WIDTH_WITH_HISTORY = 100

escapes = re.compile("\x1b\\[(.*?)m")
rssi_pattern = re.compile(r"RSSI for ([0-9]+) at ([0-9]+): \[([^]]*)\]")
# Older logs name the channel
missed_pattern = re.compile(r"RSSI missed")
incomplete_pattern = re.compile(r"Sweep incomplete")
# Older rssi example logs have "time Ok(…)" and the full PDC
received_pattern = re.compile(r"Data received: time (?:Ok\()?([0-9]+)\)?, PCC (?:Ok\()?\[([^]]+)\]")
pdc_length_pattern = re.compile(r"PDC length (?:Ok\(([0-9]+)\)|Err\((\w+)\))")
signal_pattern = re.compile(r"RSSI (-?[0-9.]+) dBm, SNR (-?[0-9.]+) dB")
receive_error_pattern = re.compile(r"Receive error: (\w+)")
location_pattern = re.compile(r"\s+\(\S+ \S+:[0-9]+\)\s*$")

EOF = object()


def dbm(raw):
    """Signed dBm of a raw RSSI byte (as printed by defmt), or RECEIVING."""
    if raw == 0:
        return RECEIVING
    return raw - 256 if raw > 127 else raw


def frequency(channel):
    """Center frequency in MHz, as far as it is known (only band 1)."""
    if 1657 <= channel <= 1677:
        return 450.144 + 0.864 * channel
    return None


class Channel:
    def __init__(self, number, window):
        self.number = number
        # Per frame: (busy samples, total samples, slot maxima, highest measured level, whether a
        # packet was received); for statistics over the window
        self.frames = deque(maxlen=window)
        # Maximum of every frame, RECEIVING if a packet was received
        self.history = deque(maxlen=500)
        self.packets = 0


class Packet:
    def __init__(self, time, pcc, pdc, rssi, snr):
        self.time = time
        self.pcc = pcc
        self.pdc = pdc
        self.rssi = rssi
        self.snr = snr
        self.channel = None


class History:
    """The newest frame maxima of a channel, as many as fit, as blocks."""

    def __init__(self, analyzer, levels):
        self.analyzer = analyzer
        self.levels = levels

    def __rich_console__(self, console, options):
        width = options.max_width
        if width <= 0:
            return
        recent = list(self.levels)[-width:]
        text = Text(no_wrap=True)
        for level in [None] * (width - len(recent)) + recent:
            text.append_text(self.analyzer.block(level))
        yield text

    def __rich_measure__(self, console, options):
        return Measurement(0, options.max_width)


class Analyzer:
    def __init__(self, args):
        self.free = args.free
        self.busy = args.busy
        self.window = args.window
        self.channels = {}
        # (start time, channel) of recent frames, to find which channel a packet came in on
        self.recent_frames = deque(maxlen=64)
        # Packets whose frame was not reported yet
        self.pending = []
        self.packets = deque(maxlen=args.packets)
        self.messages = deque(maxlen=4)
        # Why the board stopped; kept apart as the backtrace that follows would push it out
        self.panic = []
        self.receive_errors = 0
        self.missed = 0
        self.incomplete = 0
        self.first_time = None
        # Start times of recent frames, to tell how fast the sweep goes
        self.frame_times = deque(maxlen=200)

    def channel(self, number):
        if number not in self.channels:
            self.channels[number] = Channel(number, self.window)
        return self.channels[number]

    def feed(self, line):
        """Processes a log line; returns the device time of RSSI frames (to pace replays)."""
        line = escapes.sub("", line)

        if rssi := rssi_pattern.search(line):
            (number, start, data) = rssi.groups()
            start = int(start)
            self.frame(self.channel(int(number)), start, [dbm(int(i)) for i in data.split(", ")])
            return start

        if received := received_pattern.search(line):
            (pcc_time, pcc) = received.groups()
            pdc = None
            if pdc_length := pdc_length_pattern.search(line):
                (length, error) = pdc_length.groups()
                pdc = int(length) if length is not None else error
            rssi = snr = None
            if signal := signal_pattern.search(line):
                (rssi, snr) = (float(i) for i in signal.groups())
            self.pending.append(Packet(int(pcc_time), [int(i) for i in pcc.split(", ")], pdc, rssi, snr))
            self.attribute_pending()
        elif missed_pattern.search(line):
            self.missed += 1
        elif receive_error_pattern.search(line):
            self.receive_errors += 1
        elif incomplete_pattern.search(line):
            self.incomplete += 1
        elif line.strip():
            line = location_pattern.sub("", line.rstrip())
            # The message is in the same line, or (for panics outside of defmt) in the next.
            if ("panicked at" in line and not self.panic) or (
                len(self.panic) == 1 and self.panic[0].endswith(":")
            ):
                self.panic.append(line.strip())
            self.messages.append(line)
        return None

    def frame(self, channel, start, values):
        if self.frame_times and start < self.frame_times[-1]:
            # The device started over; its earlier times are meaningless now.
            self.frame_times.clear()
            self.recent_frames.clear()
            self.first_time = None
        if self.first_time is None:
            self.first_time = start
        self.frame_times.append(start)
        self.recent_frames.append((start, channel))

        # Slots are counted from where the frame starts on the device's clock (as in
        # show-rssi.py), so that they line up from one frame to the next.
        offset = int((start % TICKS_PER_FRAME) / TICKS_PER_FRAME * len(values))
        slots = [None] * SLOTS_PER_FRAME
        busy = 0
        for (i, value) in enumerate(values):
            if value > self.busy:
                busy += 1
            slot = (i + offset) % len(values) * SLOTS_PER_FRAME // len(values)
            if slots[slot] is None or value > slots[slot]:
                slots[slot] = value

        measured = [v for v in values if v != RECEIVING]
        receiving = len(measured) < len(values)
        channel.frames.append((busy, len(values), slots, max(measured, default=None), receiving))
        channel.history.append(max(values))
        self.attribute_pending()

    def attribute_pending(self):
        newest = self.recent_frames[-1][0] if self.recent_frames else None
        still_pending = []
        for packet in self.pending:
            for (start, channel) in reversed(self.recent_frames):
                if start <= packet.time < start + TICKS_PER_FRAME:
                    packet.channel = channel.number
                    channel.packets += 1
                    break
            else:
                # Its frame should have been reported by now; it is not going to be.
                if newest is None or packet.time + 3 * TICKS_PER_FRAME > newest:
                    still_pending.append(packet)
                    continue
            self.packets.appendleft(packet)
        self.pending = still_pending

    def style(self, level):
        if level is None:
            return "dim"
        if level == RECEIVING:
            return "bold magenta"
        if level > self.busy:
            return "bold red"
        if level > self.free:
            return "yellow"
        return "green"

    @staticmethod
    def fraction(level):
        if level == RECEIVING:
            return 1
        return min(1, max(0, (level - FLOOR_DBM) / (CEILING_DBM - FLOOR_DBM)))

    def block(self, level):
        if level is None:
            return Text(" ")
        return Text(BLOCKS[max(1, round(self.fraction(level) * 8))], style=self.style(level))

    def bar(self, level, receiving):
        if level is None:
            return Text(" " * BAR_WIDTH)
        eighths = round(self.fraction(level) * BAR_WIDTH * 8)
        (full, partial) = divmod(eighths, 8)
        text = "█" * full + (PARTIAL_BARS[partial] if partial else "")
        return Text(text.ljust(BAR_WIDTH), style=self.style(RECEIVING if receiving else level))

    def sweep_rate(self):
        """Frames per second, by device time."""
        if len(self.frame_times) < 2:
            return None
        return (len(self.frame_times) - 1) * TICKS_PER_SECOND / (self.frame_times[-1] - self.frame_times[0])

    def render(self, source, width):
        rate = self.sweep_rate()
        header = Text.assemble(
            ("DECT NR+ channel usage", "bold"),
            f"  from {source}",
        )
        if rate and self.channels:
            header.append(
                f"  ·  {rate / len(self.channels):.1f} sweeps/s, each channel watched "
                f"{rate / len(self.channels) / 100:.0%} of the time"
            )
        if self.receive_errors:
            header.append(f"  ·  {self.receive_errors} receive errors")
        if self.missed:
            header.append(f"  ·  {self.missed} RSSI reports missed")
        if self.incomplete:
            header.append(f"  ·  {self.incomplete} sweeps incomplete")

        # Only when the other columns fit next to it with some to spare
        show_history = width >= MIN_WIDTH_WITH_HISTORY
        table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), collapse_padding=True, show_edge=False, expand=show_history)
        table.add_column("Ch", justify="right", no_wrap=True)
        table.add_column("MHz", justify="right", no_wrap=True)
        table.add_column("Level now", min_width=BAR_WIDTH, no_wrap=True)
        table.add_column("dBm", justify="right", no_wrap=True)
        table.add_column("Peak", justify="right", no_wrap=True)
        table.add_column("Busy", justify="right", no_wrap=True)
        table.add_column("Pkts", justify="right", no_wrap=True)
        table.add_column("Slots in frame", min_width=SLOTS_PER_FRAME, no_wrap=True)
        if show_history:
            table.add_column("History", ratio=1)

        for channel in sorted(self.channels.values(), key=lambda c: c.number):
            (_, _, _, now, receiving) = channel.frames[-1]
            peak = max((f[3] for f in channel.frames if f[3] is not None), default=None)
            busy = sum(f[0] for f in channel.frames)
            total = sum(f[1] for f in channel.frames)
            share = busy / total if total else 0

            slots = Text()
            for slot in range(SLOTS_PER_FRAME):
                slots.append_text(self.block(max(
                    (f[2][slot] for f in channel.frames if f[2][slot] is not None),
                    default=None,
                )))

            mhz = frequency(channel.number)
            row = [
                str(channel.number),
                f"{mhz:.3f}" if mhz else "",
                self.bar(now, receiving),
                Text("" if now is None else str(now), style="bold magenta" if receiving else ""),
                "" if peak is None else str(peak),
                Text(f"{share:.0%}", style="bold red" if share >= 0.1 else "yellow" if share > 0 else "dim"),
                Text(str(channel.packets), style="bold magenta" if channel.packets else "dim"),
                slots,
            ]
            if show_history:
                row.append(History(self, channel.history))
            table.add_row(*row)

        legend = Text.assemble(
            ("█ free", self.style(self.free)), f" ≤ {self.free} dBm < ",
            ("█ possible", self.style(self.busy)), f" ≤ {self.busy} dBm < ",
            ("█ busy", self.style(self.busy + 1)), "  ",
            ("█ receiving a packet", self.style(RECEIVING)), "  ·  ",
            f"Busy: share of the time above {self.busy} dBm or receiving, over the last {self.window} frames",
            style="dim",
        )

        parts = [header, table, legend]
        if self.panic:
            parts.insert(1, Text("Board stopped: " + " ".join(self.panic), style="bold red"))

        if self.packets:
            packets = Table(box=box.SIMPLE_HEAD, padding=(0, 1), collapse_padding=True, show_edge=False, title="Recent packets", title_justify="left")
            for (name, justify) in [("Time", "right"), ("Ch", "right"), ("Type", "right"), ("Network", "right"),
                                    ("From", "right"), ("Length", "right"), ("MCS", "right"), ("PDC", "right"),
                                    ("RSSI", "right"), ("SNR", "right")]:
                packets.add_column(name, justify=justify)
            for packet in self.packets:
                pcc = packet.pcc
                # Fields as in ETSI TS 103 636-4 V2.1.1 Section 6.2
                length = (pcc[0] & 0x0f) + 1
                unit = "slots" if pcc[0] & 0x10 else "subslots"
                mcs = pcc[4] & (0x07 if len(pcc) == 5 else 0x0f)
                if packet.pdc is None:
                    pdc = ""
                elif isinstance(packet.pdc, int):
                    pdc = f"{packet.pdc} B"
                else:
                    pdc = packet.pdc
                packets.add_row(
                    f"{(packet.time - (self.first_time or packet.time)) / TICKS_PER_SECOND:.2f} s",
                    str(packet.channel) if packet.channel is not None else "?",
                    "1" if len(pcc) == 5 else "2",
                    f"{pcc[1]:02x}",
                    f"{pcc[2]:02x}{pcc[3]:02x}",
                    f"{length} {unit}",
                    str(mcs),
                    pdc,
                    f"{packet.rssi:.1f} dBm" if packet.rssi is not None else "",
                    f"{packet.snr:.1f} dB" if packet.snr is not None else "",
                )
            parts.append(packets)

        if self.messages:
            parts.append(Text("Other output:", style="bold"))
            parts.extend(Text(m, style="dim", no_wrap=True, overflow="ellipsis") for m in self.messages)

        return Group(*parts)


def read_lines(source, lines):
    """Puts the source's lines into the queue, following a file as it grows."""
    if source == "-":
        for line in sys.stdin:
            lines.put(line)
        lines.put(EOF)
        return

    with open(source, errors="replace") as file:
        partial = ""
        while True:
            line = file.readline()
            if not line:
                time.sleep(0.1)
                continue
            partial += line
            if partial.endswith("\n"):
                lines.put(partial)
                partial = ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="log file to follow, or - for standard input")
    parser.add_argument("--free", type=int, default=-85, help="highest level of a free channel, in dBm (default: %(default)s)")
    parser.add_argument("--busy", type=int, default=-71, help="level above which a channel is busy, in dBm (default: %(default)s)")
    parser.add_argument("--window", type=int, default=25, help="frames per channel for the Busy, Peak and Slots columns (default: %(default)s)")
    parser.add_argument("--packets", type=int, default=6, help="number of recent packets shown (default: %(default)s)")
    parser.add_argument("--replay", action="store_true", help="go through a recorded log at the speed it was recorded")
    parser.add_argument("--speed", type=float, default=1.0, help="speed up a replay by this factor (default: %(default)s)")
    args = parser.parse_args()
    if args.free > args.busy:
        parser.error("--free must not be above --busy")
    if args.speed <= 0:
        parser.error("--speed must be positive")

    lines = queue.Queue(maxsize=10_000)
    threading.Thread(target=read_lines, args=(args.log, lines), daemon=True).start()

    analyzer = Analyzer(args)
    console = Console()
    source = "standard input" if args.log == "-" else args.log
    live = None
    # Device time and wall clock time that a replay is aligned to
    replay_anchor = None
    next_refresh = 0

    def refresh():
        nonlocal next_refresh
        if live is not None and time.monotonic() >= next_refresh:
            live.update(analyzer.render(source, console.width), refresh=True)
            next_refresh = time.monotonic() + 0.2

    try:
        while True:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                refresh()
                continue
            if line is EOF:
                break

            device_time = analyzer.feed(line)

            if live is None:
                if device_time is None:
                    # Until the analysis starts, this is the output of building and flashing.
                    sys.stdout.write(line)
                    continue
                live = Live(console=console, screen=True, auto_refresh=False)
                live.start()

            if args.replay and device_time is not None:
                if replay_anchor is None or device_time < replay_anchor[0]:
                    replay_anchor = (device_time, time.monotonic())
                due = replay_anchor[1] + (device_time - replay_anchor[0]) / TICKS_PER_SECOND / args.speed
                while (delay := due - time.monotonic()) > 0:
                    refresh()
                    time.sleep(min(delay, 0.05))

            refresh()
    except KeyboardInterrupt:
        pass
    finally:
        if live is not None:
            live.stop()

    if analyzer.channels:
        # Keep the last state on screen after leaving the full screen view.
        console.print(analyzer.render(source, console.width))
    else:
        sys.exit("No RSSI reports seen; is this the output of the channel-analyzer example?")


if __name__ == "__main__":
    main()

