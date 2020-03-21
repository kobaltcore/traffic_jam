import sys
import time
import resource
import argparse
from enum import IntEnum
from abc import ABC, abstractmethod
from collections import defaultdict

import yaml

from termcolor import colored

import mido

from durations_nlp import Duration


def defaultdict_rec():
    return defaultdict(defaultdict_rec)


### State Keeping ###

class LedState:

    def __init__(self, color, state):
        self.color = color
        self.state = state

    def color_value(self):
        return PALETTE[self.color][self.state]

    def __repr__(self):
        return f"LedState(color={self.color}, state={self.state})"


class ButtonState(IntEnum):
    INACTIVE = 0
    ACTIVE = 1


### Abstract Base Classes ###

class Tickable(ABC):

    @abstractmethod
    def tick(self, tick_no):
        pass


class NoteAction(ABC):

    @abstractmethod
    def execute(self, state):
        pass

    def __call__(self, state):
        return self.execute(state) or state


### Utility Classes ###

class Palette:

    def __init__(self, filename):
        self.data = defaultdict(dict)

        with open(filename, "r") as f:
            palette_data = yaml.full_load(f)

        for color_name, start in palette_data["colors"].items():
            for state_name, index in palette_data["states"].items():
                self.data[color_name][state_name] = start + index

    def get(self, key, default):
        return self.data.get(key, default)

    def __len__(self):
        return len(self.data)

    def __setitem__(self, key, item):
        self.data[key] = item

    def __getitem__(self, key):
        return self.data[key]

    def __delitem__(self, key):
        del self.data[key]


class NoteDB:

    def __init__(self, filename):
        with open(filename, "r") as f:
            notes_data = yaml.full_load(f)

        self.data = {}
        for i, step in enumerate(range(0, 132, 12)):
            for note, num in notes_data.items():
                if step + num >= 127:
                    break
                self.data[f"{note.upper()}{i - 1}"] = step + num
                self.data[f"{note.lower()}{i - 1}"] = step + num

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __len__(self):
        return len(self.data)

    def __setitem__(self, key, item):
        self.data[key] = item

    def __getitem__(self, key):
        return self.data[key]

    def __delitem__(self, key):
        del self.data[key]


class Timeline:

    def __init__(self, filename):
        with open(filename, "r") as f:
            timeline_data = yaml.full_load(f)

        self.data = dict()
        for index, time_spec in timeline_data.items():
            try:
                # if bare int treat as tick value
                tick_index = int(index)
            except:
                # otherwise treat as natural language duration
                tick_index = CLOCK.seconds_to_ticks(Duration(index).to_seconds())
            self.data[tick_index] = defaultdict_rec()

            for note, note_spec in time_spec.items():
                try:
                    note = int(note)
                except:
                    pass

                self.data[tick_index][note]["channel"] = note_spec.get("channel", 0)

                self.data[tick_index][note]["sticky"] = note_spec.get("sticky", False)

                action_spec = note_spec.get("action", None)
                if not action_spec:
                    self.data[tick_index][note]["action"] = None
                else:
                    tokens = action_spec.split()
                    if tokens[0] == "print":
                        self.data[tick_index][note]["action"] = PrintAction(" ".join(tokens[1:]))

                # Note Action
                note_output_spec = note_spec.get("note", None)
                if not note_output_spec:
                    self.data[tick_index][note]["note_output"] = note
                else:
                    if isinstance(note_output_spec, str):
                        note_output = []
                        for _note in note_output_spec.split():
                            note_output.append(NOTE_DB.get(_note))
                        note_output = tuple(note_output)
                    else:
                        note_output = note
                    self.data[tick_index][note]["note_output"] = note_output

                # LED State
                led_spec = note_spec.get("led", None)

                if not led_spec:
                    led_state_active = LedState(color="orange", state="bright")
                    led_state_inactive = LedState(color="orange", state="dim")
                else:
                    led_spec_active = led_spec.get("active", {})
                    led_spec_inactive = led_spec.get("inactive", {})
                    led_state_active = LedState(color=led_spec_active.get("color", "orange"),
                                                state=led_spec_active.get("state", "bright"))
                    led_state_inactive = LedState(color=led_spec_inactive.get("color", "orange"),
                                                  state=led_spec_inactive.get("state", "dim"))

                self.data[tick_index][note]["led_state"]["active"] = led_state_active
                self.data[tick_index][note]["led_state"]["inactive"] = led_state_inactive

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __len__(self):
        return len(self.data)

    def __setitem__(self, key, item):
        self.data[key] = item

    def __getitem__(self, key):
        return self.data[key]

    def __delitem__(self, key):
        del self.data[key]


