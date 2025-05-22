# NMS Newton (beta test version (0.2.0))

Moving planets, in NMS!

## Before you start:

This mod doesn't implement saving yet, so for now, always ensure that you start and end your session in your space ship otherwise you may get softlocked potentially (don't blame me if you mess up a save!)

## How to use:

To use this mod you will need python installed (between version 3.9 and 3.11 inclusive)
Before you start, ensure steam is running, then run the following commands from this folder (easiest way is to type `cmd` in the windows explorer navigation bar which will open the command prompt in that folder):

1. `python -m pip install uv`
2. `uv run newton.py`

The game should start up automatically and you should see a log window as well as another popup which lets you modify some parameters of the mod.
Once you have loaded in, you can untick the "Simulation paused" check box for the planets to start moving.
Note: They will be quite slow, so if you want them faster change the "Time rate". If you set this too high it may be impossible to land on planets, so the default speed while slow is meant to be a nice compromise (they move at a nice rate while you are on a planet - it's more noticable when they near the horizon!)
