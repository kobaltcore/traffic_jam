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


class Tickable(ABC):

    @abstractmethod
    def tick(self, tick_no):
        pass


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


class TouchStrip(Tickable):

    def __init__(self, device_port, relay_port, note):
        self.device_port = device_port
        self.relay_port = relay_port
        self.note = note
        self.state = 0
        self.prev_state = 0
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
                 note_action=None, callback=None):
        self.device_port = device_port
        self.relay_port = relay_port
        self.note = note
        self.state = ButtonState.INACTIVE
        self.prev_state = None
        self.needs_tick = True
        self.led_state = {}
        self.led_state["inactive"] = led_state_inactive or LedState("black", "dim")
        self.led_state["active"] = led_state_active or LedState("black", "bright")
        self.note_action = note_action
        self.callback = callback

    def tick(self, tick_no):
        # If the state has not change, there is no need to update
        if not self.needs_tick and self.prev_state == self.state:
            return

        if self.callback:
            self.callback(self.state)

        if self.state == ButtonState.INACTIVE:
            self.device_port.send(mido.Message("control_change", control=self.note,
                                               value=self.led_state["inactive"].color_value()))
            if self.note_action:
                if isinstance(self.note_action, int):
                    self.relay_port.send(mido.Message("control_change", control=self.note_action, value=0))
                elif isinstance(self.note_action, list) or isinstance(self.note_action, tuple):
                    for note in self.note_action:
                        self.relay_port.send(mido.Message("control_change", control=note, value=0))
            else:
                self.relay_port.send(mido.Message("control_change", control=self.note, value=0))
        if self.state == ButtonState.ACTIVE:
            self.device_port.send(mido.Message("control_change", control=self.note,
                                               value=self.led_state["active"].color_value()))
            if self.note_action:
                if isinstance(self.note_action, int):
                    self.relay_port.send(mido.Message("control_change", control=self.note_action, value=127))
                elif isinstance(self.note_action, list) or isinstance(self.note_action, tuple):
                    for note in self.note_action:
                        self.relay_port.send(mido.Message("control_change", control=note, value=127))

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
                 note_action=None):
        self.device_port = device_port
        self.relay_port = relay_port
        self.note = note
        self.state = ButtonState.INACTIVE
        self.prev_state = None
        self.needs_tick = True  # indicates that properties have changed, requiring a redraw
        self.led_state = {}
        self.led_state["inactive"] = led_state_inactive or LedState("black", "dim")
        self.led_state["active"] = led_state_active or LedState("black", "bright")
        self.note_action = note_action

    def tick(self, tick_no):
        # If the state has not change, there is no need to update
        if not self.needs_tick and self.prev_state == self.state:
            return

        if self.state == ButtonState.INACTIVE:
            self.device_port.send(mido.Message("note_on", note=self.note,
                                               velocity=self.led_state["inactive"].color_value()))
            if self.note_action:
                if isinstance(self.note_action, int):
                    self.relay_port.send(mido.Message("note_on", note=self.note_action, velocity=0))
                elif isinstance(self.note_action, list) or isinstance(self.note_action, tuple):
                    for note in self.note_action:
                        self.relay_port.send(mido.Message("note_on", note=note, velocity=0))
            else:
                self.relay_port.send(mido.Message("note_on", note=self.note, velocity=0))
        if self.state == ButtonState.ACTIVE:
            self.device_port.send(mido.Message("note_on", note=self.note,
                                               velocity=self.led_state["active"].color_value()))
            if self.note_action:
                if isinstance(self.note_action, int):
                    self.relay_port.send(mido.Message("note_on", note=self.note_action, velocity=127))
                elif isinstance(self.note_action, list) or isinstance(self.note_action, tuple):
                    for note in self.note_action:
                        self.relay_port.send(mido.Message("note_on", note=note, velocity=127))

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
        self.reset_grid()

    def shutdown(self):
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

        # Play button
        def adjust_clock(state):
            if state == ButtonState.ACTIVE:
                print("toggled clock")
                CLOCK.toggle_lock()
        self.special_buttons[8].callback = adjust_clock

        # Record button
        def adjust_clock(state):
            if state == ButtonState.ACTIVE:
                print("reset clock")
                CLOCK.lock()
                CLOCK.tick_no = 0
        self.special_buttons[9].callback = adjust_clock

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
        if self.timeline:
            data = self.timeline.get(tick_no)
            if data:
                if self.data_cache and not self.data_cache == data:
                    self.reset_grid()

                self.data_cache = data

                for note, spec in data.items():
                    if isinstance(note, int):
                        self.grid[note].led_state["active"] = spec["led_state"]["active"]
                        self.grid[note].led_state["inactive"] = spec["led_state"]["inactive"]
                        self.grid[note].note_action = spec["note_action"]
                        self.grid[note].needs_tick = True
                    elif note.startswith("cc"):
                        self.special_buttons[int(note.lstrip("cc"))].led_state["active"] = spec["led_state"]["active"]
                        self.special_buttons[int(note.lstrip("cc"))].led_state[
                            "inactive"] = spec["led_state"]["inactive"]
                        self.special_buttons[int(note.lstrip("cc"))].note_action = spec["note_action"]
                        self.special_buttons[int(note.lstrip("cc"))].needs_tick = True

        for button in self.grid.values():
            button.tick(tick_no)

        for button in self.special_buttons.values():
            button.tick(tick_no)

        for strip in self.touch_strips.values():
            strip.tick(tick_no)


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
                # if bare int or int with suffix "t", treat as tick value
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

                # Note Action
                note_action_spec = note_spec.get("note", None)
                if not note_action_spec:
                    self.data[tick_index][note]["note_action"] = note
                else:
                    note_action = NOTE_DB.get(note_action_spec, note_action_spec)
                    self.data[tick_index][note]["note_action"] = note_action

                # LED State
                led_spec = note_spec.get("led", None)

                if not led_spec:
                    led_state_active = LedState(color="orange", state="bright")
                    led_state_inactive = LedState(color="orange", state="dim")
                else:
                    led_spec_active = led_spec.get("active", None)
                    led_spec_inactive = led_spec.get("inactive", None)
                    if led_spec_active:
                        led_state_active = LedState(color=led_spec_active.get("color", "orange"),
                                                    state=led_spec_active.get("state", "bright"))
                    if led_spec_inactive:
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
        print("\nAborted")
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