### Action Classes ###

class PrintAction(NoteAction):

    def __init__(self, message):
        self.message = message

    def execute(self, state):
        if state == ButtonState.ACTIVE:
            print(self.message)


class ClockToggleAction(NoteAction):

    def execute(self, state):
        if state == ButtonState.ACTIVE:
            CLOCK.toggle_lock()
            print("{} clock".format(colored("Paused", "yellow") if CLOCK.locked else colored("Started", "green")))


class ClockResetAction(NoteAction):

    def execute(self, state):
        if state == ButtonState.ACTIVE:
            print("{} clock".format(colored("Reset", "red")))
            CLOCK.lock()
            CLOCK.tick_no = 0


class ClockForwardAction(NoteAction):

    def __init__(self, step):
        if isinstance(step, float):
            self.step = max(int(CLOCK.ppq * step), 1)
        else:
            self.step = max(step, 1)

    def execute(self, state):
        if state == ButtonState.ACTIVE and not CLOCK.warping:
            CLOCK.warp(self.step)
            print(f"Warped forward by {self.step} ticks")


class ClockRewindAction(NoteAction):

    def __init__(self, step):
        if isinstance(step, float):
            self.step = max(int(CLOCK.ppq * step), 1)
        else:
            self.step = max(step, 1)

    def execute(self, state):
        if state == ButtonState.ACTIVE and not CLOCK.warping:
            CLOCK.warp(self.step, reverse=True)
            print(f"Warped backward by {self.step} ticks")


### Tickable Classes ###

class Clock:

    def __init__(self, bpm=120, ppq=24, locked=False):
        self.bpm = bpm
        self.ppq = ppq
        self.started = False
        self.next = time.time()
        self.tick_no = 0
        self.tick_length = 60 / (self.bpm * self.ppq)
        self.registered_objects = []
        self.registered_cues = []
        self.cpu = CPU()
        self.locked = locked
        self.warping = False

    def seconds_to_ticks(self, seconds):
        return seconds / self.tick_length

    def ticks_to_seconds(self, ticks):
        return ticks * self.tick_length

    def register(self, obj):
        self.registered_objects.append(obj)

    def register_cue(self, when, func, args, absolute=False):
        if absolute:
            self.registered_cues.append((when, func, args))
        else:
            self.registered_cues.append((self.tick_no + when, func, args))

    def warp(self, step, reverse=False):
        self.warping = True
        for i in range(step):
            self.tick()
            if reverse:
                self.tick_no -= 1
                if self.tick_no < 0:
                    self.tick_no = 0
                    break
            else:
                self.tick_no += 1
        self.warping = False

    def tick(self):
        """Ticks the state of the application."""
        expired_cues = [cue for cue in self.registered_cues if cue[0] <= self.tick_no]

        for when, func, args in expired_cues:
            func(*args)

        for cue in expired_cues:
            self.registered_cues.remove(cue)

        for obj in self.registered_objects:
            obj.tick(self.tick_no)

        self.cpu.tick(self.tick_no)

    def lock(self):
        self.locked = True

    def unlock(self):
        self.locked = False

    def toggle_lock(self):
        self.locked = not self.locked

    # Return how long it is until the next tick.
    # (Or zero if the next tick is due now, or overdue.)
    def poll(self):
        now = time.time()
        if now < self.next:
            return self.next - now
        self.tick()
        if not self.locked:
            self.tick_no += 1
        # Compute when we're due next
        self.next += 60.0 / self.bpm / 24
        if now > self.next:
            if self.started:
                print("We're running late by {:.2f} seconds!".format(self.next - now))
            # If we are late, should we try to stay aligned, or skip?
            margin = 0.0  # Put 1.0 for pseudo-realtime
            if now > self.next + margin:
                if self.started:
                    print("Catching up (deciding that next tick = now).")
                self.next = now
            return 0
        self.started = True
        return self.next - now

    # Wait until next tick is due.
    def once(self):
        time.sleep(self.poll())


