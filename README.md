# Loko Ground Unit Firmware

This repository contains a modified version of the Loko ground-unit firmware.
It is a fork of the original Loko project, focused on the ground-unit code and
the stability and usability fixes documented below. The ground unit receives
LoRa packets from Loko Air, parses them, and forwards valid data to the mobile
app over BLE.

Loko project:
- https://github.com/tomipiriyev/Loko

## What This Repo Is For

`main.py` is the firmware for the Loko Ground unit.

## Changes From Upstream

This fork keeps the ground-unit firmware focused and easier to use in practice.
The main changes versus the original Loko project are in
[LEDs And Button Behavior](#leds-and-button-behavior), with the rest focused on
stability and cleanup:

- smarter LED usage and button handling
  - blinking is tick-driven instead of using `sleep_ms()` inside LED helpers
  - silent mode reduces visual noise by suppressing steady blue and green packet flashes
  - button edges are queued from IRQs so double-click detection is reliable even with quick taps
- safer shutdown flow
  - long-press shutdown is armed first, then triggered on release
- quieter normal operation
  - debug output is behind a flag
- less flash wear
  - logging is disabled by default and can be turned off completely
- more robust packet parsing
  - malformed packets return `None` instead of crashing the loop

## Flashing The Ground Firmware

NoliLab documents the official firmware update flow here:
- https://www.nolilab.com/firmware/

For the ground unit, that page says to:
- install Thonny IDE
- download the ground firmware
- connect the Loko Ground unit to the PC over USB
- turn the unit on with the button on the device
- open Thonny IDE

In this repo, the ground firmware is `main.py`, so that is the file you work with on the ground unit.

## Air Firmware And Configuration

The same NoliLab firmware page also covers Loko Air firmware updates:
- https://www.nolilab.com/firmware/

For device configuration, NoliLab provides an online tool:
- https://www.nolilab.com/loko-config-tool/

That tool requires Chrome or Chromium-based browsers because it uses the Web Serial API.
It is intended for configuring the Loko Air unit from the browser.

This README only points to the Air-side firmware and configuration documentation.
The code in this repository is for the ground unit.

## Matching ID1, ID2, And Frequency

The Air and Ground units must be configured to match each other.

- `id1` is the tracker identifier. It identifies the Air unit itself.
- `id2` is the match/filter identifier used by the Ground unit to decide whether a packet should be forwarded.
- `frequency` must be the same on Air and Ground or they will not hear each other over LoRa.

If `id2` and `frequency` values do not match, the Ground unit may still receive radio traffic, but it
will reject or ignore packets that are not intended for the configured tracker pair.

## Ground Configuration

The ground unit settings are stored in `settings.json`.
Use Thonny to edit this file on the Ground unit.

Typical fields:
- `id2`
- `freq`
- `p2p_key`

Notes:
- `freq` is stored in Hz in this firmware.
- `id2` should match the Air unit you want this ground unit to accept.
- `p2p_key` must be a 64-character hex string when encryption is enabled.

## LEDs And Button Behavior

The LEDs are active-low:
- `0` means ON
- `1` means OFF

### LED Colors

- Blue:
  - blinking means BLE is waiting for a connection
  - solid means BLE is connected in verbose mode
  - in silent mode, solid blue is suppressed, but the disconnected blink still remains
- Green:
  - in verbose mode, green pulses on valid received packets
  - green double-blinks when a packet is forwarded over BLE
  - in silent mode, green is quiet during normal packet traffic
  - a single button click shows the current mode with a green status flash
- Red:
  - low battery warning uses red pulses
  - critical low battery pulses faster
  - solid red means shutdown is armed or the unit is shutting down

### Button Use

- Single click:
  - show current status
  - green pulse = verbose mode
  - green double-blink = silent mode
- Double click:
  - toggle between verbose and silent mode
  - the double-click window is tuned for quick taps, so you do not need to pause between clicks
- Hold for 3 seconds:
  - arms shutdown
  - once armed, the red LED becomes solid
  - release the button to actually power off

### Low Battery Behavior

Low battery is handled in stages:
- normal warning: occasional red pulses
- critical warning: faster red pulses
- shutdown: solid red, then power-off and deep sleep

## Current Runtime Flags

At the top of `main.py`:

- `DEBUG = False`
  - hides diagnostic prints
- `LOG_ENABLED = False`
  - disables flash-backed logging
- `VERBOSE_MODE = True`
  - controls packet activity indications on the green LED
  - also controls whether steady blue is shown when BLE is connected

## Notes

- This repo is focused on the ground unit firmware.
- The mobile app and the Air unit firmware/configuration are separate parts of the Loko system.
- If you change `id1`, `id2`, `freq`, or encryption settings, make sure both units are updated to match.
