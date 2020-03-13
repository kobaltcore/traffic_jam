# Traffic JAM
Take control of your [Maschine JAM](https://www.native-instruments.com/en/products/maschine/production-systems/maschine-jam/).

This script uses the standalone "MIDI Mode" common to all `Maschine` controllers to render custom control surfaces that can change over time.

At the current moment, its main purpose is to remap buttons to different MIDI signals than what they typically output. While the same can be achieved using ControllerEditor, the added value in this case is that mappings can be changed on the fly.

Features:
- Custom color per button, per state (`active` or `inactive`)
- Custom MIDI output per button, i.e. remapping input `17` to output `43`
- Multi-note output mapping, i.e. remapping input `17` to output `43`, `45` and `46`
- Changing mappings at specific points in time, synchronized to the BPM of the song
- Remap Touch Stripes and CC buttons to any other MIDI message (or multiple messages)

Traffic JAM operates on a timeline that can be tick- or time-indexed, meaning that configurations of buttons, lights and note mappings can automatically change at specific points in a song. Alternatively, this could also be used to implement light shows for this controller.