class CPU(Tickable):

    def __init__(self, report_interval=10):
        self.last_usage = 0
        self.last_time = 0
        self.last_shown = 0
        self.report_interval = report_interval

    def tick(self, tick):
        r = resource.getrusage(resource.RUSAGE_SELF)
        new_usage = r.ru_utime + r.ru_stime
        new_time = time.time()
        if new_time > self.last_shown + self.report_interval:
            percent = ((new_usage - self.last_usage) / (new_time - self.last_time)) * 100
            print("CPU usage: {}%".format(colored(f"{percent:.2f}", "red" if percent > 90 else "green")))
            self.last_shown = new_time
        self.last_usage = new_usage
        self.last_time = new_time


class TouchStrip(Tickable):

    def __init__(self, device_port, relay_port, note):
        self.device_port = device_port
        self.relay_port = relay_port
        self.note = note
        self.state = 0
        self.prev_state = 0
        self.needs_tick = True

    def reset(self):
        self.state = 0
        self.pev_state = 0
        self.needs_tick = True

    def tick(self, tick_no):
        if not self.needs_tick and self.prev_state == self.state:
            return

        self.device_port.send(mido.Message("control_change", control=self.note,
                                           value=self.state))
        self.relay_port.send(mido.Message("control_change", control=self.note,
                                          value=self.state))

        self.needs_tick = False
        self.prev_state = self.state

    def update(self, message):
        self.needs_tick = True
        self.state = message.value


class CCButton(Tickable):

    def __init__(self, device_port, relay_port, note,
                 led_state_inactive=None, led_state_active=None,
                 note_output=None, action=None):
        self.device_port = device_port
        self.relay_port = relay_port
        self.note = note
        self.state = ButtonState.INACTIVE
        self.prev_state = None
        self.needs_tick = True
        self.led_state = {}
        self.led_state["inactive"] = led_state_inactive or LedState("black", "dim")
        self.led_state["active"] = led_state_active or LedState("black", "bright")
        self.note_output = note_output
        self.action = action

    def reset(self):
        self.state = ButtonState.INACTIVE
        self.prev_state = None
        self.needs_tick = True
        self.led_state["inactive"] = LedState("black", "dim")
        self.led_state["active"] = LedState("black", "bright")
        self.note_output = None
        self.action = None

    def tick(self, tick_no):
        # If the state has not change, there is no need to update
        if not self.needs_tick and self.prev_state == self.state:
            return

        if self.action:
            self.state = self.action(self.state)

        if self.state == ButtonState.INACTIVE:
            self.device_port.send(mido.Message("control_change", control=self.note,
                                               value=self.led_state["inactive"].color_value()))
            if self.note_output:
                if isinstance(self.note_output, int):
                    self.relay_port.send(mido.Message("control_change", control=self.note_output, value=0))
                elif isinstance(self.note_output, tuple) or isinstance(self.note_output, list):
                    for note in self.note_output:
                        self.relay_port.send(mido.Message("control_change", control=note, value=0))
            else:
                self.relay_port.send(mido.Message("control_change", control=self.note, value=0))
        elif self.state == ButtonState.ACTIVE:
            self.device_port.send(mido.Message("control_change", control=self.note,
                                               value=self.led_state["active"].color_value()))
            if self.note_output:
                if isinstance(self.note_output, int):
                    self.relay_port.send(mido.Message("control_change", control=self.note_output, value=127))
                elif isinstance(self.note_output, tuple) or isinstance(self.note_output, list):
                    for note in self.note_output:
                        self.relay_port.send(mido.Message("control_change", control=note, value=127))
            else:
                self.relay_port.send(mido.Message("control_change", control=self.note, value=127))

        self.needs_tick = False
        self.prev_state = self.state

    def update(self, message):
        self.needs_tick = True

        if message.value == 0:
            self.state = ButtonState.INACTIVE
        elif message.value == 127:
            self.state = ButtonState.ACTIVE


class Button(Tickable):

    def __init__(self, device_port, relay_port, note,
                 led_state_inactive=None, led_state_active=None,
                 note_output=None, action=None, channel=0):
        self.device_port = device_port
        self.relay_port = relay_port
        self.note = note
        self.state = ButtonState.INACTIVE
        self.prev_state = None
        self.needs_tick = True
        self.led_state = {}
        self.led_state["inactive"] = led_state_inactive or LedState("black", "dim")
        self.led_state["active"] = led_state_active or LedState("black", "bright")
        self.note_output = note_output
        self.action = action
        self.channel = channel

    def reset(self):
        self.state = ButtonState.INACTIVE
        self.prev_state = None
        self.needs_tick = True
        self.led_state["inactive"] = LedState("black", "dim")
        self.led_state["active"] = LedState("black", "bright")
        self.note_output = None
        self.action = None

    def tick(self, tick_no):
        # If the state has not change, there is no need to update
        if not self.needs_tick and self.prev_state == self.state:
            return

        if self.action:
            self.state = self.action(self.state)

        if self.state == ButtonState.INACTIVE:
            self.device_port.send(mido.Message("note_on", note=self.note,
                                               velocity=self.led_state["inactive"].color_value()))
            if self.note_output:
                if isinstance(self.note_output, int):
                    self.relay_port.send(mido.Message("note_on", channel=self.channel,
                                                      note=self.note_output, velocity=0))
                elif isinstance(self.note_output, tuple) or isinstance(self.note_output, list):
                    for note in self.note_output:
                        self.relay_port.send(mido.Message("note_on", channel=self.channel,
                                                          note=note, velocity=0))
        elif self.state == ButtonState.ACTIVE:
            self.device_port.send(mido.Message("note_on", note=self.note,
                                               velocity=self.led_state["active"].color_value()))
            if self.note_output:
                if isinstance(self.note_output, int):
                    self.relay_port.send(mido.Message("note_on", channel=self.channel,
                                                      note=self.note_output, velocity=127))
                elif isinstance(self.note_output, tuple) or isinstance(self.note_output, list):
                    for note in self.note_output:
                        self.relay_port.send(mido.Message("note_on", channel=self.channel,
                                                          note=note, velocity=127))

        self.needs_tick = False
        self.prev_state = self.state

    def update(self, message):
        self.needs_tick = True

        if message.velocity == 0:
            self.state = ButtonState.INACTIVE
        elif message.velocity == 127:
            self.state = ButtonState.ACTIVE


class MaschineJam(Tickable):

    def __init__(self, port_name_in, port_name_out, port_name_relay):
        super().__init__()
        self.port_in = mido.open_input(port_name_in)
        self.port_out = mido.open_output(port_name_out)
        self.relay_port = mido.open_output(port_name_relay, virtual=True)
        self.port_in.callback = self.process_message
        self.timeline = None
        self.data_cache = None
        self.prev_tick = None
        self.grid = None
        self.reset_grid()

    def shutdown(self):
        for button in self.grid.values():
            button.reset()
            button.tick(0)

        for strip in self.touch_strips.values():
            strip.reset()
            strip.tick(0)

        for button in self.special_buttons.values():
            button.reset()
            button.tick(0)

        self.port_out.close()
        self.port_in.close()
        self.relay_port.close()

    def activate_timeline(self, timeline):
        self.timeline = timeline

    def reset_grid(self):
        self.grid = {i: Button(device_port=self.port_out, relay_port=self.relay_port, note=i)
                     for i in range(64)}
        self.touch_strips = {i + 48: TouchStrip(device_port=self.port_out, relay_port=self.relay_port, note=48 + i)
                             for i in range(8)}
        self.special_buttons = {i: CCButton(device_port=self.port_out, relay_port=self.relay_port, note=i)
                                for i in range(16)}

        self.grid[63].led_state["active"] = LedState("mint", "bright")
        self.grid[63].led_state["inactive"] = LedState("mint", "dim")

        self.special_buttons[8].action = ClockToggleAction()
        self.special_buttons[9].action = ClockResetAction()
        self.special_buttons[10].action = ClockRewindAction(50)
        self.special_buttons[11].action = ClockForwardAction(50)

    def process_message(self, message):
        if message.type == "note_on":
            if 0 <= message.note <= 64:
                self.grid[message.note].update(message)
            else:
                print("unhandled message", message)
        elif message.type == "control_change":
            if 0 <= message.control <= 16:
                self.special_buttons[message.control].update(message)
            elif 48 <= message.control <= 111:
                self.touch_strips[message.control].update(message)
            else:
                print("unhandled message", message)

    def tick(self, tick_no):
        if self.timeline and not self.prev_tick == tick_no:
            self.prev_tick = tick_no
            data = self.timeline.get(tick_no)
            if data:
                if self.data_cache and not self.data_cache == data:
                    # Reset buttons from the previous time slice
                    for note, note_spec in self.data_cache.items():
                        if note_spec["sticky"]:
                            continue
                        if isinstance(note, int):
                            self.grid[note].reset()
                        elif note.startswith("cc"):
                            cc_note = int(note.lstrip("cc"))
                            self.special_buttons[cc_note].reset()

                self.data_cache = data

                for note, spec in data.items():
                    if isinstance(note, int):
                        self.grid[note].led_state["active"] = spec["led_state"]["active"]
                        self.grid[note].led_state["inactive"] = spec["led_state"]["inactive"]
                        self.grid[note].note_output = spec["note_output"]
                        self.grid[note].action = spec["action"]
                        self.grid[note].channel = spec["channel"]
                        self.grid[note].needs_tick = True
                    elif note.startswith("cc"):
                        cc_note = int(note.lstrip("cc"))
                        self.special_buttons[cc_note].led_state["active"] = spec["led_state"]["active"]
                        self.special_buttons[cc_note].led_state["inactive"] = spec["led_state"]["inactive"]
                        self.special_buttons[cc_note].note_output = spec["note_output"]
                        self.special_buttons[cc_note].action = spec["action"]
                        self.special_buttons[cc_note].needs_tick = True

        if not CLOCK.locked:
            if (tick_no // CLOCK.ppq) % 2 == 0:
                if self.grid[63].state == ButtonState.INACTIVE:
                    self.grid[63].state = ButtonState.ACTIVE
                    self.grid[63].needs_tick = True
            elif self.grid[63].state == ButtonState.ACTIVE:
                self.grid[63].state = ButtonState.INACTIVE
                self.grid[63].needs_tick = True

        for button in self.grid.values():
            button.tick(tick_no)

        for button in self.special_buttons.values():
            button.tick(tick_no)

        for strip in self.touch_strips.values():
            strip.tick(tick_no)


def main(args):
    global CLOCK, NOTE_DB, PALETTE

    CLOCK = Clock(bpm=args.bpm, ppq=args.ppq, locked=True)

    NOTE_DB = NoteDB(args.notes_file)

    PALETTE = Palette(args.palette_file)

    timeline = None
    if args.timeline_file:
        timeline = Timeline(args.timeline_file)
    else:
        print(colored("Warning:", "yellow"), "No timeline file specified, no responses will be generated")

    maschine_jam_inputs = [item for item in mido.get_input_names() if "Maschine Jam" in item]
    maschine_jam_outputs = [item for item in mido.get_output_names() if "Maschine Jam" in item]

    if not maschine_jam_inputs or not maschine_jam_outputs:
        print(colored("Error:", "red"), "No Maschine Jam controller found, is it plugged in?")
        sys.exit(1)

    jam = MaschineJam(maschine_jam_inputs[0], maschine_jam_outputs[0], args.port_name_relay)

    if timeline:
        jam.activate_timeline(timeline)

    CLOCK.register(jam)

    try:
        while True:
            CLOCK.once()
    except KeyboardInterrupt:
        print("\nClosed")
        jam.reset_grid()
        CLOCK.tick()
        jam.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--timeline", type=str, dest="timeline_file",
                        metavar="file", help="(required) The file containing the timeline data")
    parser.add_argument("-n", "--notes", type=str, dest="notes_file", default="notes.yaml",
                        metavar="file", help="The file containing notes data")
    parser.add_argument("-c", "--palette", type=str, dest="palette_file", default="palette.yaml",
                        metavar="file", help="The file containing color data for the device")
    parser.add_argument("-r", "--relay-port", type=str, dest="port_name_relay", default="MJAM Out",
                        metavar="name", help="The name of the port to relay MIDI messages to")
    parser.add_argument("-b", "--bpm", type=int, dest="bpm", default=120,
                        metavar="number", help="Beats per Minute")
    parser.add_argument("-p", "--ppq", type=int, dest="ppq", default=24,
                        metavar="number", help="Pulses per Quarter Note")
    args = parser.parse_args()

    main(args)
